"""The recorder: class-level SQLAlchemy event hooks + finalize/report.

Design invariants (see build contract):
  * Never crash the host job: every hook body is wrapped in try/except.
  * Never store parameter VALUES — only counts, durations, and normalized SQL.
  * Cheap hot path: memoized library detection, bounded normalization cache,
    full stack capture only for the first 5 samples of each group.
  * finalize() prints exactly once, to stderr, via atexit AND a signal handler.
"""

import atexit
import functools
import json
import os
import signal
import statistics
import sys
import threading
import time
from time import perf_counter

from . import callsite, report, rules
from .callsite import capture_stack, get_async_site, resolve_call_site
from .normalize import group_key, normalize_sql
from .stats import GroupSnapshot, RunSnapshot

_NORM_CACHE_MAX = 4096


class Recorder:
    def __init__(self):
        self.installed = False
        self.finalized = False
        self.save_path = None

        self.start_perf = None
        self._start_times = None

        # per-group accumulators keyed by group_key
        self.groups = {}
        # normalization cache keyed by raw SQL string (bounded)
        self._norm_cache = {}
        # per-execution start times keyed by id(context)
        self._exec_start = {}
        # per-connection transaction generation (kept for --save completeness)
        self._txn_gen = {}

        self.total_queries = 0
        self.total_commits = 0
        self.total_rollbacks = 0
        self.total_rows = 0
        self.db_time = 0.0
        self.overhead_time = 0.0

        self.commit_time = 0.0
        self.commit_measurable = False   # only True once a dialect is wrapped

        self.sqlalchemy_active = False
        self._prev_handlers = {}

    # -- installation ----------------------------------------------------------

    def install(self, save_path=None):
        if self.installed:
            return
        self.installed = True
        self.save_path = save_path or None
        self.start_perf = perf_counter()
        try:
            self._start_times = os.times()
        except Exception:
            self._start_times = None

        try:
            import sqlalchemy  # noqa: F401
            from sqlalchemy import event
            from sqlalchemy.engine import Engine

            # class-level: every engine in the process is captured, zero config
            event.listen(Engine, "before_cursor_execute", self._before, named=True)
            event.listen(Engine, "after_cursor_execute", self._after, named=True)
            event.listen(Engine, "begin", self._on_begin)
            event.listen(Engine, "commit", self._on_commit)
            event.listen(Engine, "rollback", self._on_rollback)
            event.listen(Engine, "engine_connect", self._on_connect)
            self.sqlalchemy_active = True
        except Exception:
            self.sqlalchemy_active = False

        # A1: async call-site attribution — wrap the AsyncSession/AsyncConnection
        # user-entry coroutine methods so they stamp the call-site contextvar.
        self._install_async_wrappers()

        atexit.register(self.finalize)
        self._install_signal_handlers()
        self._start_interval_thread()

    # -- async call-site wrappers (A1) -----------------------------------------

    # coroutine methods that are user entry points for issuing SQL. commit()/
    # flush() issue their SQL OUTSIDE execute(), so the whole family is wrapped.
    _ASYNC_TARGETS = {
        "AsyncSession": (
            "execute", "scalars", "scalar", "stream", "stream_scalars",
            "get", "commit", "flush", "refresh",
        ),
        "AsyncConnection": ("execute", "scalar", "scalars", "stream"),
    }

    def _install_async_wrappers(self):
        try:
            from sqlalchemy.ext import asyncio as sa_async
        except Exception:
            return  # async not available -> skip silently
        for cls_name, methods in self._ASYNC_TARGETS.items():
            cls = getattr(sa_async, cls_name, None)
            if cls is None:
                continue
            for name in methods:
                try:
                    self._wrap_async_method(cls, name)
                except Exception:
                    pass

    def _wrap_async_method(self, cls, name):
        orig = getattr(cls, name, None)
        if orig is None:
            return  # method absent in this SQLAlchemy minor -> skip
        if getattr(orig, "_wherewent_wrapped", False):
            return  # idempotent: already wrapped

        @functools.wraps(orig)
        async def wrapper(self_, *args, **kwargs):
            tok = callsite.set_async_site()
            try:
                return await orig(self_, *args, **kwargs)
            finally:
                callsite.reset_async_site(tok)

        wrapper._wherewent_wrapped = True
        setattr(cls, name, wrapper)

    def _install_signal_handlers(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                self._prev_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, self._handle_signal)
            except Exception:
                pass
        # A4: SIGUSR1 -> mid-run PARTIAL SNAPSHOT (does not finalize/exit).
        if hasattr(signal, "SIGUSR1"):
            try:
                signal.signal(signal.SIGUSR1, self._handle_peek_signal)
            except Exception:
                pass

    def _handle_signal(self, signum, frame):
        # Ctrl-C mid-job MUST still produce the report.
        self.finalize()
        try:
            signal.signal(signum, signal.SIG_DFL)
        except Exception:
            pass
        sys.exit(130 if signum == signal.SIGINT else 143)

    def _handle_peek_signal(self, signum, frame):
        # A4: print a partial snapshot and RETURN — the job keeps running.
        try:
            self.peek(reason="SIGUSR1")
        except Exception:
            pass

    def _start_interval_thread(self):
        # A4: optional periodic PARTIAL SNAPSHOTs via WHEREWENT_INTERVAL seconds.
        try:
            raw = os.environ.get("WHEREWENT_INTERVAL")
            if not raw:
                return
            interval = float(raw)
            if interval <= 0:
                return
        except Exception:
            return

        def _loop():
            while not self.finalized:
                try:
                    time.sleep(interval)
                except Exception:
                    return
                if self.finalized:
                    return
                try:
                    self.peek(reason="interval")
                except Exception:
                    pass

        try:
            threading.Thread(target=_loop, daemon=True).start()
        except Exception:
            pass

    # -- statement hooks -------------------------------------------------------

    def _before(self, **kw):
        t0 = perf_counter()
        try:
            ctx = kw.get("context")
            if ctx is not None:
                self._exec_start[id(ctx)] = perf_counter()
        except Exception:
            pass
        finally:
            self.overhead_time += perf_counter() - t0

    def _after(self, **kw):
        t0 = perf_counter()
        try:
            ctx = kw.get("context")
            statement = kw.get("statement") or ""
            cursor = kw.get("cursor")
            executemany = bool(kw.get("executemany"))

            start = self._exec_start.pop(id(ctx), None) if ctx is not None else None
            duration = (perf_counter() - start) if start is not None else 0.0

            self.sqlalchemy_active = True
            self.total_queries += 1
            self.db_time += duration

            key, normalized = self._normalize_cached(statement)
            g = self.groups.get(key)
            if g is None:
                g = {
                    "normalized_sql": normalized,
                    "calls": 0,
                    "durations": [],
                    "total_time": 0.0,
                    "rows": 0,
                    "executemany_calls": 0,
                    "call_sites": {},        # (file,line,func) -> count
                    "sample_stacks": [],
                }
                self.groups[key] = g

            g["calls"] += 1
            g["durations"].append(duration)
            g["total_time"] += duration
            if executemany:
                g["executemany_calls"] += 1

            try:
                rc = cursor.rowcount if cursor is not None else None
                if rc is not None and rc >= 0:
                    g["rows"] += rc
                    self.total_rows += rc
            except Exception:
                pass

            # A1: prefer the async-stamped site (the sync frame walk finds no
            # user frames inside a greenlet); only fall back to the frame walk
            # when it is None, so sync overhead is unchanged.
            site = get_async_site()
            if site is None:
                site = resolve_call_site(skip=1)
            if site is not None:
                g["call_sites"][site] = g["call_sites"].get(site, 0) + 1

            # full stack capture only for the first 5 executions per group
            if g["calls"] <= 5:
                g["sample_stacks"].append(capture_stack())
        except Exception:
            pass
        finally:
            self.overhead_time += perf_counter() - t0

    # -- transaction hooks -----------------------------------------------------

    def _on_begin(self, conn):
        t0 = perf_counter()
        try:
            self._txn_gen[id(conn)] = self._txn_gen.get(id(conn), 0) + 1
        except Exception:
            pass
        finally:
            self.overhead_time += perf_counter() - t0

    def _on_commit(self, conn):
        t0 = perf_counter()
        try:
            self.total_commits += 1
            self._txn_gen[id(conn)] = self._txn_gen.get(id(conn), 0) + 1
        except Exception:
            pass
        finally:
            self.overhead_time += perf_counter() - t0

    def _on_rollback(self, conn):
        t0 = perf_counter()
        try:
            self.total_rollbacks += 1
            self._txn_gen[id(conn)] = self._txn_gen.get(id(conn), 0) + 1
        except Exception:
            pass
        finally:
            self.overhead_time += perf_counter() - t0

    def _on_connect(self, conn, *args):
        # engine_connect fires with the Connection positionally (2.0: (conn);
        # older: (conn, branch)). There is no after-commit event, so wrap the
        # dialect's do_commit / do_rollback with a perf_counter timer here.
        # Idempotent per dialect.
        t0 = perf_counter()
        try:
            dialect = getattr(conn, "dialect", None)
            if dialect is not None and not getattr(dialect, "_wherewent_wrapped", False):
                self._wrap_dialect(dialect)
        except Exception:
            pass
        finally:
            self.overhead_time += perf_counter() - t0

    def _wrap_dialect(self, dialect):
        rec = self
        orig_commit = dialect.do_commit
        orig_rollback = dialect.do_rollback

        def timed_commit(dbapi_connection, _orig=orig_commit):
            t = perf_counter()
            try:
                return _orig(dbapi_connection)
            finally:
                try:
                    rec.commit_time += perf_counter() - t
                except Exception:
                    pass

        def timed_rollback(dbapi_connection, _orig=orig_rollback):
            t = perf_counter()
            try:
                return _orig(dbapi_connection)
            finally:
                try:
                    rec.commit_time += perf_counter() - t
                except Exception:
                    pass

        dialect.do_commit = timed_commit
        dialect.do_rollback = timed_rollback
        dialect._wherewent_wrapped = True
        self.commit_measurable = True

    # -- normalization cache ---------------------------------------------------

    def _normalize_cached(self, statement):
        cached = self._norm_cache.get(statement)
        if cached is not None:
            return cached
        normalized = normalize_sql(statement)
        key = group_key(normalized)
        if len(self._norm_cache) >= _NORM_CACHE_MAX:
            self._norm_cache.clear()   # bounded: on overflow just drop everything
        self._norm_cache[statement] = (key, normalized)
        return key, normalized

    # -- snapshot / finalize ---------------------------------------------------

    def snapshot(self) -> RunSnapshot:
        wall = (perf_counter() - self.start_perf) if self.start_perf else 0.0

        cpu = None
        if self._start_times is not None:
            try:
                now = os.times()
                cpu = (now.user - self._start_times.user) + (
                    now.system - self._start_times.system
                )
            except Exception:
                cpu = None

        groups = []
        for key, g in self.groups.items():
            durations = g["durations"]
            median = statistics.median(durations) if durations else 0.0
            site = None
            if g["call_sites"]:
                site = max(g["call_sites"].items(), key=lambda kv: kv[1])[0]
            groups.append(
                GroupSnapshot(
                    key=key,
                    normalized_sql=g["normalized_sql"],
                    calls=g["calls"],
                    total_time=g["total_time"],
                    median=median,
                    rows=g["rows"],
                    executemany_calls=g["executemany_calls"],
                    call_site=site,
                )
            )

        commit_time = self.commit_time if self.commit_measurable else None

        return RunSnapshot(
            wall_time=wall,
            cpu_time=cpu,
            total_queries=self.total_queries,
            total_commits=self.total_commits,
            total_rollbacks=self.total_rollbacks,
            commit_time=commit_time,
            total_rows=self.total_rows,
            db_time=self.db_time,
            overhead_time=self.overhead_time,
            sqlalchemy_active=self.sqlalchemy_active,
            groups=groups,
        )

    def peek(self, reason="signal"):
        """A4: render the CURRENT state to stderr without finalizing/exiting.

        Read-only (snapshot() only reads the accumulators) and fully guarded so a
        mid-run trigger can never crash the host job. Does NOT set self.finalized,
        so the atexit/signal final report still fires later.
        """
        try:
            run = self.snapshot()
            findings = rules.evaluate(run)
            text = report.render(run, findings)
            banner = "····· wherewent PARTIAL SNAPSHOT (job still running) ·····"
            out = (
                "\n" + banner + f"  [reason={reason}]\n"
                + text + "\n"
                + "····· end PARTIAL SNAPSHOT — job continues ·····\n"
            )
            sys.stderr.write(out)
            sys.stderr.flush()
        except Exception:
            pass

    def finalize(self):
        if self.finalized:
            return
        self.finalized = True
        try:
            run = self.snapshot()
            findings = rules.evaluate(run)
            text = report.render(run, findings)
            sys.stderr.write(text + "\n")
            sys.stderr.flush()
            if self.save_path:
                self._write_json(run, findings)
        except Exception:
            pass

    def _write_json(self, run: RunSnapshot, findings):
        # groups in the snapshot are sorted for the report; for JSON we walk the
        # raw accumulators so we can attach the captured sample stacks too.
        group_by_key = {g.key: g for g in run.groups}
        groups_json = []
        for key, gs in group_by_key.items():
            raw = self.groups.get(key, {})
            groups_json.append(
                {
                    "key": gs.key,
                    "normalized_sql": gs.normalized_sql,
                    "calls": gs.calls,
                    "total_time": gs.total_time,
                    "median": gs.median,
                    "rows": gs.rows,
                    "executemany_calls": gs.executemany_calls,
                    "call_site": list(gs.call_site) if gs.call_site else None,
                    "sample_stacks": raw.get("sample_stacks", []),
                }
            )

        payload = {
            "wall_time": run.wall_time,
            "cpu_time": run.cpu_time,
            "total_queries": run.total_queries,
            "total_commits": run.total_commits,
            "total_rollbacks": run.total_rollbacks,
            "commit_time": run.commit_time,
            "total_rows": run.total_rows,
            "db_time": run.db_time,
            "overhead_time": run.overhead_time,
            "sqlalchemy_active": run.sqlalchemy_active,
            "groups": groups_json,
            "findings": [
                {"rule": f.rule, "title": f.title, "detail": f.detail, "seconds": f.seconds}
                for f in findings
            ],
        }
        try:
            with open(self.save_path, "w") as fh:
                json.dump(payload, fh, indent=2)
        except Exception:
            pass


# module-level singleton
recorder = Recorder()


def install_from_env() -> None:
    """Entry point called by the shim: install using WHEREWENT_SAVE from env."""
    save = os.environ.get("WHEREWENT_SAVE") or None
    recorder.install(save)

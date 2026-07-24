"""The recorder: class-level SQLAlchemy event hooks + finalize/report.

Design invariants (see build contract):
  * Never crash the host job: every hook body is wrapped in try/except.
  * Never store parameter VALUES — only counts, durations, and normalized SQL.
  * Cheap hot path: memoized library detection, bounded normalization cache,
    full stack capture only for the first 5 samples of each group.
  * finalize() prints exactly once, to stderr, via atexit AND a signal handler.
"""

import atexit
import collections
import contextvars
import functools
import importlib.abc
import importlib.util
import inspect
import json
import os
import random
import signal
import statistics
import sys
import sysconfig
import threading
import time
from time import perf_counter

from . import callsite, report, rules
from .callsite import capture_stack, get_async_site, resolve_call_site
from .normalize import group_key, normalize_sql
from .stats import GroupSnapshot, RunSnapshot, UnitStats

_NORM_CACHE_MAX = 4096
# D1: bounded reservoir cap for per-GROUP duration medians (matches the
# _UnitAccumulator cap). total_time stays exact; only the median is sampled.
_DUR_RESERVOIR_CAP = 5000

# ---------------------------------------------------------------------------
# Unit-aware profiling (v0.3, --unit-function / wherewent.unit()).
#
# Two module-level ContextVars carry per-unit state. They are only ever TOUCHED
# on the query hot path behind `if self._unit_enabled:`, so a run without
# --unit-function/unit() pays zero extra cost (the v0.2 hot path is unchanged).
# ---------------------------------------------------------------------------

# The current unit's mutable record dict (or None when not inside a unit).
_current_unit = contextvars.ContextVar("wherewent_current_unit", default=None)
# Recursion depth: only the OUTERMOST wrapped call / context is a unit.
_unit_depth = contextvars.ContextVar("wherewent_unit_depth", default=0)


class _UnitAccumulator:
    """Bounded-memory aggregator: never stores every unit (jobs may do millions).

    Keeps exact running sums, a reservoir sample (cap 5000) for the medians, a
    FIRST window (first 100 units) and a LAST window (deque of the last 100),
    all bounded.
    """

    W = 100          # first-window size
    CAP = 5000       # reservoir cap

    def __init__(self):
        self.count = 0
        self.sum_duration = 0.0
        self.sum_queries = 0
        self.sum_commits = 0
        self.sum_rollbacks = 0
        self.sum_rows = 0
        # reservoir samples (paired so a sampled unit contributes both figures)
        self._res_dur = []
        self._res_q = []
        self._seen = 0
        # FIRST window: count + sum_duration (+ sum_queries for the D4 slope)
        self.first_n = 0
        self.first_sum_duration = 0.0
        self.first_sum_queries = 0
        # LAST window: bounded rings of recent durations and query-counts
        self.last_durations = collections.deque(maxlen=self.W)
        self.last_queries = collections.deque(maxlen=self.W)

    def add(self, duration, queries, commits, rollbacks, rows):
        self.count += 1
        self.sum_duration += duration
        self.sum_queries += queries
        self.sum_commits += commits
        self.sum_rollbacks += rollbacks
        self.sum_rows += rows

        # reservoir sampling (algorithm R): exact while under the cap
        self._seen += 1
        if len(self._res_dur) < self.CAP:
            self._res_dur.append(duration)
            self._res_q.append(queries)
        else:
            j = random.randint(0, self._seen - 1)
            if j < self.CAP:
                self._res_dur[j] = duration
                self._res_q[j] = queries

        if self.first_n < self.W:
            self.first_n += 1
            self.first_sum_duration += duration
            self.first_sum_queries += queries

        self.last_durations.append(duration)
        self.last_queries.append(queries)


class _WrapLoader(importlib.abc.Loader):
    """Delegates to the real loader, then runs a patch callback on the module."""

    def __init__(self, inner, patch):
        self.inner = inner
        self.patch = patch

    def create_module(self, spec):
        return self.inner.create_module(spec)

    def exec_module(self, module):
        self.inner.exec_module(module)
        try:
            self.patch(module)
        except Exception:
            pass


class _PostImportPatcher(importlib.abc.MetaPathFinder):
    """meta_path finder: patches a specific module right after it is executed."""

    def __init__(self, target_module, patch):
        self.target_module = target_module
        self.patch = patch
        self._busy = False

    def find_spec(self, name, path, target=None):
        if name != self.target_module or self._busy:
            return None
        self._busy = True
        try:
            spec = importlib.util.find_spec(name)   # real spec, via other finders
        except Exception:
            spec = None
        finally:
            self._busy = False
        if spec is None or spec.loader is None:
            return None
        spec.loader = _WrapLoader(spec.loader, self.patch)
        return spec


class _BareNamePatcher(importlib.abc.MetaPathFinder):
    """meta_path finder for a bare function name: scans each user module that
    finishes importing and patches the first top-level callable that matches."""

    def __init__(self, patch, done):
        self.patch = patch
        self._done = done      # callable() -> bool, so we stop once wrapped
        self._busy = False

    def find_spec(self, name, path, target=None):
        if self._busy or self._done():
            return None
        self._busy = True
        try:
            spec = importlib.util.find_spec(name)
        except Exception:
            spec = None
        finally:
            self._busy = False
        if spec is None or spec.loader is None:
            return None
        if not _is_user_origin(getattr(spec, "origin", None)):
            return None
        spec.loader = _WrapLoader(spec.loader, self.patch)
        return spec


def _is_user_origin(origin):
    """Best-effort: True for a user source file, False for stdlib/site-packages."""
    if not origin or not isinstance(origin, str):
        return False
    if origin in ("built-in", "frozen", "namespace"):
        return False
    if "site-packages" in origin or "dist-packages" in origin:
        return False
    try:
        stdlib = sysconfig.get_paths().get("stdlib", "")
        platlib = sysconfig.get_paths().get("platstdlib", "")
        if stdlib and origin.startswith(stdlib):
            return False
        if platlib and origin.startswith(platlib):
            return False
    except Exception:
        pass
    return True


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
        # D2: rollback time is split out of commit_time. Both become measurable
        # together (same dialect wrap), so rollback mirrors commit_measurable.
        self.rollback_time = 0.0
        self.rollback_measurable = False

        self.sqlalchemy_active = False
        self._prev_handlers = {}
        self._listeners = []   # D10: (identifier, fn) pairs registered on Engine
        self._wrapped_dialects = []  # D10: dialects whose commit/rollback we wrapped
        self._meta_finders = []      # D10: sys.meta_path finders we inserted

        # -- unit-aware profiling (v0.3) --------------------------------------
        # All of this stays dormant (and the hot path unchanged) unless unit
        # mode is enabled via --unit-function (WHEREWENT_UNIT_FUNCTION) or a
        # first use of wherewent.unit().
        self._unit_enabled = False        # THE gate for the query hot path
        self._unit_spec = None            # SPEC string from --unit-function
        self._unit_name = None            # first name seen via unit() context mgr
        self._unit_wrapped = False        # True once a target was actually patched
        self._unit_setup_done = False     # guard against double-wiring the wrapper
        self._unit_acc = None             # _UnitAccumulator (lazily created)
        self._unit_module = None          # module part of SPEC
        self._unit_attr_path = None       # attr path: "func" or "Class.method"
        self._unit_bare = False           # SPEC was a bare name (no module)
        self._current_unit = _current_unit   # hot-path reads self._current_unit

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
            # D3: a failed execute never fires after_cursor_execute, so its
            # _exec_start entry would leak. handle_error pops it (best-effort).
            event.listen(Engine, "handle_error", self._on_error)
            # D10: remember what we registered so disable() can remove it.
            self._listeners = [
                ("before_cursor_execute", self._before),
                ("after_cursor_execute", self._after),
                ("begin", self._on_begin),
                ("commit", self._on_commit),
                ("rollback", self._on_rollback),
                ("engine_connect", self._on_connect),
                ("handle_error", self._on_error),
            ]
            self.sqlalchemy_active = True
        except Exception:
            self.sqlalchemy_active = False

        # A1: async call-site attribution — wrap the AsyncSession/AsyncConnection
        # user-entry coroutine methods so they stamp the call-site contextvar.
        self._install_async_wrappers()

        # v0.3: wire the --unit-function wrapper (if a SPEC was supplied). This
        # is fully guarded — an unresolvable target leaves the job running.
        if self._unit_enabled and self._unit_spec and not self._unit_setup_done:
            try:
                self._setup_unit_wrapping()
            except Exception:
                pass

        atexit.register(self.finalize)
        self._install_signal_handlers()
        self._start_interval_thread()

    def disable(self):
        """D10: best-effort teardown of the process-global hooks this recorder
        installed. Fully guarded — never raises. Does NOT reset accumulated
        counters (a caller may still want snapshot()). Idempotent.

        Removes the Engine class-level event listeners and restores any dialect
        commit/rollback wrappers we own. Existing behavior is unchanged when
        disable() is never called.
        """
        try:
            self._unit_enabled = False
            self.installed = False
        except Exception:
            pass
        # D10: drop the atexit report. Without this, a recorder that ever
        # install()ed still prints its full report to stderr at interpreter
        # shutdown even after disable() — surprising output from a recorder the
        # embedder explicitly turned off. unregister() is a no-op if we never
        # registered, so this stays idempotent.
        try:
            atexit.unregister(self.finalize)
        except Exception:
            pass
        # remove the SQLAlchemy event listeners we registered
        try:
            from sqlalchemy import event
            from sqlalchemy.engine import Engine
            for identifier, fn in list(getattr(self, "_listeners", [])):
                try:
                    event.remove(Engine, identifier, fn)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            self._listeners = []
        # restore wrapped dialect commit/rollback if feasible (idempotent)
        for dialect in list(getattr(self, "_wrapped_dialects", [])):
            try:
                orig_commit = getattr(dialect, "_wherewent_orig_commit", None)
                orig_rollback = getattr(dialect, "_wherewent_orig_rollback", None)
                if orig_commit is not None:
                    dialect.do_commit = orig_commit
                if orig_rollback is not None:
                    dialect.do_rollback = orig_rollback
                try:
                    dialect._wherewent_wrapped = False
                except Exception:
                    pass
            except Exception:
                pass
        try:
            self._wrapped_dialects = []
        except Exception:
            pass
        # drop any import hooks we inserted: a live finder would keep wrapping
        # unit targets in modules imported after disable() (cross-test leak).
        for finder in list(getattr(self, "_meta_finders", [])):
            try:
                sys.meta_path.remove(finder)
            except Exception:
                pass
        try:
            self._meta_finders = []
        except Exception:
            pass

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

            # v0.3 per-unit attribution. When unit mode is DISABLED this is a
            # single bool check evaluating to None — no contextvar touch, so the
            # v0.2 hot path is unchanged. When enabled, `u` is the current unit
            # record (or None outside any unit) and we count this query + rows.
            u = self._current_unit.get() if self._unit_enabled else None
            if u is not None:
                u["queries"] += 1

            key, normalized = self._normalize_cached(statement)
            g = self.groups.get(key)
            if g is None:
                g = {
                    "normalized_sql": normalized,
                    "calls": 0,
                    # D1: bounded reservoir for the MEDIAN only (cap 5000). The
                    # count (`calls`) and `total_time` stay EXACT.
                    "dur_reservoir": [],
                    "dur_seen": 0,
                    "total_time": 0.0,
                    "rows": 0,
                    "executemany_calls": 0,
                    "call_sites": {},        # (file,line,func) -> count
                    "sample_stacks": [],
                }
                self.groups[key] = g

            g["calls"] += 1
            g["total_time"] += duration
            # D1: reservoir sampling (algorithm R) — exact under the cap, then
            # random replacement above it, so per-group memory is O(cap).
            g["dur_seen"] += 1
            res = g["dur_reservoir"]
            if len(res) < _DUR_RESERVOIR_CAP:
                res.append(duration)
            else:
                j = random.randint(0, g["dur_seen"] - 1)
                if j < _DUR_RESERVOIR_CAP:
                    res[j] = duration
            if executemany:
                g["executemany_calls"] += 1

            try:
                rc = cursor.rowcount if cursor is not None else None
                if rc is not None and rc >= 0:
                    g["rows"] += rc
                    self.total_rows += rc
                    if u is not None:
                        u["rows"] += rc
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

    def _on_error(self, exception_context):
        # D3: a failed execute skips after_cursor_execute, leaking _exec_start.
        # Clean up the entry for the failing context. Do NOT touch the
        # exception — SQLAlchemy re-raises; we just return None.
        t0 = perf_counter()
        try:
            ctx = getattr(exception_context, "execution_context", None)
            if ctx is not None:
                self._exec_start.pop(id(ctx), None)
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
            if self._unit_enabled:
                u = self._current_unit.get()
                if u is not None:
                    u["commits"] += 1
        except Exception:
            pass
        finally:
            self.overhead_time += perf_counter() - t0

    def _on_rollback(self, conn):
        t0 = perf_counter()
        try:
            self.total_rollbacks += 1
            self._txn_gen[id(conn)] = self._txn_gen.get(id(conn), 0) + 1
            if self._unit_enabled:
                u = self._current_unit.get()
                if u is not None:
                    u["rollbacks"] += 1
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
                    # D2: rollback time is its OWN bucket, not commit_time. The
                    # pool fires do_rollback as a reset on checkin, so this
                    # includes pool resets (report.py labels it accordingly).
                    rec.rollback_time += perf_counter() - t
                except Exception:
                    pass

        dialect.do_commit = timed_commit
        dialect.do_rollback = timed_rollback
        dialect._wherewent_wrapped = True
        # D10: stash originals so disable() can restore this dialect.
        try:
            dialect._wherewent_orig_commit = orig_commit
            dialect._wherewent_orig_rollback = orig_rollback
            self._wrapped_dialects.append(dialect)
        except Exception:
            pass
        self.commit_measurable = True
        # D2: the SAME wrap makes both timers real, so they flip together.
        self.rollback_measurable = True

    # -- unit-aware profiling (v0.3) -------------------------------------------

    def enable_unit(self, spec):
        """Enable unit mode from a --unit-function SPEC (WHEREWENT_UNIT_FUNCTION).

        Idempotent; safe before or after install(). Fully guarded so a bad SPEC
        never affects the host job.
        """
        try:
            self._unit_spec = spec or None
            self._unit_enabled = True
            if self._unit_acc is None:
                self._unit_acc = _UnitAccumulator()
            if self.installed and self._unit_spec and not self._unit_setup_done:
                self._setup_unit_wrapping()
        except Exception:
            pass

    def _ensure_unit_mode(self, name):
        """Lazily enable unit mode from a wherewent.unit() context manager use.

        No SPEC / no target wrapping — the context manager IS the entry point.
        """
        if self._unit_acc is None:
            self._unit_acc = _UnitAccumulator()
        self._unit_enabled = True
        if self._unit_spec is None and self._unit_name is None:
            self._unit_name = name

    def _unit_note(self, what):
        # Honesty: tell the user (stderr) exactly what got wrapped as a unit.
        try:
            sys.stderr.write(f"wherewent: wrapping unit function -> {what}\n")
            sys.stderr.flush()
        except Exception:
            pass

    def _setup_unit_wrapping(self):
        """Parse the SPEC and arrange for the target callable to be wrapped.

        `pkg.mod:func` / `pkg.mod:Class.method`: patch immediately if the module
        is already imported (incl. __main__), else install a post-import finder.
        Bare `func`: scan already-loaded user modules, and install a finder for
        modules that load later. All guarded — an unresolvable target simply
        leaves `wrapped=False`.
        """
        self._unit_setup_done = True
        spec = self._unit_spec or ""
        if ":" in spec:
            mod, attr = spec.split(":", 1)
            self._unit_module = mod.strip()
            self._unit_attr_path = attr.strip()
            self._unit_bare = False
            existing = sys.modules.get(self._unit_module)
            if existing is not None:
                self._patch_module_attr(existing)
            if not self._unit_wrapped:
                try:
                    finder = _PostImportPatcher(
                        self._unit_module, self._patch_module_attr
                    )
                    sys.meta_path.insert(0, finder)
                    self._meta_finders.append(finder)   # D10: so disable() can drop it
                except Exception:
                    pass
        else:
            self._unit_bare = True
            self._unit_attr_path = spec.strip()
            self._unit_module = None
            # scan already-loaded user modules first (incl. __main__ if present)
            for module in list(sys.modules.values()):
                if self._unit_wrapped:
                    break
                try:
                    self._patch_bare(module)
                except Exception:
                    pass
            if not self._unit_wrapped:
                try:
                    finder = _BareNamePatcher(
                        self._patch_bare, lambda: self._unit_wrapped
                    )
                    sys.meta_path.insert(0, finder)
                    self._meta_finders.append(finder)   # D10: so disable() can drop it
                except Exception:
                    pass

    def _patch_module_attr(self, module):
        """Wrap the SPEC's `func` or `Class.method` on an imported module."""
        if self._unit_wrapped:
            return
        try:
            parts = (self._unit_attr_path or "").split(".")
            if len(parts) == 1:
                name = parts[0]
                orig = getattr(module, name, None)
                if orig is None or not callable(orig):
                    return
                wrapped = self._make_unit_wrapper(orig)
                if wrapped is not orig:
                    setattr(module, name, wrapped)
            elif len(parts) == 2:
                cls = getattr(module, parts[0], None)
                if cls is None:
                    return
                orig = getattr(cls, parts[1], None)
                if orig is None or not callable(orig):
                    return
                wrapped = self._make_unit_wrapper(orig)
                if wrapped is not orig:
                    setattr(cls, parts[1], wrapped)
            else:
                return
            self._unit_wrapped = True
            self._unit_note(self._unit_spec)
        except Exception:
            pass

    def _patch_bare(self, module):
        """Best-effort: wrap the first top-level callable in `module` whose
        __name__ matches the bare SPEC and which was defined in that module."""
        if self._unit_wrapped:
            return
        try:
            mod_name = getattr(module, "__name__", None)
            origin = getattr(module, "__file__", None)
            if mod_name != "__main__" and not _is_user_origin(origin):
                return
            name = self._unit_attr_path
            obj = getattr(module, "__dict__", {}).get(name)
            if obj is None or not callable(obj):
                return
            if getattr(obj, "__module__", None) != mod_name:
                return
            wrapped = self._make_unit_wrapper(obj)
            if wrapped is not obj:
                setattr(module, name, wrapped)
            self._unit_wrapped = True
            self._unit_note(f"{name} (in {mod_name})")
        except Exception:
            pass

    def _make_unit_wrapper(self, orig):
        """Return a wrapper making each OUTERMOST call one unit. Handles sync and
        coroutine callables; idempotent via a `_wherewent_unit_wrapped` marker."""
        if getattr(orig, "_wherewent_unit_wrapped", False):
            return orig
        rec = self

        @functools.wraps(orig)
        def wrapper(*args, **kwargs):
            try:
                depth = _unit_depth.get()
            except Exception:
                depth = 0
            if depth > 0:
                # nested self-call: NOT a new unit (outermost wins)
                return orig(*args, **kwargs)
            rec_dict, dtok, utok = rec._unit_begin()
            try:
                result = orig(*args, **kwargs)
            except BaseException:
                rec._unit_end(rec_dict, dtok, utok)
                raise
            if inspect.isawaitable(result):
                # coroutine/awaitable: account after it is awaited
                return rec._unit_await(result, rec_dict, dtok, utok)
            rec._unit_end(rec_dict, dtok, utok)
            return result

        wrapper._wherewent_unit_wrapped = True
        return wrapper

    def _unit_begin(self):
        rec_dict = {
            "queries": 0, "commits": 0, "rollbacks": 0, "rows": 0,
            "t0": perf_counter(),
        }
        dtok = _unit_depth.set(1)
        utok = _current_unit.set(rec_dict)
        return rec_dict, dtok, utok

    def _unit_end(self, rec_dict, dtok, utok):
        try:
            duration = perf_counter() - rec_dict["t0"]
            if self._unit_acc is not None:
                self._unit_acc.add(
                    duration,
                    rec_dict["queries"], rec_dict["commits"],
                    rec_dict["rollbacks"], rec_dict["rows"],
                )
        except Exception:
            pass
        finally:
            try:
                _current_unit.reset(utok)
            except Exception:
                pass
            try:
                _unit_depth.reset(dtok)
            except Exception:
                pass

    async def _unit_await(self, awaitable, rec_dict, dtok, utok):
        try:
            return await awaitable
        finally:
            self._unit_end(rec_dict, dtok, utok)

    def _build_unit_stats(self):
        """Build a UnitStats from the accumulator. Even at count==0 we report
        (so an unwrapped / never-called target is honest about `wrapped`)."""
        name = self._unit_spec or self._unit_name or "unit"
        acc = self._unit_acc
        # `wrapped` is honest: a target was patched, OR units actually ran (the
        # context-manager path has no target but clearly captured units).
        wrapped = bool(self._unit_wrapped) or (acc is not None and acc.count > 0)

        if acc is None or acc.count == 0:
            return UnitStats(
                name=name, wrapped=wrapped, count=0,
                median_duration=0.0, mean_duration=0.0,
                median_queries=0.0, mean_queries=0.0,
                mean_commits=0.0, mean_rollbacks=0.0, mean_rows=0.0,
                first_window_n=0, first_window_mean_duration=None,
                last_window_n=0, last_window_mean_duration=None,
                first_window_mean_queries=None,
                last_window_mean_queries=None,
            )

        n = acc.count
        median_dur = statistics.median(acc._res_dur) if acc._res_dur else 0.0
        median_q = statistics.median(acc._res_q) if acc._res_q else 0.0
        first_mean = (acc.first_sum_duration / acc.first_n) if acc.first_n else None
        last_mean = (
            (sum(acc.last_durations) / len(acc.last_durations))
            if acc.last_durations else None
        )
        # D4: query-slope windows (enables R6 queries/unit slope).
        first_mean_q = (acc.first_sum_queries / acc.first_n) if acc.first_n else None
        last_mean_q = (
            (sum(acc.last_queries) / len(acc.last_queries))
            if acc.last_queries else None
        )
        return UnitStats(
            name=name, wrapped=wrapped, count=n,
            median_duration=median_dur,
            mean_duration=acc.sum_duration / n,
            median_queries=median_q,
            mean_queries=acc.sum_queries / n,
            mean_commits=acc.sum_commits / n,
            mean_rollbacks=acc.sum_rollbacks / n,
            mean_rows=acc.sum_rows / n,
            first_window_n=acc.first_n,
            first_window_mean_duration=first_mean,
            last_window_n=len(acc.last_durations),
            last_window_mean_duration=last_mean,
            first_window_mean_queries=first_mean_q,
            last_window_mean_queries=last_mean_q,
        )

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

    @staticmethod
    def _duration_samples(g):
        """Return a group's duration samples, whatever shape the group dict is.

        D1 replaced the unbounded per-group `durations` list with a bounded
        `dur_reservoir`. This reader is deliberately shape-tolerant: a group
        dict produced by an older recorder, a restored --save payload, or any
        other code path that still carries the legacy `durations` key must
        still render. A READER of our own accumulator must never be able to
        raise KeyError and take the whole report down with it.
        """
        try:
            res = g.get("dur_reservoir")
            if res:
                return res
            legacy = g.get("durations")      # pre-v0.3 unbounded list
            if legacy:
                return legacy
        except Exception:
            pass
        return ()

    def _emit_internal_error(self, where, exc):
        """Honesty invariant: an internal failure is never silent — but it also
        never propagates into the host job.

        A blanket `except: pass` around report rendering turns a real bug into
        an empty stderr, which reads to the user as "wherewent had nothing to
        say". That is a lie. Degrade to a one-line, clearly-marked diagnostic
        instead, then return normally.
        """
        try:
            sys.stderr.write(
                f"wherewent: internal error in {where} "
                f"({type(exc).__name__}: {exc}) — no report could be rendered "
                "for this call. Recording continues; your job is unaffected.\n"
            )
            sys.stderr.flush()
        except Exception:
            pass

    def _snapshot_groups(self):
        """Build the GroupSnapshot list defensively — every field is read with
        .get() so a partially-populated group dict degrades instead of raising."""
        groups = []
        for key, g in list(self.groups.items()):
            try:
                # D1: sample median from the bounded reservoir (total_time is
                # exact); falls back to a legacy `durations` list if present.
                samples = self._duration_samples(g)
                median = statistics.median(samples) if samples else 0.0
                sites = g.get("call_sites") or {}
                site = max(sites.items(), key=lambda kv: kv[1])[0] if sites else None
                groups.append(
                    GroupSnapshot(
                        key=key,
                        normalized_sql=g.get("normalized_sql", ""),
                        calls=g.get("calls", 0),
                        total_time=g.get("total_time", 0.0),
                        median=median,
                        rows=g.get("rows", 0),
                        executemany_calls=g.get("executemany_calls", 0),
                        call_site=site,
                    )
                )
            except Exception:
                continue   # one malformed group must not cost the whole report
        return groups

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

        groups = self._snapshot_groups()

        commit_time = self.commit_time if self.commit_measurable else None
        # D2: rollback becomes measurable together with commit (same wrap).
        rollback_time = self.rollback_time if self.rollback_measurable else None

        unit_stats = self._build_unit_stats() if self._unit_enabled else None

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
            unit_stats=unit_stats,
            rollback_time=rollback_time,
        )

    def peek(self, reason="signal"):
        """A4: render the CURRENT state to stderr without finalizing/exiting.

        Read-only (snapshot() only reads the accumulators) and fully guarded so a
        mid-run trigger can never crash the host job. Does NOT set self.finalized,
        so the atexit/signal final report still fires later.

        If rendering itself fails, we say so on stderr rather than printing
        nothing — a silent peek is indistinguishable from a broken install.
        """
        try:
            run = self.snapshot()
            findings = rules.evaluate(run)
            text = report.render(run, findings)
        except Exception as exc:
            self._emit_internal_error(f"peek(reason={reason})", exc)
            return
        try:
            banner = "····· wherewent PARTIAL SNAPSHOT (job still running) ·····"
            out = (
                "\n" + banner + f"  [reason={reason}]\n"
                + text + "\n"
                + "····· end PARTIAL SNAPSHOT — job continues ·····\n"
            )
            sys.stderr.write(out)
            sys.stderr.flush()
        except Exception as exc:
            self._emit_internal_error(f"peek(reason={reason})", exc)

    def finalize(self):
        if self.finalized:
            return
        self.finalized = True
        run = findings = None
        try:
            run = self.snapshot()
            findings = rules.evaluate(run)
            text = report.render(run, findings)
            sys.stderr.write(text + "\n")
            sys.stderr.flush()
        except Exception as exc:
            # Same honesty rule as peek(): never print nothing at all.
            self._emit_internal_error("finalize()", exc)
        try:
            if self.save_path and run is not None:
                self._write_json(run, findings or [])
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
            "rollback_time": run.rollback_time,
            "total_rows": run.total_rows,
            "db_time": run.db_time,
            "overhead_time": run.overhead_time,
            "sqlalchemy_active": run.sqlalchemy_active,
            "groups": groups_json,
            "findings": [
                {"rule": f.rule, "title": f.title, "detail": f.detail, "seconds": f.seconds}
                for f in findings
            ],
            "unit_stats": _unit_stats_to_json(run.unit_stats),
        }
        try:
            with open(self.save_path, "w") as fh:
                json.dump(payload, fh, indent=2)
        except Exception:
            pass


def _unit_stats_to_json(us):
    """Serialize a UnitStats (or None) for the --save payload."""
    if us is None:
        return None
    return {
        "name": us.name,
        "wrapped": us.wrapped,
        "count": us.count,
        "median_duration": us.median_duration,
        "mean_duration": us.mean_duration,
        "median_queries": us.median_queries,
        "mean_queries": us.mean_queries,
        "mean_commits": us.mean_commits,
        "mean_rollbacks": us.mean_rollbacks,
        "mean_rows": us.mean_rows,
        "first_window_n": us.first_window_n,
        "first_window_mean_duration": us.first_window_mean_duration,
        "last_window_n": us.last_window_n,
        "last_window_mean_duration": us.last_window_mean_duration,
        "first_window_mean_queries": us.first_window_mean_queries,
        "last_window_mean_queries": us.last_window_mean_queries,
    }


# module-level singleton
recorder = Recorder()


class unit:
    """Context manager marking one unit of work — the in-code twin of
    --unit-function. Shares the SAME contextvar/accumulator/recursion guard.

    Usage::

        with wherewent.unit("receivable"):
            process(receivable)

    Behavior:
      * Records duration + per-unit query/commit/rollback/row counts on exit,
        EVEN IF the body raised (then re-raises — never swallows).
      * Respects the recursion-depth guard: a unit() inside a --unit-function
        unit (or a nested unit()) does NOT double-count; the outermost wins.
      * A safe **no-op that still runs the body** when the recorder is not
        installed, so code using it is production-safe without wherewent.

    Sync-only (a sync `with` inside an async loop body is sufficient for v0.3).
    """

    def __init__(self, name="unit"):
        self.name = name
        self._active = False
        self._rec_dict = None
        self._dtok = None
        self._utok = None

    def __enter__(self):
        rec = recorder
        try:
            if not rec.installed:
                return self  # no-op, but the body still runs
            try:
                depth = _unit_depth.get()
            except Exception:
                depth = 0
            if depth > 0:
                return self  # nested: outermost wins, this one is a no-op
            rec._ensure_unit_mode(self.name)
            self._rec_dict = {
                "queries": 0, "commits": 0, "rollbacks": 0, "rows": 0,
                "t0": perf_counter(),
            }
            self._dtok = _unit_depth.set(1)
            self._utok = _current_unit.set(self._rec_dict)
            self._active = True
        except Exception:
            self._active = False
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._active:
            try:
                duration = perf_counter() - self._rec_dict["t0"]
                acc = recorder._unit_acc
                if acc is not None:
                    acc.add(
                        duration,
                        self._rec_dict["queries"], self._rec_dict["commits"],
                        self._rec_dict["rollbacks"], self._rec_dict["rows"],
                    )
            except Exception:
                pass
            finally:
                try:
                    _current_unit.reset(self._utok)
                except Exception:
                    pass
                try:
                    _unit_depth.reset(self._dtok)
                except Exception:
                    pass
        return False  # never suppress the body's exception


def install_from_env() -> None:
    """Entry point called by the shim: install using WHEREWENT_SAVE from env,
    and enable unit mode from WHEREWENT_UNIT_FUNCTION when set."""
    save = os.environ.get("WHEREWENT_SAVE") or None
    spec = os.environ.get("WHEREWENT_UNIT_FUNCTION") or None
    if spec:
        recorder.enable_unit(spec)
    recorder.install(save)

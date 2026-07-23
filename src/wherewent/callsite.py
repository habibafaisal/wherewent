"""Call-site resolution — find the user-code frame that issued a query.

This runs on the hot path (once per execution), so is_library_file is memoized
and the full stack capture is reserved for the first few samples of each group.
"""

import os
import sys
import sysconfig

# memoization cache for is_library_file (keyed by raw filename string)
_LIB_CACHE: "dict[str, bool]" = {}

# directory of the installed wherewent package itself (its frames are "library")
_WHEREWENT_DIR = os.path.dirname(os.path.abspath(__file__))

# stdlib / install-tree prefixes computed once
_STDLIB_PREFIXES = set()
for _key in ("stdlib", "platstdlib", "purelib", "platlib"):
    try:
        _p = sysconfig.get_paths().get(_key)
        if _p:
            _STDLIB_PREFIXES.add(os.path.abspath(_p))
    except Exception:
        pass
for _p in (getattr(sys, "prefix", None), getattr(sys, "base_prefix", None)):
    if _p:
        _STDLIB_PREFIXES.add(os.path.abspath(_p))
_STDLIB_PREFIXES = tuple(_STDLIB_PREFIXES)


def _compute_is_library(filename: str) -> bool:
    # frozen / built-in / <string> / <stdin> pseudo-files are never user code
    if not filename or filename.startswith("<"):
        return True
    path = os.path.abspath(filename)
    sep = os.sep
    if "site-packages" in path or "dist-packages" in path:
        return True
    if sep + "sqlalchemy" + sep in path:
        return True
    if path.startswith(_WHEREWENT_DIR):
        return True
    for prefix in _STDLIB_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def is_library_file(filename: str) -> bool:
    """True if *filename* belongs to a library/stdlib/wherewent (memoized)."""
    cached = _LIB_CACHE.get(filename)
    if cached is not None:
        return cached
    result = _compute_is_library(filename)
    _LIB_CACHE[filename] = result
    return result


def _short(filename: str) -> str:
    """A compact display name: path relative to cwd, else basename."""
    try:
        rel = os.path.relpath(filename, os.getcwd())
        if not rel.startswith(".."):
            return rel
    except Exception:
        pass
    return os.path.basename(filename)


def resolve_call_site(skip: int = 1) -> "tuple[str, int, str] | None":
    """Walk up the stack and return the first user-code frame.

    Returns (short_file, lineno, function) or None if only library frames exist.
    """
    try:
        frame = sys._getframe(skip)
    except ValueError:
        return None
    while frame is not None:
        filename = frame.f_code.co_filename
        if not is_library_file(filename):
            return (_short(filename), frame.f_lineno, frame.f_code.co_name)
        frame = frame.f_back
    return None


def capture_stack(limit: int = 30) -> "list[str]":
    """Return up to *limit* frames (user + library) as 'file:line in func'."""
    frames = []
    try:
        frame = sys._getframe(1)
    except ValueError:
        return frames
    while frame is not None and len(frames) < limit:
        code = frame.f_code
        frames.append(f"{_short(code.co_filename)}:{frame.f_lineno} in {code.co_name}")
        frame = frame.f_back
    return frames

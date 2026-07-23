"""SQL normalization — turn a raw statement into a literal-free group signature.

We NEVER keep parameter values. The output is a shape string used only to group
structurally-identical queries together.
"""

import hashlib
import re

# --- pre-compiled patterns (applied in order) ---------------------------------

# 1. comments: -- to end of line, and /* ... */ blocks (dotall for multiline)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

# 2. single-quoted string literals, honoring the '' escape sequence
_STRING = re.compile(r"'(?:[^']|'')*'")

# 3. bind placeholders: %s, %(name)s, :name, $1-style (done before numbers so
#    that $1 is treated as a placeholder rather than the number 1)
_PLACEHOLDER = re.compile(r"%\(\w+\)s|%s|\$\d+|(?<![:\w]):\w+")

# 4. numeric literals: standalone numbers only, never the trailing digits of an
#    identifier like col1 (lookbehind rejects a preceding word char or dot)
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")

# 5. whitespace runs
_WS = re.compile(r"\s+")

# 6. IN (?, ?, ?)  ->  IN (?)
_IN_LIST = re.compile(r"(?i)\bIN\s*\(\s*\?(?:\s*,\s*\?)*\s*\)")

# 7. multi-row VALUES (?, ?), (?, ?), ...  ->  VALUES (?, ?)  (keep first tuple)
_MULTI_VALUES = re.compile(r"(?i)(\bVALUES\s*)(\([^()]*\))(?:\s*,\s*\([^()]*\))+")


def normalize_sql(sql: str) -> str:
    """Return a literal-free, whitespace-collapsed signature for *sql*."""
    if not sql:
        return ""
    s = sql
    s = _LINE_COMMENT.sub(" ", s)
    s = _BLOCK_COMMENT.sub(" ", s)
    s = _STRING.sub("?", s)
    s = _PLACEHOLDER.sub("?", s)
    s = _NUMBER.sub("?", s)
    s = _WS.sub(" ", s).strip()
    s = _IN_LIST.sub("IN (?)", s)
    s = _MULTI_VALUES.sub(r"\1\2", s)
    return s


def group_key(normalized: str) -> str:
    """Stable 12-hex-char group id derived from a normalized statement."""
    return hashlib.sha1(normalized.encode()).hexdigest()[:12]

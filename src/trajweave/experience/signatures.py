"""Normalize a failure/error blob into a stable, comparable signature.

Used to decide whether "the same failure" recurs across trajectories
(``repeated_failure``) and to describe a failure in a compact way. Deterministic
and documented so a given error text always maps to the same signature.
"""

from __future__ import annotations

import re

_MAX_LEN = 200

_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_HEX_RE = re.compile(r"\b0x[0-9a-f]+\b", re.I)
_PATH_RE = re.compile(r"(?:/[\w.\-]+)+/?|(?:[\w.\-]+/)+[\w.\-]+")
_WINPATH_RE = re.compile(r"[a-z]:\\[\w\\.\-]+", re.I)
_LINECOL_RE = re.compile(r"\bline \d+|:\d+:\d+|:\d+\b")
_NUM_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"\s+")

# Lines that are pure framing / noise - skip them when picking the salient line.
_SKIP_LINE_RE = re.compile(
    r"^\s*(=+|-+|_+|\*+|traceback \(most recent call last\)|during handling|"
    r"the above exception|\.\.\.)\s*$",
    re.I,
)


def error_signature(text: str | None) -> str:
    """Return a normalized one-line signature for ``text`` (may be empty).

    Steps: pick the first salient line -> lowercase -> strip paths, Windows
    paths, line/col markers, hex addresses and UUIDs -> collapse digit runs to
    ``N`` -> squeeze whitespace -> cap length.
    """

    if not text:
        return ""

    salient = ""
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line or _SKIP_LINE_RE.match(line):
            continue
        salient = line
        # Prefer a line that actually names an error over the first prose line.
        if re.search(r"error|exception|assert|failed|fail\b|E\s{2,}", line, re.I):
            break
    if not salient:
        salient = _WS_RE.sub(" ", str(text)).strip()

    s = salient.lower()
    s = _UUID_RE.sub("<uuid>", s)
    s = _WINPATH_RE.sub("<path>", s)
    s = _HEX_RE.sub("<addr>", s)
    s = _LINECOL_RE.sub("", s)
    s = _PATH_RE.sub("<path>", s)
    s = _NUM_RE.sub("N", s)
    s = _WS_RE.sub(" ", s).strip(" .:-=_*#")
    if not re.search(r"[a-z]", s):
        return ""
    return s[:_MAX_LEN]

"""Find stack traces and log blocks inside free text, and normalise them.

Pasted traces/logs would drown the surrounding prose in one embedding and
their line numbers / timestamps make identical errors look different. So they
are cut out into their own segments; for embedding we keep the exception class
and message plus the first few frames, with volatile parts removed. The
original text is kept separately for display.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Java / Kotlin / JS frames ("at pkg.Cls.m(File.java:12)", "at fn (file.js:1:2)", "at file.js:1:2")
_FRAME = re.compile(r"^\s*at\s+\S.*\(.*\)\s*$|^\s*at\s+\S+:\d+:\d+\s*$")
_MORE = re.compile(r"^\s*\.\.\.\s*\d+\s+(?:more|common frames omitted)")
_CAUSED = re.compile(r"^\s*Caused by:\s")
_PY_HEADER = re.compile(r"^\s*Traceback \(most recent call last\):")
_PY_FRAME = re.compile(r'^\s+File ".*", line \d+')
_RUBY_FRAME = re.compile(r"^\s*from\s+\S+:\d+:in\s")
_EXC_LINE = re.compile(r"^\s*(?:Exception in thread \"[^\"]*\"\s+)?[\w.$]+(?:Exception|Error|Throwable)\b")
_LOG_LINE = re.compile(r"^\s*\[?\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}")

_TIMESTAMP = re.compile(
    r"\d{4}[-/]\d{2}[-/]\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
    r"|\b\d{2}:\d{2}:\d{2}[.,]\d+"
)
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_LINE_NO = re.compile(r":\d+(?::\d+)?(?=\)|\s|$)")
_PY_LINE_NO = re.compile(r"line \d+")

MIN_LOG_LINES = 3


@dataclass(slots=True)
class Segment:
    kind: str  # "text" | "trace" | "log"
    text: str


def _is_frame(line: str) -> bool:
    return bool(_FRAME.match(line) or _MORE.match(line) or _CAUSED.match(line) or _PY_FRAME.match(line) or _RUBY_FRAME.match(line))


def split_segments(text: str) -> list[Segment]:
    lines = text.splitlines()
    n = len(lines)
    kind = ["text"] * n

    in_py = False
    for i, line in enumerate(lines):
        if _PY_HEADER.match(line):
            kind[i], in_py = "trace", True
            continue
        if in_py:
            if not line.strip():
                in_py = False
                continue
            kind[i] = "trace"
            if not line[0].isspace():  # the final "SomeError: message" line ends it
                in_py = False
            continue
        if _is_frame(line):
            kind[i] = "trace"
            if i > 0 and kind[i - 1] == "text" and _EXC_LINE.match(lines[i - 1]):
                kind[i - 1] = "trace"  # the exception header right above the frames

    i = 0
    while i < n:
        if kind[i] == "text" and _LOG_LINE.match(lines[i]):
            j = i
            while j < n and kind[j] == "text" and _LOG_LINE.match(lines[j]):
                j += 1
            if j - i >= MIN_LOG_LINES:
                kind[i:j] = ["log"] * (j - i)
            i = j
        else:
            i += 1

    # A blank line stays inside a block only if both neighbours belong to it.
    for i, line in enumerate(lines):
        if not line.strip():
            prev = kind[i - 1] if i > 0 else "text"
            nxt = next((kind[j] for j in range(i + 1, n) if lines[j].strip()), "text")
            kind[i] = prev if prev == nxt else "text"

    segments: list[Segment] = []
    start = 0
    for i in range(1, n + 1):
        if i == n or kind[i] != kind[start]:
            body = "\n".join(lines[start:i]).strip("\n")
            if body.strip():
                segments.append(Segment(kind[start], body))
            start = i
    return segments


def _scrub(line: str) -> str:
    line = _TIMESTAMP.sub("", line)
    line = _UUID.sub("", line)
    line = _HEX.sub("", line)
    return re.sub(r"[ \t]{2,}", " ", line).strip()


def normalize_trace(text: str, max_frames: int = 5) -> str:
    """Keep headers/causes/messages and the first ``max_frames`` frames, minus line numbers."""
    out: list[str] = []
    frames = 0
    keep_detail = False  # python: the source line under a kept "File ..." line
    for line in text.splitlines():
        if not line.strip():
            continue
        if not line[0].isspace():  # exception header, "Caused by:", final python error line
            out.append(_scrub(line))
            keep_detail = False
        elif _is_frame(line):
            frames += 1
            keep_detail = frames <= max_frames
            if keep_detail and not _MORE.match(line):
                out.append(_PY_LINE_NO.sub("line", _LINE_NO.sub("", _scrub(line))))
        elif keep_detail:
            out.append(_scrub(line))
            keep_detail = False
    return "\n".join(l for l in out if l)


def normalize_log(text: str, max_lines: int = 10) -> str:
    lines = [_scrub(l) for l in text.splitlines() if l.strip()]
    return "\n".join(l for l in lines[:max_lines] if l)

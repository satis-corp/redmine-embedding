"""Excerpt of a chunk for the search result.

Credentials in the text are masked only here (the index is not touched).
Masking runs on the whole normalised text *before* the cut, so a value that
straddles the length limit cannot be left half visible.
"""

from __future__ import annotations

import re
from collections.abc import Callable

_I = re.IGNORECASE

_LABEL = (
    # optional prefix such as DB_ / spring.datasource. / PG / root
    r"(?<![A-Za-z0-9$_.])(?:[A-Za-z0-9]+[_.])*"
    r"(?:[A-Za-z0-9]*(?:password|passwd|(?-i:PW))|PW|pass|pwd|パスワード|ﾊﾟｽﾜｰﾄﾞ)"
)
# The value: a quoted string (up to the closing quote), or a run of non-space
# characters (may contain , ' ) inside; trailing punctuation is not part of it).
_LABEL_VALUE = re.compile(
    r"(?P<head>" + _LABEL + r"[\"']?(?:\s*(?:=>|[:：=＝])\s*|\s+is\s+))"
    r"(?:\"(?P<dq>[^\"]{1,100})\"|'(?P<sq>[^']{1,100})'"
    r"|[\"']?(?P<raw>(?!<)[^\s\"、。;）」』]+[^\s\"、。;）」』,.:)]))",
    _I,
)


def _mask_label_value(m: re.Match[str]) -> str:
    head = m.group("head")
    if m.group("dq") is not None:
        return head + '"****"'
    if m.group("sq") is not None:
        return head + "'****'"
    return head + "****"

# (pattern, replacement). Only "label + separator + value" and well-known
# formats are masked; a bare word such as "password" is left alone.
_MASK_RULES: list[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]]] = [
    # Private key block (to the END line, or to the end of the text).
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
     "[REDACTED:private key]"),
    # Well-known token formats (not in the middle of a word such as "task-...").
    (re.compile(
        r"(?<![A-Za-z0-9_-])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
        r"|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}"
        r"|xox[baprs]-[A-Za-z0-9-]{10,}|glpat-[A-Za-z0-9_-]{15,}|AIza[0-9A-Za-z_-]{30,})"),
     "[REDACTED:token]"),
    # user:pass@ (the user may be empty) inside a URL.
    (re.compile(r"(?<=://)[^/\s:@]*:[^/\s@]+(?=@)"), "****:****"),
    # Bearer / Basic credentials (the scheme name is case-sensitive, except Bearer).
    (re.compile(r"\b((?i:bearer)|Basic|BASIC)\s+[A-Za-z0-9._=+/-]{16,}"), r"\1 ****"),
    # secret / token / api_key = value.
    (re.compile(
        r"((?:api[_-]?key|secret|token|access[_-]?key)[\"']?\s*[:=：＝]\s*[\"']?)[A-Za-z0-9_./+=-]{12,}", _I),
     r"\1****"),
    # Label + separator + value (PW: / パスワード： / DB_PASSWORD= / "password": "x" ...).
    (_LABEL_VALUE, _mask_label_value),
]


def mask_credentials(text: str) -> str:
    """Replace credential-looking values in ``text`` (label kept, value hidden)."""
    for pattern, repl in _MASK_RULES:
        text = pattern.sub(repl, text)
    return text


def make_excerpt(content: str, max_chars: int, *, mask: bool = True) -> str:
    """Whitespace-normalised, at most ``max_chars`` characters (``…`` marks a cut).

    With ``mask`` (default) credentials are hidden before the text is cut.
    """
    text = " ".join(content.split())
    if mask:
        text = mask_credentials(text)
    if len(text) <= max_chars:
        return text
    return text[: max(max_chars - 1, 0)] + "…"

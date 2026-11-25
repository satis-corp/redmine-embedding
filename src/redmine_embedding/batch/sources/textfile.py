"""Decide whether an attachment is a text file and turn its bytes into text.

Standard library only. PDF / Office extraction is deliberately not here: the
attachment connector takes any ``bytes -> str | None`` callable, so an extractor
library can be plugged in later without touching the connector.

The extension list and the excluded names below are PROVISIONAL; they are module constants so they are easy to change.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re

#: 暫定。content_type が text/* でなくても、この拡張子なら対象。
TEXT_EXTENSIONS = frozenset(
    ".txt .md .markdown .csv .tsv .log .json .xml .yml .yaml .ini .conf .properties .sql .py .sh .diff .patch".split()
)
#: 暫定。text/* でも対象外にする（タグがノイズになる）。
NON_TEXT_EXTENSIONS = frozenset({".html", ".htm"})
#: 暫定。拡張子が対象内でも本文を取り込まないファイル名（小文字で比較）。
EXCLUDED_NAME_PATTERNS = (
    ".env*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.ppk", "*.jks", "*.keystore",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*", ".netrc", ".pgpass",
    "*credential*", "*secret*", "*password*", "*passwd*", "*token*",
)
#: 暫定。本文にこの形のヘッダがあるテキストは（ファイル名に関係なく）本文を取り込まない。
PRIVATE_KEY_HEADER = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")

_SNIFF_BYTES = 8192
_MAX_CONTROL_RATIO = 0.05
_HALFWIDTH_KANA = range(0xFF61, 0xFFA0)


def _basename(filename: str) -> str:
    return posixpath.basename(filename.replace("\\", "/")).lower()


def is_excluded_name(filename: str) -> bool:
    """True for names that may hold secrets (case-insensitive, directory part ignored)."""
    name = _basename(filename)
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in EXCLUDED_NAME_PATTERNS)


def contains_private_key(text: str) -> bool:
    """True when the text holds a PEM private key header (``-----BEGIN ... PRIVATE KEY-----``)."""
    return PRIVATE_KEY_HEADER.search(text) is not None


def is_text_candidate(filename: str, content_type: str | None) -> bool:
    """Judge by extension and content_type (content_type can be None or odd, e.g. text/x-python)."""
    ext = posixpath.splitext(_basename(filename))[1]
    if ext in NON_TEXT_EXTENSIONS:
        return False
    if ext in TEXT_EXTENSIONS:
        return True
    mime = (content_type or "").split(";")[0].strip().lower()
    return mime.startswith("text/") and mime != "text/html"


def _control_ratio(text: str) -> float:
    if not text:
        return 0.0
    bad = sum(1 for ch in text if ord(ch) < 32 and ch not in "\t\n\r\f")
    return bad / len(text)


def decode_text(data: bytes) -> str | None:
    """Decode a text file; None when it looks binary or no encoding fits.

    Order: BOM (UTF-8 / UTF-16), UTF-8, CP932 (Shift_JIS superset), EUC-JP.
    ``replace`` is never used, so garbled text is not indexed. CP932 output that
    is mostly half-width katakana is probably EUC-JP and is retried as such.
    """
    if not data:
        return ""
    if data.startswith(b"\xef\xbb\xbf"):
        text = _try(data, "utf-8-sig")
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = _try(data, "utf-16")
    else:
        if b"\x00" in data[:_SNIFF_BYTES]:
            return None
        text = _try(data, "utf-8")
        if text is None:
            text = _try(data, "cp932")
            if text is None or _kana_ratio(text) > 0.3:
                text = _try(data, "euc_jp") or text
    if text is None or _control_ratio(text) > _MAX_CONTROL_RATIO:
        return None
    return text


def _try(data: bytes, encoding: str) -> str | None:
    try:
        return data.decode(encoding)
    except (UnicodeDecodeError, ValueError):
        return None


def _kana_ratio(text: str) -> float:
    return sum(1 for ch in text if ord(ch) in _HALFWIDTH_KANA) / len(text) if text else 0.0

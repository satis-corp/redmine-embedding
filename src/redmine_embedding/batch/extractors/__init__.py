"""Text extraction from binary documents (PDF / xlsx). PROVISIONAL.

Each extractor is ``(data: bytes, max_chars: int) -> str | None``. ``None`` means
"no body" (unsupported, encrypted, broken, too big, no text layer); the caller
then indexes the file name / description only. The extractor libraries are
imported lazily so that the MCP server (which does not install them) can import
this package.
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable

from .pdf import extract_pdf
from .xlsx import extract_xlsx

DocumentExtractor = Callable[[bytes, int], "str | None"]

#: Extensions handled here. Deliberately NOT part of ``TEXT_EXTENSIONS``: these bytes must never be decoded as text.
DOCUMENT_EXTRACTORS: dict[str, DocumentExtractor] = {".pdf": extract_pdf, ".xlsx": extract_xlsx}


def document_extractor(filename: str) -> DocumentExtractor | None:
    """Extractor for the file's extension (case-insensitive; content_type is not consulted), or None."""
    ext = posixpath.splitext(posixpath.basename(filename.replace("\\", "/")).lower())[1]
    return DOCUMENT_EXTRACTORS.get(ext)

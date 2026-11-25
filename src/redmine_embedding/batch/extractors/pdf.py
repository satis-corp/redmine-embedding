"""PDF text via pypdf (BSD-3-Clause). PROVISIONAL limits .

Never logs file names, text, URLs or keys; only the exception type name.
"""

from __future__ import annotations

import contextlib
import io
import logging
import time

logger = logging.getLogger(__name__)

#: 暫定。これを超えるページは読まない。
MAX_PAGES = 200
#: 暫定。1 ファイルの抽出に使う時間（ページ単位で確認。1 ページ内は止められない）。
MAX_SECONDS = 30.0


@contextlib.contextmanager
def _quiet_pypdf():
    """pypdf's own warnings can echo file content; mute them only while extracting."""
    pylog = logging.getLogger("pypdf")
    old = pylog.level
    pylog.setLevel(logging.CRITICAL + 1)
    try:
        yield
    finally:
        pylog.setLevel(old)


def extract_pdf(
    data: bytes, max_chars: int, *, max_pages: int = MAX_PAGES, max_seconds: float = MAX_SECONDS
) -> str | None:
    """Text layer of a PDF, or None. Encrypted PDFs are skipped (no password is tried); no OCR."""
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pdf: pypdf is not installed; body skipped (install the 'batch' extra)")
        return None
    deadline = time.monotonic() + max_seconds
    parts: list[str] = []
    total = 0
    try:
        with _quiet_pypdf():
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                logger.warning("pdf: encrypted; body skipped")
                return None
            for index, page in enumerate(reader.pages):
                if index >= max_pages or time.monotonic() > deadline or total > max_chars:
                    break  # cut here: what was read so far is kept
                text = (page.extract_text() or "").strip()
                if text:
                    parts.append(text)
                    total += len(text) + 2
    except Exception as e:  # noqa: BLE001 - a broken file must not stop the sync
        logger.warning("pdf: body skipped (%s)", type(e).__name__)
        return None
    body = "\n\n".join(parts)[:max_chars].strip()
    return body or None

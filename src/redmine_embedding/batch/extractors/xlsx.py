"""xlsx cell values via openpyxl (MIT). PROVISIONAL limits.

Values only (``data_only``: cached results, not formulas). External links and
macros are not read; ``.xlsm`` is not handled (the extension is not registered).
Hidden sheets are skipped. Never logs file names, cell text, URLs or keys.
"""

from __future__ import annotations

import io
import logging
import time
import zipfile

logger = logging.getLogger(__name__)

#: 暫定。zip の宣言展開サイズの合計がこれを超えたら読まない（zip bomb 対策）。
MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
#: 暫定。xl/sharedStrings.xml の宣言サイズがこれを超えたら読まない
#: （openpyxl は read_only でも共有文字列を全部メモリに載せる。XML の約 8 倍のメモリ）。
MAX_SHARED_STRINGS_BYTES = 10 * 1024 * 1024
#: 暫定。zip 内のファイル数の上限。
MAX_ZIP_ENTRIES = 2000
#: 暫定。1 ファイルの抽出に使う時間（行単位で確認）。
MAX_SECONDS = 30.0


def extract_xlsx(
    data: bytes,
    max_chars: int,
    *,
    max_uncompressed: int = MAX_UNCOMPRESSED_BYTES,
    max_seconds: float = MAX_SECONDS,
    max_shared_strings: int = MAX_SHARED_STRINGS_BYTES,
) -> str | None:
    """``# <sheet name>`` then one tab-separated line per non-empty row, or None."""
    try:
        from openpyxl import load_workbook
    except ImportError:
        logger.warning("xlsx: openpyxl is not installed; body skipped (install the 'batch' extra)")
        return None
    deadline = time.monotonic() + max_seconds  # counted from before the load (load itself cannot be interrupted)
    wb = None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            # Python's zip reader stops at the declared size, so the declared sum is a real cap.
            if len(infos) > MAX_ZIP_ENTRIES or sum(i.file_size for i in infos) > max_uncompressed:
                logger.warning("xlsx: archive too large when expanded; body skipped")
                return None
            if sum(i.file_size for i in infos if i.filename.lower() == "xl/sharedstrings.xml") > max_shared_strings:
                logger.warning("xlsx: shared strings too large; body skipped")
                return None
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False, keep_vba=False)
        lines: list[str] = []
        total = 0
        for ws in wb.worksheets:
            if time.monotonic() > deadline:
                break  # the load itself may have used up the time
            if ws.sheet_state != "visible":
                continue
            header_at = len(lines)
            lines.append(f"# {ws.title}")
            ws.reset_dimensions()  # a forged <dimension> would make openpyxl pad rows up to it
            for n, row in enumerate(ws.iter_rows(values_only=True)):
                cells = [str(v).strip() for v in row if v is not None and str(v).strip()]
                if cells:
                    line = "\t".join(cells)
                    lines.append(line)
                    total += len(line) + 1
                if total > max_chars or (n % 500 == 0 and time.monotonic() > deadline):
                    break
            if len(lines) == header_at + 1:
                lines.pop()  # empty sheet: no header
            if total > max_chars or time.monotonic() > deadline:
                break
    except Exception as e:  # noqa: BLE001 - a broken file must not stop the sync
        logger.warning("xlsx: body skipped (%s)", type(e).__name__)
        return None
    finally:
        if wb is not None:
            wb.close()
    body = "\n".join(lines)[:max_chars].strip()
    return body or None

"""PDF / xlsx extraction. Files are built in memory; no network."""

import builtins
import io
import logging
import re
import time
import zipfile

import pytest

openpyxl = pytest.importorskip("openpyxl")
pypdf = pytest.importorskip("pypdf")

from redmine_embedding.batch.extractors import document_extractor, pdf, xlsx  # noqa: E402
from redmine_embedding.batch.sources.attachments import read_attachment_text  # noqa: E402
from redmine_embedding.batch.sources.textfile import decode_text  # noqa: E402


def make_pdf(pages: list[str]) -> bytes:
    """Minimal PDF with one Helvetica text line per page (empty string = no text layer)."""
    objs: list[bytes] = []
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET".encode() if text else b""
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {5 + 2 * i} 0 R "
            "/Resources << /Font << /F1 3 0 R >> >> >>".encode()
        )
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    out = b"%PDF-1.4\n"
    offsets = []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def make_xlsx(sheets: dict[str, list[list]], hidden=()) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
        if name in hidden:
            ws.sheet_state = "hidden"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# -- PDF -----------------------------------------------------------------------
def test_pdf_extracts_text_of_every_page():
    assert pdf.extract_pdf(make_pdf(["Hello page one", "Second page"]), 1000) == "Hello page one\n\nSecond page"


def test_pdf_without_text_layer_or_empty_is_none():
    assert pdf.extract_pdf(make_pdf(["", ""]), 1000) is None  # image-only stand-in: no OCR
    assert pdf.extract_pdf(make_pdf([]), 1000) is None


def test_pdf_encrypted_is_none_and_no_password_tried(caplog):
    writer = pypdf.PdfWriter(clone_from=io.BytesIO(make_pdf(["secret words"])))
    writer.encrypt("pw", algorithm="AES-128" if _has_crypto() else "RC4-128")
    buf = io.BytesIO()
    writer.write(buf)
    with caplog.at_level(logging.WARNING):
        assert pdf.extract_pdf(buf.getvalue(), 1000) is None
    assert "encrypted" in caplog.text
    assert "secret" not in caplog.text


def _has_crypto():
    try:
        import cryptography  # noqa: F401
    except ImportError:
        return False
    return True


def test_pdf_broken_file_is_none_with_type_name_only(caplog):
    with caplog.at_level(logging.WARNING):
        assert pdf.extract_pdf(b"%PDF-1.4\nthis is not a pdf at all", 1000) is None
        assert pdf.extract_pdf(make_pdf(["x"])[:60], 1000) is None
    assert "body skipped (" in caplog.text


def test_pdf_stops_at_char_limit():
    big = make_pdf(["A" * 100] * 50)
    text = pdf.extract_pdf(big, 250)
    assert text is not None and len(text) <= 250
    # pages beyond the limit are not even read
    reads = []
    original = pypdf.PageObject.extract_text

    def counting(self, *a, **k):
        reads.append(1)
        return original(self, *a, **k)

    pypdf.PageObject.extract_text = counting
    try:
        pdf.extract_pdf(big, 250)
    finally:
        pypdf.PageObject.extract_text = original
    assert len(reads) < 10


def test_pdf_page_limit():
    data = make_pdf([f"page{i}" for i in range(10)])
    text = pdf.extract_pdf(data, 10_000, max_pages=3)
    assert text == "page0\n\npage1\n\npage2"


def test_pdf_time_limit_keeps_what_was_read():
    data = make_pdf([f"page{i}" for i in range(10)])
    assert pdf.extract_pdf(data, 10_000, max_seconds=-1) is None  # deadline already passed: nothing read


# -- xlsx ----------------------------------------------------------------------
def test_xlsx_multiple_sheets_japanese_and_compaction():
    data = make_xlsx({
        "仕様": [["項目", "値"], [None, None], ["名前", "テスト"], [1, 2.5, None, "x"]],
        "Sheet2": [["a"], [""]],
        "Empty": [[None, None]],
    })
    assert xlsx.extract_xlsx(data, 1000) == "# 仕様\n項目\t値\n名前\tテスト\n1\t2.5\tx\n# Sheet2\na"


def test_xlsx_uses_cached_values_not_formulas_and_skips_hidden():
    data = make_xlsx({"S": [[1, 2, "=A1+B1"]], "H": [["hidden text"]]}, hidden=("H",))
    text = xlsx.extract_xlsx(data, 1000)
    assert "=A1" not in text  # no cached value was written by openpyxl, so the formula cell is empty
    assert "hidden" not in text


def test_xlsx_empty_is_none():
    assert xlsx.extract_xlsx(make_xlsx({"S": [[None]]}), 1000) is None


def test_xlsx_broken_or_not_a_zip_is_none(caplog):
    with caplog.at_level(logging.WARNING):
        assert xlsx.extract_xlsx(b"PK\x03\x04 garbage", 1000) is None
        assert xlsx.extract_xlsx(b"plain text", 1000) is None
    assert "body skipped (" in caplog.text


def test_xlsx_stops_at_char_limit():
    data = make_xlsx({"S": [["x" * 50] for _ in range(1000)]})
    text = xlsx.extract_xlsx(data, 200)
    assert text is not None and len(text) <= 200


def test_xlsx_zip_bomb_declared_size_is_refused(caplog):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/worksheets/sheet1.xml", b"\0" * (3 * 1024 * 1024))  # compresses to a few KB
    assert len(buf.getvalue()) < 100_000
    with caplog.at_level(logging.WARNING):
        assert xlsx.extract_xlsx(buf.getvalue(), 1000, max_uncompressed=1024 * 1024) is None
    assert "too large" in caplog.text


def test_xlsx_too_many_zip_entries_is_refused(monkeypatch):
    monkeypatch.setattr(xlsx, "MAX_ZIP_ENTRIES", 1)
    assert xlsx.extract_xlsx(make_xlsx({"S": [["a"]]}), 1000) is None


def test_xlsx_time_limit_counts_from_before_the_load():
    assert xlsx.extract_xlsx(make_xlsx({"S": [["a"]]}), 1000, max_seconds=-1) is None  # deadline already passed


def test_xlsx_forged_dimension_does_not_pad_rows_or_burn_time():
    data = make_xlsx({"S": [["a", "b"]]})
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            raw = zin.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                text = raw.decode()
                assert "<dimension" in text
                text = re.sub(r'<dimension ref="[^"]*"', '<dimension ref="A1:XFD1048576"', text)
                text = text.replace("</sheetData>", '<row r="1048576"><c r="A1048576" t="n"><v>1</v></c></row></sheetData>')
                raw = text.encode()
            zout.writestr(info.filename, raw)
    t0 = time.monotonic()
    text = xlsx.extract_xlsx(out.getvalue(), 1000, max_seconds=5)
    assert time.monotonic() - t0 < 3
    assert text is not None and text.startswith("# S\na\tb")


def test_xlsx_shared_strings_over_limit_is_refused(caplog):
    data = make_xlsx({"S": [["x" * 200] * 5]})
    plain = data
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            zout.writestr(info.filename, zin.read(info.filename))
        zout.writestr("xl/sharedStrings.xml", b"<sst>" + b"<si><t>a</t></si>" * 100 + b"</sst>")
    data = out.getvalue()
    with caplog.at_level(logging.WARNING):
        assert xlsx.extract_xlsx(data, 1000, max_shared_strings=100) is None
    assert "shared strings too large" in caplog.text
    assert xlsx.extract_xlsx(plain, 1000, max_shared_strings=100) is not None  # no shared strings part
    assert xlsx.extract_xlsx(data, 1000, max_shared_strings=10_000_000) is not None  # default limit lets normal files through


def test_pypdf_logger_is_muted_during_extraction_and_restored():
    pylog = logging.getLogger("pypdf")
    seen = []

    class H(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    h = H()
    pylog.addHandler(h)
    old = pylog.level
    pylog.setLevel(logging.WARNING)
    try:
        original = pypdf.PageObject.extract_text

        def noisy(self, *a, **k):
            pylog.warning("SECRET-FRAGMENT-FROM-FILE")
            return original(self, *a, **k)

        pypdf.PageObject.extract_text = noisy
        try:
            assert pdf.extract_pdf(make_pdf(["hi"]), 100) == "hi"
        finally:
            pypdf.PageObject.extract_text = original
        assert seen == []
        assert pylog.level == logging.WARNING
    finally:
        pylog.removeHandler(h)
        pylog.setLevel(old)


# -- fallback when a library is missing ------------------------------------------
@pytest.mark.parametrize("module,func,data", [
    ("pypdf", pdf.extract_pdf, b"%PDF-1.4"),
    ("openpyxl", xlsx.extract_xlsx, b"PK"),
])
def test_missing_library_gives_none_and_warning(monkeypatch, caplog, module, func, data):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == module or name.startswith(module + "."):
            raise ImportError(name)
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with caplog.at_level(logging.WARNING):
        assert func(data, 1000) is None
    assert "not installed" in caplog.text


# -- wiring into read_attachment_text -------------------------------------------
class FakeClient:
    def __init__(self, data: bytes):
        self.data, self.downloads = data, 0

    def download(self, url, max_bytes):
        self.downloads += 1
        return self.data


def meta(name, ctype="application/octet-stream", size=100):
    return {"id": 1, "filename": name, "content_type": ctype, "filesize": size, "content_url": "https://r.example.com/a"}


def read(name, data, ctype="application/octet-stream", max_chars=1000, **over):
    client = FakeClient(data)
    return read_attachment_text(client, meta(name, ctype, **over), 2_000_000, max_chars, decode_text), client


def test_dispatch_by_extension_not_content_type():
    text, _ = read("Spec.PDF", make_pdf(["hello pdf"]), ctype="application/pdf")
    assert text == "hello pdf"
    text, _ = read("book.xlsx", make_xlsx({"S": [["日本語", "セル"]]}), ctype="application/octet-stream")
    assert text == "# S\n日本語\tセル"


def test_not_decoded_as_text_and_other_office_formats_unsupported():
    assert document_extractor("a.docx") is None and document_extractor("a.xlsm") is None
    assert document_extractor("dir\\b.PdF") is pdf.extract_pdf
    text, client = read("a.docx", b"PK\x03\x04hello text", ctype="application/vnd.ms-word")
    assert text is None and client.downloads == 0


def test_size_limit_applies_before_download():
    text, client = read("big.pdf", make_pdf(["x"]), size=3_000_000)
    assert text is None and client.downloads == 0


def test_char_limit_applies_to_documents():
    text, _ = read("a.pdf", make_pdf(["B" * 500]), max_chars=100)
    assert text == "B" * 100


def test_excluded_name_and_private_key_header_apply():
    text, client = read("token-list.xlsx", make_xlsx({"S": [["a"]]}))
    assert text is None and client.downloads == 0
    key = "-----BEGIN RSA PRIVATE KEY-----"
    assert read("notes.xlsx", make_xlsx({"S": [[key]]}))[0] is None
    assert read("notes.pdf", make_pdf([key]))[0] is None


def test_broken_document_does_not_raise():
    assert read("a.pdf", b"garbage")[0] is None
    assert read("a.xlsx", b"garbage")[0] is None


def test_empty_document_logs_attachment_id_only(caplog):
    with caplog.at_level(logging.WARNING):
        assert read("quarterly-report.pdf", b"garbage")[0] is None
    assert "attachment 1:" in caplog.text and "quarterly-report" not in caplog.text


def test_files_tab_path_reads_pdf_and_xlsx_via_the_shared_function():
    from redmine_embedding.batch.sources.files import FileConnector

    def build(name, data):
        c = FileConnector(FakeClient(data))
        return c._build({"id": 7, "filename": name, "filesize": 100, "content_url": "https://r.example.com/a",
                         "created_on": "2026-01-01T00:00:00Z"}, 1)

    doc = build("spec.pdf", make_pdf(["files pdf"]))
    assert [s.text for s in doc.sections][-1] == "files pdf"
    doc = build("book.xlsx", make_xlsx({"S": [["日本語"]]}))
    assert [s.text for s in doc.sections][-1] == "# S\n日本語"

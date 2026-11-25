import pytest

from redmine_embedding.batch.sources.textfile import contains_private_key, decode_text, is_excluded_name, is_text_candidate


@pytest.mark.parametrize("name,ctype,expected", [
    ("notes.txt", "text/plain", True),
    ("README.MD", None, True),  # content_type None, extension decides; case-insensitive
    ("data.csv", "application/octet-stream", True),
    ("run.py", "text/x-python", True),
    ("fix.patch", None, True),
    ("noext", "text/plain; charset=utf-8", True),  # content_type decides, parameters ignored
    ("noext", "text/x-whatever", True),
    ("page.html", "text/html", False),
    ("page.HTM", "text/plain", False),  # html is excluded even when content_type says text
    ("noext", "text/html", False),
    ("report.pdf", "application/pdf", False),
    ("book.xlsx", None, False),
    ("shot.png", "image/png", False),
    ("mystery", None, False),
])
def test_is_text_candidate(name, ctype, expected):
    assert is_text_candidate(name, ctype) is expected


@pytest.mark.parametrize("name", [
    ".env", ".env.production", "server.pem", "A.KEY", "cert.p12", "cert.pfx", "id_rsa", "id_rsa.pub",
    "aws_credentials.txt", "My-Secret-Notes.md", "dir/sub/.env",
    "id_ed25519", "id_ecdsa.pub", "id_dsa", "prod.env", "APP.ENV", ".netrc", ".pgpass", "key.ppk", "store.jks",
    "my.keystore", "db_password.txt", "passwd.txt", "api-token.md", "Token.json",
])
def test_excluded_names(name):
    assert is_excluded_name(name)


@pytest.mark.parametrize("name", ["notes.txt", "environment.md", "keynote.txt", "monkey.txt"])
def test_normal_names_are_not_excluded(name):
    assert not is_excluded_name(name)


def test_decodes_utf8_cp932_and_boms():
    assert decode_text("日本語のログ\n".encode()) == "日本語のログ\n"
    assert decode_text("日本語".encode("shift_jis")) == "日本語"
    assert decode_text("機種依存文字①㈱".encode("cp932")) == "機種依存文字①㈱"
    assert decode_text(b"\xef\xbb\xbf" + "bom付き".encode()) == "bom付き"  # BOM is dropped
    assert decode_text("あいう".encode("utf-16")) == "あいう"  # BOM written by the codec
    assert decode_text(b"") == ""


def test_euc_jp_misread_as_cp932_is_corrected():
    text = "これはEUC-JPで保存されたテキストファイルの内容です。" * 3
    assert decode_text(text.encode("euc_jp")) == text


    kana_only = "ひらがなだけのてすと"  # valid as cp932 too (all half-width katakana), but EUC-JP is meant
    assert decode_text(kana_only.encode("euc_jp")) == kana_only


def test_binary_and_undecodable_data_is_rejected():
    assert decode_text(b"abc\x00def") is None  # NUL
    assert decode_text(b"\x89PNG\r\n\x1a\n" + bytes(range(128, 256))) is None  # not any encoding
    assert decode_text(bytes([1, 2, 3, 4, 5, 6, 7, 8]) * 20 + b"text") is None  # control characters
    assert decode_text(b"\xff\xfe\x00\xd8") is None  # broken UTF-16 (lone surrogate)


def test_private_key_header_is_detected_in_text():
    assert contains_private_key("x\n-----BEGIN OPENSSH PRIVATE KEY-----\n")
    assert contains_private_key("-----BEGIN PRIVATE KEY-----")
    assert contains_private_key("-----BEGIN ENCRYPTED PRIVATE KEY-----")
    assert not contains_private_key("-----BEGIN CERTIFICATE-----\n")
    assert not contains_private_key("a private key is needed")

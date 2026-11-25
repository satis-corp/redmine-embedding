import pytest

from redmine_embedding.mcp_server.search.excerpt import make_excerpt

# Dummy values only.
MASKED = [
    ("ID: admin PW: Zq9xYw3p", "ID: admin PW: ****"),
    ("パスワード：Abcd1234!", "パスワード：****"),
    ("パスワード: Abcd1234", "パスワード: ****"),
    ("password = 'hunter22'", "password = '****'"),
    ("db passwd=Secr3tXx", "db passwd=****"),
    ("push ghp_" + "a" * 30 + " now", "push [REDACTED:token] now"),
    ("key AKIA" + "A" * 16, "key [REDACTED:token]"),
    ("curl -H 'Authorization: Bearer " + "abcDEF123456" * 2 + "'", "curl -H 'Authorization: Bearer ****'"),
    ("Authorization: Basic dXNlcjpwYXNzd29yZDEyMzQ=", "Authorization: Basic ****"),
    ("git clone https://bob:pa55w0rd@git.example/r.git", "git clone https://****:****@git.example/r.git"),
    ("api_key = 'abcdefghij1234'", "api_key = '****'"),
    ("SECRET: abcdefghijkl", "SECRET: ****"),
    ("token=abcdefghijklmnop", "token=****"),
    ("export DB_PASSWORD=Zq9xYw3p", "export DB_PASSWORD=****"),
    ("PGPASSWORD=Zq9xYw3p psql", "PGPASSWORD=**** psql"),
    ("MYSQL_ROOT_PASSWORD=Zq9xYw3p", "MYSQL_ROOT_PASSWORD=****"),
    ("db_pass=Zq9xYw3p", "db_pass=****"),
    ("rootPW: Zq9xYw3p", "rootPW: ****"),
    ("spring.datasource.password=Zq9xYw3p", "spring.datasource.password=****"),
    ('{"password": "Zq9xYw3p"}', '{"password": "****"}'),
    ("'password' => 'Zq9xYw3p'", "'password' => '****'"),
    ("password is Zq9xYw3p", "password is ****"),
    ("ﾊﾟｽﾜｰﾄﾞ: Zq9xYw3p", "ﾊﾟｽﾜｰﾄﾞ: ****"),
    ("パスワード＝Abcd1234", "パスワード＝****"),
    ("gho_" + "a" * 25, "[REDACTED:token]"),
    ("PW: ab,cdEFGH", "PW: ****"),
    ("PW:ab'cdefg", "PW:****"),
    ('PW: "my secret pass" 後', 'PW: "****" 後'),
    ("PW: abc, 次へ", "PW: ****, 次へ"),
    ("redis://:pass@host:6379", "redis://****:****@host:6379"),
    ("-----BEGIN RSA PRIVATE KEY----- MIIE abc -----END RSA PRIVATE KEY----- 後", "[REDACTED:private key] 後"),
]

UNTOUCHED = [
    "パスワードを入力してください",
    "パスワードは8文字以上にする",
    "ID/PW 認証を使う",
    "sudo passwd <user> で変更する",
    "password policy and token handling",
    "$pass = 1; bypass: no",
    "https://example.com/path:80@x",
    "token: short",
    "Bearer token を付ける",
    "ログイン情報を確認する",
    "https://example.com/task-management-system-config",
    "disk-usage-monitoring-script を実行",
    "risk-assessment-procedure-v2",
    "a basic infrastructure-management plan",
    "Password:<input type=text>",
]


@pytest.mark.parametrize("src,expected", MASKED)
def test_masks(src, expected):
    assert make_excerpt(src, 500) == expected


@pytest.mark.parametrize("src", UNTOUCHED)
def test_not_masked(src):
    assert make_excerpt(src, 500) == src


def test_no_redos_on_bad_input():
    import time

    for bad in ["://a:" * 40000, "PW:" * 50000, 'PW: "' * 40000, "a_" * 80000 + "password",
                "Basic " * 50000, "password is " * 20000, "PW:'" * 40000]:
        start = time.perf_counter()
        make_excerpt(bad, 300)
        assert time.perf_counter() - start < 1.0


def test_off_passes_through():
    assert make_excerpt("PW: Zq9xYw3p", 500, mask=False) == "PW: Zq9xYw3p"


def test_masks_before_cut():
    text = "x" * 290 + " PW: Zq9xYw3pAbcdef y"
    out = make_excerpt(text, 300)
    assert "Zq9" not in out and "PW: ****" in out
    # Without masking the same cut leaves part of the value visible.
    assert "Zq9x" in make_excerpt(text, 300, mask=False)
    # A masked text that is still too long is cut to the limit.
    out = make_excerpt("x" * 296 + " PW: Zq9xYw3pAbcdef", 300)
    assert len(out) == 300 and out.endswith("…") and "Zq9" not in out


def test_label_and_value_on_other_line_joined_by_whitespace_normalisation():
    # Whitespace is normalised first; this is the existing excerpt behaviour.
    assert make_excerpt("PW:\nZq9xYw3p", 100) == "PW: ****"

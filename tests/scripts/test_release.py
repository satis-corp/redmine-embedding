"""scripts/release.sh のテスト（使い捨ての一時 git リポジトリで実行する）。"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "release.sh"


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / "README.md").write_text("hi\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "init")
    git(r, "switch", "-qc", "dev-line")
    for p, c in {
        "INTERNAL.md": "a",
        "notes/1/x.md": "w",
        "docs/hidden/a.md": "d",
        "docs/keep.md": "k",
        "src/a.py": "print(1)\n",
        "pyproject.toml": 'version = "0.1.0"\n',
        ".release-exclude": "# c\nINTERNAL.md\nnotes/\ndocs/hidden/\n.release-exclude\n.release-forbidden-patterns\n.release-config\n",
        # 架空の禁止パターン(実際の社内文字列はテストに書かない)
        ".release-forbidden-patterns": "# c\nsecretcorp\n(^|[^A-Za-z0-9_])notes/\nINTERNAL-BRANCH\n",
        ".release-config": "SRC_BRANCH=dev-line\n",
    }.items():
        f = r / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(c)
    git(r, "add", ".")
    git(r, "commit", "-qm", "base")
    return r


def run(repo, *args, **env):
    base = {k: v for k, v in os.environ.items() if not k.startswith("RELEASE_GIT_")}
    # 既定で作成者を指定する(必須のため)。未指定のテストは None を渡して外す
    base.update({"RELEASE_GIT_NAME": "redmine-embedding", "RELEASE_GIT_EMAIL": "release@example.com"})
    base.update({k: v for k, v in env.items() if v is not None})
    for k, v in env.items():
        if v is None:
            base.pop(k, None)
    return subprocess.run(["bash", str(SCRIPT), *args], cwd=repo, capture_output=True, text=True, env=base)


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
class TestRelease:
    def test_creates_snapshot(self, repo):
        base = git(repo, "rev-parse", "dev-line")
        (repo / ".venv").mkdir()
        (repo / ".venv" / "keep").write_text("x")
        res = run(repo, "0.1.0")
        assert res.returncode == 0, res.stderr
        files = git(repo, "ls-tree", "-r", "--name-only", "main").splitlines()
        assert "src/a.py" in files and "docs/keep.md" in files
        assert "INTERNAL.md" not in files and ".release-exclude" not in files
        assert not any(f.startswith(("notes/", "docs/hidden/")) for f in files)
        msg = git(repo, "log", "-1", "--format=%B", "main") + git(repo, "tag", "-n9")
        assert "v0.1.0" in msg
        assert base not in msg and "dev-line" not in msg and "INTERNAL.md" not in msg  # 社内情報を出さない
        assert git(repo, "cat-file", "-t", "v0.1.0") == "tag"
        assert git(repo, "rev-list", "--count", "main") == "2"
        assert git(repo, "branch", "--show-current") == "dev-line"
        assert (repo / ".venv" / "keep").exists()
        assert (repo / "INTERNAL.md").exists()

    def test_target_branch_checked_out_stops(self, repo):
        git(repo, "switch", "-q", "main")
        before = git(repo, "rev-parse", "main")
        res = run(repo, "0.1.0", SRC_BRANCH="dev-line")
        assert res.returncode != 0 and "切り出され" in res.stderr
        assert git(repo, "rev-parse", "main") == before
        assert git(repo, "status", "--porcelain") == ""
        assert git(repo, "tag") == ""

    def test_target_branch_checked_out_in_other_worktree_stops(self, repo, tmp_path):
        git(repo, "worktree", "add", "-q", str(tmp_path / "wt"), "main")
        res = run(repo, "0.1.0")
        assert res.returncode != 0 and "切り出され" in res.stderr
        assert git(repo, "tag") == ""

    def test_dirty_tree_stops(self, repo):
        (repo / "src" / "a.py").write_text("changed\n")
        res = run(repo, "0.1.0")
        assert res.returncode != 0
        assert git(repo, "tag") == ""

    def test_duplicate_tag_stops(self, repo):
        git(repo, "tag", "v0.1.0")
        assert run(repo, "0.1.0").returncode != 0
        assert git(repo, "rev-list", "--count", "main") == "1"

    def test_bad_version_stops(self, repo):
        assert run(repo, "1.0").returncode != 0

    def test_pyproject_mismatch_stops(self, repo):
        assert run(repo, "0.2.0").returncode != 0

    def test_missing_branch_stops(self, repo):
        assert run(repo, "0.1.0", SRC_BRANCH="nope").returncode != 0

    @pytest.mark.parametrize(
        "content",
        [
            "x = 1  # " + "#" + "3445\n",  # 組み込み: チケット番号
            "mail bob@" + "acme.io\n",  # 組み込み: メールアドレス
            "host = '10." + "1.2.3'\n",  # 組み込み: 私設 IP
            "host = '192." + "168.0.9'\n",
            "host = '172." + "20.0.9'\n",
            "see " + "no" + "tes/12-foo/README.md\n",  # ファイル由来のパターン
            "url = 'https://secret" + "corp.example'\n",
            "branch INTERNAL" + "-BRANCH\n",
        ],
    )
    def test_forbidden_pattern_stops_without_commit_or_tag(self, repo, content):
        (repo / "src" / "leak.py").write_text(content)
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "leak")
        before = git(repo, "rev-parse", "main")
        res = run(repo, "0.1.0")
        assert res.returncode != 0 and "禁止パターン" in res.stderr and "src/leak.py" in res.stderr
        assert git(repo, "rev-parse", "main") == before
        assert git(repo, "tag") == ""

    @pytest.mark.parametrize(
        "content",
        [
            "mail bob@" + "example.com\n",
            "host = '127.0.0.1' '203.0.113.5' '172.32.0.1' '1.10.2.3'\n",
        ],
    )
    def test_allowed_dummy_values_pass(self, repo, content):
        (repo / "src" / "ok.py").write_text(content)
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "ok")
        res = run(repo, "0.1.0")
        assert res.returncode == 0, res.stderr

    def test_without_patterns_file_only_builtin_patterns(self, repo):
        git(repo, "rm", "-q", ".release-forbidden-patterns")
        (repo / "src" / "a.py").write_text("secretcorp\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "nopat")
        assert run(repo, "0.1.0").returncode == 0

    def test_bad_pattern_in_file_stops_not_ignored(self, repo):
        (repo / ".release-forbidden-patterns").write_text("(unclosed\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "badpat")
        before = git(repo, "rev-parse", "main")
        res = run(repo, "0.1.0")
        assert res.returncode != 0 and "git grep が失敗" in res.stderr
        assert git(repo, "rev-parse", "main") == before
        assert git(repo, "tag") == ""

    def test_src_branch_from_config_and_current_branch(self, repo):
        git(repo, "branch", "other", "dev-line")
        # 設定ファイルが無ければ現在のブランチ(dev-line)
        (repo / ".release-config").unlink()
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "noconf")
        assert run(repo, "0.1.0").returncode == 0
        # 設定ファイルの指定が使われる
        (repo / ".release-config").write_text("SRC_BRANCH=nope\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "conf")
        res = run(repo, "0.1.1")
        assert res.returncode != 0 and "nope" in res.stderr
        # 環境変数が設定ファイルより優先
        (repo / "pyproject.toml").write_text('version = "0.1.1"\n')
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "ver")
        assert run(repo, "0.1.1", SRC_BRANCH="dev-line").returncode == 0

    def test_release_author_from_env(self, repo):
        res = run(repo, "0.1.0", RELEASE_GIT_NAME="Release Bot", RELEASE_GIT_EMAIL="bot@" + "example.com")
        assert res.returncode == 0, res.stderr
        assert git(repo, "log", "-1", "--format=%an <%ae> / %cn <%ce>", "main") == (
            "Release Bot <bot@example.com> / Release Bot <bot@example.com>"
        )
        assert "Release Bot <bot@example.com>" in git(repo, "for-each-ref", "--format=%(taggername) %(taggeremail)", "refs/tags/v0.1.0").replace(" <", " <")

    @pytest.mark.parametrize(
        "env",
        [
            {"RELEASE_GIT_EMAIL": None},
            {"RELEASE_GIT_NAME": None},
            {"RELEASE_GIT_NAME": None, "RELEASE_GIT_EMAIL": None},
            {"RELEASE_GIT_NAME": ""},
            {"RELEASE_GIT_EMAIL": ""},
        ],
    )
    def test_release_author_required(self, repo, env):
        before = git(repo, "rev-parse", "main")
        res = run(repo, "0.1.0", **env)
        assert res.returncode != 0 and "RELEASE_GIT_NAME" in res.stderr and "RELEASE_GIT_EMAIL" in res.stderr
        assert "例:" in res.stderr
        assert git(repo, "rev-parse", "main") == before
        assert git(repo, "tag") == ""

    def test_forbidden_pattern_in_excluded_file_is_fine(self, repo):
        (repo / "notes" / "1" / "x.md").write_text("#" + "3445 INTERNAL" + "-BRANCH secret" + "corp\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "notes")
        assert run(repo, "0.1.0").returncode == 0

    def test_missing_pyproject_version_stops(self, repo):
        (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "nover")
        res = run(repo, "0.1.0")
        assert res.returncode != 0 and "version" in res.stderr
        assert git(repo, "tag") == ""

    def test_missing_exclude_file_stops_with_its_own_message(self, repo):
        git(repo, "rm", "-q", ".release-exclude")
        git(repo, "commit", "-qm", "noexcl")
        res = run(repo, "0.1.0")
        assert res.returncode != 0 and ".release-exclude がありません" in res.stderr
        assert git(repo, "tag") == ""


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_lockfile_tests_and_dummy_email_are_not_false_positives(repo):
    """uv.lock のバージョン文字列、tests/ のダミー IP、サブドメインのダミーメールでは止まらない。"""
    for p, c in {
        "uv.lock": 'version = "10.4.0.35"\n',
        "tests/t.py": 'HOST = "192.168.1.5"\n',
        "docs/keep2.md": "user@h.example.com\n",
    }.items():
        f = repo / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(c)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "dummy")
    assert run(repo, "0.1.0").returncode == 0


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_private_ip_outside_tests_is_forbidden(repo):
    (repo / "docs" / "ip.md").write_text("host 192.168.1.5\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "ip")
    assert run(repo, "0.1.0").returncode != 0


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
@pytest.mark.parametrize("path", ["uv.lock", "tests/t.py"])
def test_forbidden_strings_are_detected_in_lockfile_and_tests(repo, path):
    """IP・メールの除外は、社内固有パターンの検査には効かない。"""
    f = repo / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("url = secretcorp\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "leak")
    assert run(repo, "0.1.0").returncode != 0


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
def test_dummy_example_tld_is_allowed(repo):
    (repo / "docs" / "d.md").write_text("https://user:dummy@git.example/x\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "d")
    assert run(repo, "0.1.0").returncode == 0


@pytest.mark.skipif(shutil.which("git") is None, reason="git required")
class TestAllowedEmails:
    """.release-allowed-emails に載せたアドレスだけ公開を許容する(完全一致)。"""

    def _write(self, repo, files):
        for p, c in files.items():
            f = repo / p
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(c)
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "emails")

    def test_real_email_stops_without_allowlist(self, repo):
        self._write(repo, {"NOTICE": "contact: info@" + "team.acme-co.test\n"})
        res = run(repo, "0.1.0")
        assert res.returncode != 0 and "メールアドレス" in res.stderr

    def test_listed_email_passes(self, repo):
        self._write(repo, {
            "NOTICE": "contact: info@" + "team.acme-co.test\n",
            ".release-allowed-emails": "# c\ninfo@" + "team.acme-co.test\n",
        })
        res = run(repo, "0.1.0")
        assert res.returncode == 0, res.stderr

    def test_other_address_on_same_domain_still_stops(self, repo):
        self._write(repo, {
            "docs/a.md": "info@" + "team.acme-co.test / bob@" + "team.acme-co.test\n",
            ".release-allowed-emails": "info@" + "team.acme-co.test\n",
        })
        res = run(repo, "0.1.0")
        assert res.returncode != 0 and "bob@" in res.stderr and "info@" not in res.stderr

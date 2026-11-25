#!/usr/bin/env bash
# main をリリース時点のスナップショットとして作る。
#   scripts/release.sh <バージョン>      例: scripts/release.sh 0.1.0
# SRC_BRANCH の中身から .release-exclude の分を除いたツリーを
# TARGET_BRANCH(既定 main) の上に 1 コミットとして作り、注釈付きタグ v<バージョン> を付ける。
# 作業ツリーは切り替えず、一時 index で作る。push はしない。
# 作ったツリーに禁止パターンが残っていたら、コミットもタグも作らずに止める。
#
# 設定(いずれも公開側には含めない。.release-exclude に載せる):
#   .release-config             SRC_BRANCH=<ブランチ名> の行。環境変数 SRC_BRANCH が優先。
#                               どちらも無ければ現在のブランチを使う。
#   .release-forbidden-patterns 禁止パターン(拡張正規表現、1 行 1 件、# 始まりはコメント)。
#                               元ブランチのものを読む。無ければ組み込みの汎用パターンだけで検査する。
#   .release-allowed-emails     公開してよいメールアドレス(1 行 1 件、完全一致)。元ブランチのものを読む。
#                               無ければ example.com などのダミーだけを許容する。
# コミット・タグの作成者(必須): 環境変数 RELEASE_GIT_NAME と RELEASE_GIT_EMAIL の両方を指定する。
# どちらかでも未指定ならエラーで止める(コミットもタグも作らない)。
#   RELEASE_GIT_NAME=redmine-embedding RELEASE_GIT_EMAIL=release@example.com scripts/release.sh 0.1.0
set -euo pipefail

TARGET_BRANCH="${TARGET_BRANCH:-main}"
EXCLUDE_FILE_IN_TREE=".release-exclude"
PATTERNS_FILE_IN_TREE=".release-forbidden-patterns"
ALLOWED_EMAILS_FILE_IN_TREE=".release-allowed-emails"
CONFIG_FILE=".release-config"

die() { echo "エラー: $*" >&2; exit 1; }

[ $# -eq 1 ] || die "使い方: $0 <バージョン> (例 0.1.0)"
VERSION="$1"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "バージョン形式が不正です: $VERSION (X.Y.Z)"
TAG="v$VERSION"

git rev-parse --git-dir >/dev/null 2>&1 || die "git リポジトリではありません"
cd "$(git rev-parse --show-toplevel)"

if [ -z "${SRC_BRANCH:-}" ] && [ -f "$CONFIG_FILE" ]; then
  SRC_BRANCH="$(sed -n 's/^SRC_BRANCH[[:space:]]*=[[:space:]]*//p' "$CONFIG_FILE" | head -1 | sed 's/[[:space:]]*$//')"
fi
if [ -z "${SRC_BRANCH:-}" ]; then
  SRC_BRANCH="$(git symbolic-ref -q --short HEAD || true)"
fi
[ -n "$SRC_BRANCH" ] || die "元ブランチを決められません(SRC_BRANCH か $CONFIG_FILE を指定してください)"

if [ -z "${RELEASE_GIT_NAME:-}" ] || [ -z "${RELEASE_GIT_EMAIL:-}" ]; then
  die "RELEASE_GIT_NAME と RELEASE_GIT_EMAIL を両方指定してください(コミット・タグの作成者)。
  例: RELEASE_GIT_NAME=redmine-embedding RELEASE_GIT_EMAIL=release@example.com $0 $VERSION"
fi
export GIT_AUTHOR_NAME="$RELEASE_GIT_NAME" GIT_COMMITTER_NAME="$RELEASE_GIT_NAME"
export GIT_AUTHOR_EMAIL="$RELEASE_GIT_EMAIL" GIT_COMMITTER_EMAIL="$RELEASE_GIT_EMAIL"

# 追跡ファイルの変更のみ見る(未追跡・無視ファイルは対象外で、何も消さない)
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  die "作業ツリーに未コミットの変更があります"
fi
git rev-parse -q --verify "refs/heads/$SRC_BRANCH" >/dev/null || die "元ブランチがありません: $SRC_BRANCH"
git rev-parse -q --verify "refs/heads/$TARGET_BRANCH" >/dev/null || die "対象ブランチがありません: $TARGET_BRANCH"
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && die "タグが既に存在します: $TAG"

# 対象ブランチがどこかの作業ツリーで切り出されていると、update-ref で index と作業ツリーが食い違う
if git worktree list --porcelain | grep -qx "branch refs/heads/$TARGET_BRANCH"; then
  die "対象ブランチ $TARGET_BRANCH が作業ツリーで切り出されています。別のブランチに切り替えてから実行してください"
fi

SRC_HASH="$(git rev-parse "refs/heads/$SRC_BRANCH")"
TARGET_HASH="$(git rev-parse "refs/heads/$TARGET_BRANCH")"

# pyproject.toml の version と不一致ならエラー
PY_VER="$(git show "$SRC_HASH:pyproject.toml" 2>/dev/null | sed -n 's/^version *= *"\(.*\)"/\1/p' | head -1)"
[ -n "$PY_VER" ] || die "元ブランチの pyproject.toml から version を読めません"
if [ "$PY_VER" != "$VERSION" ]; then
  die "pyproject.toml の version ($PY_VER) と引数 ($VERSION) が違います"
fi

# 除外リスト(元ブランチのものを使う)
EXCLUDE_TEXT="$(git show "$SRC_HASH:$EXCLUDE_FILE_IN_TREE" 2>/dev/null)" || die "元ブランチに $EXCLUDE_FILE_IN_TREE がありません"
EXCLUDES=()
while IFS= read -r line; do
  line="${line%%#*}"; line="$(echo "$line" | sed 's/[[:space:]]*$//; s/^[[:space:]]*//')"
  [ -n "$line" ] && EXCLUDES+=("$line")
done <<<"$EXCLUDE_TEXT"
[ ${#EXCLUDES[@]} -gt 0 ] || die "$EXCLUDE_FILE_IN_TREE が空です"

TMP_INDEX="$(mktemp)"
trap 'rm -f "$TMP_INDEX"' EXIT
export GIT_INDEX_FILE="$TMP_INDEX"
git read-tree "$SRC_HASH"
RM_ARGS=()
for p in "${EXCLUDES[@]}"; do
  case "$p" in
    */) RM_ARGS+=("${p%/}") ;;
    *) RM_ARGS+=("$p") ;;
  esac
done
git rm -r -q --cached --ignore-unmatch -- "${RM_ARGS[@]}"
TREE="$(git write-tree)"
unset GIT_INDEX_FILE

# 禁止パターン(拡張正規表現)。公開ツリーに 1 件でもあれば止める。
# 組み込みは汎用のものだけ。固有の文字列は元ブランチの .release-forbidden-patterns に置く。
# このスクリプト自身も検査対象なので、パターンが自分に一致しないよう書く。
FORBIDDEN=(
  '#[0-9]{3,4}([^0-9]|$)'                                   # チケット番号
)
# 私設 IP(uv.lock のバージョン文字列と tests/ のダミー値は対象外)
IP_PATS=(
  '(^|[^0-9])10\.[0-9]+\.[0-9]+\.[0-9]+'
  '(^|[^0-9])192\.168\.[0-9]+\.[0-9]+'
  '(^|[^0-9])172\.(1[6-9]|2[0-9]|3[01])\.[0-9]+\.[0-9]+'
)
if PATTERNS_TEXT="$(git show "$SRC_HASH:$PATTERNS_FILE_IN_TREE" 2>/dev/null)"; then
  while IFS= read -r line; do
    line="$(echo "$line" | sed 's/[[:space:]]*$//')"
    case "$line" in ''|'#'*) continue ;; esac
    FORBIDDEN+=("$line")
  done <<<"$PATTERNS_TEXT"
fi
# メールアドレス(ダミー用のドメインは許容する)
EMAIL_PAT='[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}'
EMAIL_OK='@([A-Za-z0-9-]+\.)*(example(\.(com|org|net))?|users\.noreply\.github\.com)$'
# 公開してよいメールアドレス(元ブランチの .release-allowed-emails、1 行 1 件、# 始まりはコメント)。
# 完全一致のみ許容する(同じドメインの別アドレスは止める)。
ALLOWED_EMAILS=()
if ALLOWED_TEXT="$(git show "$SRC_HASH:$ALLOWED_EMAILS_FILE_IN_TREE" 2>/dev/null)"; then
  while IFS= read -r line; do
    line="$(echo "$line" | sed 's/[[:space:]]*$//; s/^[[:space:]]*//')"
    case "$line" in ''|'#'*) continue ;; esac
    ALLOWED_EMAILS+=("$line")
  done <<<"$ALLOWED_TEXT"
fi
is_allowed_email() { # is_allowed_email <git grep -o の 1 行(パス:行:アドレス)>
  local addr="${1##*:}" a
  for a in ${ALLOWED_EMAILS[@]+"${ALLOWED_EMAILS[@]}"}; do
    [ "$addr" = "$a" ] && return 0
  done
  return 1
}

# git grep の終了コード: 0=該当あり、1=該当なし、2 以上=エラー(検査できていないので止める)
# 第 2 引数以降は git grep に渡す。除外するパスは SCAN_EXCLUDES(pathspec の配列)で指定する
scan() { # scan <パターン> -> 該当行を標準出力、エラーなら die
  local rc=0 out
  out="$(git grep -nIE "${@:2}" -e "$1" "$TREE" -- . ${SCAN_EXCLUDES[@]+"${SCAN_EXCLUDES[@]}"} 2>&1)" || rc=$?
  if [ "$rc" -ge 2 ]; then
    die "git grep が失敗しました(終了コード $rc): $1: $out"
  fi
  [ "$rc" -eq 0 ] && printf '%s\n' "$out"
  return 0
}
HITS=""
SCAN_EXCLUDES=()
for pat in "${FORBIDDEN[@]}"; do
  out="$(scan "$pat")"
  [ -n "$out" ] && HITS+="[$pat]"$'\n'"$(echo "$out" | head -5)"$'\n'
done
SCAN_EXCLUDES=(':!uv.lock' ':!tests')
for pat in "${IP_PATS[@]}"; do
  out="$(scan "$pat")"
  [ -n "$out" ] && HITS+="[$pat]"$'\n'"$(echo "$out" | head -5)"$'\n'
done
SCAN_EXCLUDES=(':!uv.lock')
out="$(scan "$EMAIL_PAT" -o | { grep -vE "$EMAIL_OK" || true; } | while IFS= read -r l; do is_allowed_email "$l" || printf '%s\n' "$l"; done)"
[ -n "$out" ] && HITS+="[メールアドレス]"$'\n'"$(echo "$out" | head -5)"$'\n'
if [ -n "$HITS" ]; then
  printf '%s' "$HITS" >&2
  die "公開ツリーに禁止パターンが残っています(上記)。除去してからやり直してください"
fi

# コミット・タグのメッセージに元ブランチ名・除外一覧などは入れない(画面にだけ出す)
NEW="$(git commit-tree "$TREE" -p "$TARGET_HASH" -m "リリース $TAG")"
# タグを先に作る。ブランチの更新が失敗したらタグを消す(main だけ進む・タグだけ残る状態を作らない)
git tag -a "$TAG" -m "リリース $TAG" "$NEW"
if ! git update-ref "refs/heads/$TARGET_BRANCH" "$NEW" "$TARGET_HASH"; then
  git tag -d "$TAG" >/dev/null
  die "$TARGET_BRANCH の更新に失敗しました(タグ $TAG も取り消しました)"
fi

echo "作成しました: $TARGET_BRANCH = $NEW, タグ $TAG (元 $SRC_BRANCH $SRC_HASH)"
echo "公開するとき(人が実行):"
echo "  git push <remote> $TARGET_BRANCH $TAG"

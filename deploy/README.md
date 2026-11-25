# deploy/ — 配備用サンプル設定

そのままは動かない**例**です。`/opt/redmine-embedding`、ユーザー名 `redmine-embedding`、ポート 8001、パス `/mcp-search`、`--env prod` を自環境に合わせて書き換えてください。
DB・モデル取得・Redmine OAuth アプリ登録などの構築手順はこのファイルでは扱わない。

| ファイル | 役割 | 置き場所 |
|---|---|---|
| `systemd/redmine-embedding-mcp.service` | MCP サーバー（`redmine-embedding-mcp`）の常駐 | `/etc/systemd/system/` |
| `systemd/redmine-embedding-sync.service` | 同期バッチ（`redmine-embedding-batch sync`）の 1 回実行。flock で多重起動防止 | 同上 |
| `systemd/redmine-embedding-sync.timer` | 上記を夜間 1 回（既定 03:00）起動 | 同上 |
| `apache/redmine-embedding.conf` | Apache のリバースプロキシ例 | 443 の VirtualHost 内に追記 |
| `nginx/redmine-embedding.conf` | nginx のリバースプロキシ例 | 443 の server 内に追記 |

## 書き換える箇所
- インストール先・実行ユーザー: 各 `.service` の `User` / `WorkingDirectory` / `EnvironmentFile` / `ExecStart`
- 環境ファイル（git に入れない）。MCP 用と同期用を分ける（下の「環境ファイル」参照）
- 設定ファイル `config/settings.prod.json`（配布物に含まれないので `config/settings.example.json` をコピーして自分で作る）: `mcp_public_base_url`（外部から見える https のオリジン）、`mcp_public_path`、`mcp_port`（プロキシの転送先と一致させる）
- 実行時刻: timer の `OnCalendar`
- プロキシ: 転送先ポートと `/mcp-search`（`mcp_public_path`）。既存 MCP と同じホストで `/mcp` を使うときは、前置一致の注意（各ファイルのコメント）に従う
- `HF_HUB_OFFLINE=1`: モデルを事前取得してから有効にする（初回取得だけ 0）

## 環境ファイル（秘密情報）
- サンプルは `/etc/redmine-embedding/mcp.env`（MCP）と `/etc/redmine-embedding/sync.env`（同期）。`EnvironmentFile=` は systemd（root）が読むので、`root:root` の `0600` でよい（`sudo install -d -m 0750 /etc/redmine-embedding`）。
- 実行ユーザーが WorkingDirectory の `.env` を直接読む運用（pydantic-settings が `.env` を読む）にする場合は、`.env` の所有者を実行ユーザー（`redmine-embedding`）にして `0600`。root 所有の `0600` だと読めず起動に失敗する。
- MCP に必要: `DATABASE_URL`、`REDMINE_URL`、OAuth2 を使うなら `REDMINE_OAUTH_CLIENT_ID`、`REDMINE_OAUTH_CLIENT_SECRET`、`MCP_OAUTH_ENCRYPTION_KEY`。`REDMINE_API_KEY` は MCP は使わない。
- 同期に必要: `DATABASE_URL`、`REDMINE_URL`、`REDMINE_API_KEY`（**管理者 API キー**。非公開データを読むため）。
- 1 つの `.env` を MCP と同期で共用すると、MCP プロセスに管理者 API キーが渡る。最小権限のため分けること。例:

```
# /etc/redmine-embedding/mcp.env      （root:root 0600）
DATABASE_URL=postgresql://...
REDMINE_URL=https://redmine.example.com
REDMINE_OAUTH_CLIENT_ID=...
REDMINE_OAUTH_CLIENT_SECRET=...
MCP_OAUTH_ENCRYPTION_KEY=...
# /etc/redmine-embedding/sync.env     （root:root 0600）
DATABASE_URL=postgresql://...
REDMINE_URL=https://redmine.example.com
REDMINE_API_KEY=...
```
- `DATABASE_URL` は URL 形式のほか、libpq のキーワード形式（`host=... dbname=...`）も受け付ける。

## 手動実行と排他
timer 起動の sync は systemd が二重起動しない。手動実行は `sudo systemctl start redmine-embedding-sync.service` が簡単。引数を変えたいときに `redmine-embedding-batch sync` を直接実行するなら、同じロックを使う。ロック用ディレクトリは sync.service の `RuntimeDirectory=` が作り、`RuntimeDirectoryPreserve=yes` によりサービス停止後も残るが、sync.service を一度も起動していないうちは無いので、先に `sudo install -d -o redmine-embedding -g redmine-embedding -m 0755 /run/redmine-embedding` で作り、環境変数（sync.env）も渡す。具体的なコマンドは `docs/install.md` §7.2。
本番の配備前の確認は `redmine-embedding-batch --env prod check --require-oauth`。`--require-oauth` を付けると `mcp_public_base_url` 未設定（OAuth2 無効）を NG にする（本番は OAuth2 必須）。付けない場合は WARN 表示のみ。

## 有効化
`sudo systemctl daemon-reload && sudo systemctl enable --now redmine-embedding-mcp redmine-embedding-sync.timer`

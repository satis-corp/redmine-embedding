# redmine-embedding

[English](README.md) | 日本語

Redmine のチケット・wiki などをベクトル化し、Claude 等の MCP クライアントから類似検索できるようにする。


- `redmine_embedding.batch` : Redmine REST API からデータを取得し、埋め込みを作成して pgvector に同期するバッチ
- `redmine_embedding.mcp_server` : OAuth2 でユーザーの閲覧可能プロジェクトを判定し、ベクトル検索結果を返す MCP サーバー
- `redmine_embedding.common` : 両者で共通の設定・Redmine クライアント・埋め込み・DB アクセス

PyPI には公開していません。リリースタグ（例 `v0.1.0`）を指定して取得し、ソースからインストールします。

```
git clone <公開リポジトリの URL> redmine-embedding && cd redmine-embedding
git checkout v0.1.0
pip install ".[batch]"   # 同期バッチ
pip install ".[mcp]"     # MCPサーバー
```

サーバーへの配備（PostgreSQL + pgvector、Redmine 側の準備、systemd・リバースプロキシ、更新とロールバック）は [docs/install.md](docs/install.md) を参照。変更履歴は [CHANGELOG.md](CHANGELOG.md)。

## 設定ファイル

設定の基本は JSON。例外は「git に載せたくない項目」（`REDMINE_URL`、`REDMINE_API_KEY`、`DATABASE_URL`）で、これだけ環境変数（`export` または `.env`）で指定する。全項目と既定値は `config/settings.example.json`、意味と範囲は `src/redmine_embedding/common/settings.schema.json`。

```
redmine-embedding-batch --env prod sync                # config/settings.prod.json（自分で作る）を使う
redmine-embedding-batch --config /path/to/x.json sync  # パス指定
echo prod > config/active                              # 引数を省略したときに使う環境名（またはパス）
```

- 使うファイルの優先順位: `--config` / `--env` > `config/active` > `config/settings.json`（最後だけ無くても既定値で動く）。
- 例外の 3 項目は JSON に書けない（書くとエラー）。`export` と `.env` の両方にあれば `export` が優先（`.env.example` 参照）。`REDMINE_URL` に `user:pass@` は含められない（`.env` 側が不正なら export が正しくてもエラーになる）。
- JSON の項目は環境変数・`.env` では上書きできない（設定されていても無視し、警告を出す）。
- `config/` と `--config` の相対パスは実行時のカレントディレクトリ基準。cron / systemd では WorkingDirectory を指定するか絶対パスを使う。
- 環境別ファイル（`config/settings.<環境名>.json`、例 `config/settings.prod.json`）は配布物に含まれない。`config/settings.example.json`（全キーと既定値のリファレンス）をコピーして自分の環境用に作り、`--env <環境名>` で使う。`REDMINE_URL` などの例外 3 項目と秘密は JSON に書かない（公開 URL の `mcp_public_base_url` は JSON に書く）。
- `config/active`（環境ごとの切り替え指定）と `config/*.local.json`（手元用の一時上書き）はコミットしない。

PostgreSQL + pgvector の起動と DB テストの流し方は `docker/README.md`。

## License

Apache License 2.0（`LICENSE` と `NOTICE` を参照）。

開発: [satis-corp.com](https://satis-corp.com/)（連絡先: info@team.satis-corp.com）

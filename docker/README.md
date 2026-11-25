# docker/ — PostgreSQL + pgvector

`compose.yaml` は PostgreSQL 17 + pgvector 0.8.1（`pgvector/pgvector:0.8.1-pg17`）を 1 台起動する。
pgvector 0.8 以降が `hnsw.iterative_scan`（絞り込み検索で k 件取るため）に必要。

## 起動・停止

> 以下は**開発用**の手順（リポジトリ直下の `.env` を使う）。本番では、アプリがカレントの `.env` を読むため、`.env` を作らず、`/etc/redmine-embedding/docker.env` に同じ項目（`POSTGRES_*`）を置いて `--env-file /etc/redmine-embedding/docker.env` を指定する。手順は [docs/install.md](../docs/install.md) の §3 を参照。

```sh
cp .env.example .env        # POSTGRES_PASSWORD を自分で決めて設定（必須。既定値なし）
docker compose -f docker/compose.yaml --env-file .env up -d --wait
docker compose -f docker/compose.yaml --env-file .env down        # 停止（データは残る）
docker compose -f docker/compose.yaml --env-file .env down -v     # データも削除
```

- ポートは既定で `127.0.0.1:5432` のみ。変更は `.env` の `POSTGRES_BIND` / `POSTGRES_PORT`。
- データは名前付きボリューム `pgdata`。初回起動時に `postgres/init.sql` が `CREATE EXTENSION vector` を実行する。
- 接続 URL: `postgresql://<POSTGRES_USER>:<POSTGRES_PASSWORD>@127.0.0.1:<POSTGRES_PORT>/<POSTGRES_DB>`

## DB テスト

テストは `redmine_chunks` / `index_meta` / `sync_state` を **DROP する**。本番・運用中の DB に向けないこと。
テスト用に別の compose プロジェクト（別ボリューム）を立てる:

```sh
POSTGRES_PASSWORD=test-only POSTGRES_DB=redmine_embedding_test POSTGRES_PORT=55432 docker compose -p rembed-test -f docker/compose.yaml up -d --wait
TEST_DATABASE_URL=postgresql://redmine_embedding:test-only@127.0.0.1:55432/redmine_embedding_test uv run pytest
POSTGRES_PASSWORD=x docker compose -p rembed-test -f docker/compose.yaml down -v   # 必ず -p を付ける（付け忘れると本体のデータボリュームを消す）
```

`TEST_DATABASE_URL` 未設定だと `-m db` のテストは skip される。

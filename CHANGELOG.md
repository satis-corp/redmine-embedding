# 変更履歴

形式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) に準じ、バージョニングは [Semantic Versioning](https://semver.org/lang/ja/) に従う。
各リリースに「スキーマ変更の有無・戻し方の注意」の欄を設ける（更新前に DB を `pg_dump` し、問題があれば前のタグを再インストールして戻す）。

## [0.1.0] - 2026-11-25

初回リリース。

### 追加

- MCP 検索サーバー（`redmine_embedding.mcp_server`）: OAuth2 でユーザーの閲覧可能プロジェクトを判定し、ベクトル検索結果を返す。
- 同期バッチ（`redmine-embedding-batch sync`）: Redmine REST API からチケット・wiki・ファイル・添付・ニュース・バージョン・作業時間などを取得し、埋め込みを作成して pgvector に同期する。差分同期と `--full` による全件再読込。
- 削除の突き合わせ（`reconcile`）: アーカイブ済みプロジェクトや削除済みレコードのチャンクを整理する。既定は dry run で、削除の有効化は設定または `--apply-deletions` で明示する。
- `check` コマンド: 設定・DB・pgvector・スキーマ・Redmine API の事前確認（読み取りのみ）。`--require-oauth` で OAuth2 設定の欠落も NG にできる。
- `init-db` コマンド: pgvector のスキーマ作成。
- 添付ファイルの本文抽出（PDF・xlsx・テキスト）。
- JSON の設定ファイル（`config/settings.example.json`、スキーマは `src/redmine_embedding/common/settings.schema.json`）と、秘密情報のみ環境変数で渡す方式。
- `deploy/` に systemd・nginx・Apache の配備サンプル。
- `docker/` に PostgreSQL + pgvector の構成。

### スキーマ変更・戻し方の注意

- 初回リリースのため、スキーマ変更の比較対象はなし。
- 埋め込みモデルの切り替え（再埋め込み）は v0.1 では未対応。

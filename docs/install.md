# 構築手順

redmine-embedding の MCP サーバー（類似検索）と同期バッチを、1 台のサーバーに配備して検索できるようにするまでの手順です。例の URL・パス・ユーザー名は自環境に読み替えてください（以下、Redmine は `https://redmine.example.com`、MCP の公開 URL は `https://redmine.example.com/mcp-search`）。

## 0. 全体像

```
[MCP クライアント] --https--> [リバースプロキシ(Apache/nginx)] --> redmine-embedding-mcp (127.0.0.1:8001)
                                                                      |  OAuth2 でユーザー認証 / 権限判定は Redmine API
                                                                      v
[systemd timer] -> redmine-embedding-batch sync --(REST API, 管理者キー)--> Redmine
                                   |
                                   v
                          PostgreSQL + pgvector
```

- 同期バッチ: Redmine の REST API からデータを取り、埋め込みを作って pgvector に入れる（夜間 1 回など）。
- MCP サーバー: 検索リクエストをベクトル検索し、**ユーザーが Redmine で閲覧できるものだけ**を返す。ユーザー認証は Redmine の OAuth2。

## 1. 前提

| 項目 | 内容 |
|---|---|
| OS | Linux（systemd が使えるもの）。動作実績は Rocky Linux 10 / x86_64 |
| Python | 3.11 以上（実績は 3.12）。パッケージ管理は pip または uv |
| Docker | Docker Engine と Docker Compose（PostgreSQL + pgvector を同梱の `docker/compose.yaml` で動かすため）。導入は Docker 公式の手順（https://docs.docker.com/engine/install/）に従う。RHEL 系（Rocky / Alma）の例: `sudo dnf install dnf-plugins-core`（config-manager のため）→ `sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo` → `sudo dnf install docker-ce docker-ce-cli containerd.io docker-compose-plugin` → `sudo systemctl enable --now docker`（動作実績: Docker CE 29.10.0 / Compose v5.8.0） |
| PostgreSQL | リポジトリ同梱の pgvector コンテナ（PostgreSQL 17 + pgvector 0.8.1）を標準とします。pgvector **0.8 以上**が必要です（絞り込み検索で `hnsw.iterative_scan` を使うため） |
| メモリ | **4GB 以上を推奨**。同期でピーク約 1.5〜1.9GB（既定モデル `cl-nagoya/ruri-v3-130m`、CPU。データ量・環境で差がある）。Redmine と同居する場合は余裕を見る。1.7GB の機械では同期が OOM で落ちた |
| ディスク | Python 環境（CPU 版 torch 込み）約 1.4GB + モデル約 0.5GB + DB（DB サイズの目安は、現在利用中の Redmine の DB の 2〜3 倍程度。実測: Redmine の DB（MariaDB）53MB に対し埋め込み DB 125MB で約 2.4 倍。Redmine の添付ファイル本体（files ディレクトリ）は Redmine の DB サイズに含まれませんが、添付の本文も索引されるため、添付が多い環境では目安より大きくなりえます） |
| ネットワーク | 配備時に PyPI / PyTorch / Hugging Face に HTTPS で到達できること（オフライン運用は §5 参照）。サーバーから Redmine の REST API に到達できること |
| Redmine | OAuth2（Doorkeeper）が使えるバージョン。Redmine 7.0.2 で動作を確認しています。それより前のバージョンは未確認です |
| GPU | 不要（CPU で動作。実績は 1 コア） |

**コマンドの実行について**: 手順のコマンドは 1 ブロックずつ実行してください。`docker compose ... exec -T` など標準入力を読むコマンドは、複数行をまとめて貼り付けたり、スクリプトの標準入力（heredoc / `bash -s`）で流したりすると、後続の行を読み込んで消費してしまい実行されません。スクリプトにする場合は `exec -T` に `</dev/null` を付けてください（パイプで入力を渡す §12 の復元コマンドは除く）。

必要な権限: サーバーの root（または sudo。Docker を操作できること）、Redmine の管理者。PostgreSQL のスーパーユーザー（ホスト側）は不要です。

## 2. 取得とインストール

リリースは `main` ブランチのスナップショットとタグ（`vX.Y.Z`）で配布します。**必ずタグを指定して取得**してください（`main` の先頭ではなく）。

### 2.1 専用ユーザーと配置先

```
sudo useradd --system --home /opt/redmine-embedding --shell /usr/sbin/nologin redmine-embedding
sudo mkdir -p /opt/redmine-embedding && sudo chown redmine-embedding: /opt/redmine-embedding
```

### 2.2 取得

```
sudo -u redmine-embedding git clone --branch v0.1.0 --depth 1 <公開リポジトリの URL> /opt/redmine-embedding
```

公開リポジトリの URL は未定です（公開先が決まり次第、ここに記載します）。最初のリリースタグは `v0.1.0` です。

### 2.3 インストール（CPU 版 torch を先に入れる）

`sentence-transformers` は PyTorch に依存します。何も指定しないと CUDA 版（数 GB）が入ることがあるため、**CPU 版を先に入れます**。

```
cd /opt/redmine-embedding
sudo -u redmine-embedding python3 -m venv .venv
sudo -u redmine-embedding .venv/bin/pip install --upgrade pip
sudo -u redmine-embedding .venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
sudo -u redmine-embedding .venv/bin/pip install ".[batch,mcp]"
```

- 同期用と MCP 用を別のサーバーに分けるなら、`".[batch]"` / `".[mcp]"` をそれぞれ入れます。
- uv を使う場合の例: `uv venv && uv pip install torch --index-url https://download.pytorch.org/whl/cpu && uv pip install ".[batch,mcp]"`（`uv pip install` では別 index の torch と PyPI の他パッケージを同時に解決しないよう、torch を先に別コマンドで入れる）。
- 確認: `.venv/bin/redmine-embedding-batch --help` と `.venv/bin/redmine-embedding-mcp --help` が動くこと。
- 配布は、タグを指定して取得（§2.2）し、`pip install ".[...]"` でインストールする方式です。PyPI での公開は初回は行いません。

## 3. PostgreSQL + pgvector

リポジトリ同梱の pgvector コンテナ（`docker/compose.yaml`、PostgreSQL 17 + pgvector 0.8.1）を標準とします。pgvector 拡張はコンテナの初回起動時に自動で作られるため、**ホスト側の PostgreSQL スーパーユーザーは不要**です。

**サポート外**: 既存の PostgreSQL（Redmine と共用しているものなど）に pgvector を入れて使う構成は想定していません。Redmine の DB とは別に、このコンテナを使ってください。

### 3.1 コンテナの起動

コンテナ用の設定は、アプリが読む `.env` とは**別のファイル**（`/etc/redmine-embedding/docker.env`）に置きます。アプリはカレントディレクトリ（`/opt/redmine-embedding`）の `.env` を読むため、`.env.example` をコピーした `.env` を置くと、ダミーの `DATABASE_URL` などがアプリに読まれて原因の分かりにくい失敗になります。**`/opt/redmine-embedding/.env` は作らないでください**（環境変数が設定されていればそちらが優先されますが、混在を避けるため）。

```
sudo install -d -m 0750 /etc/redmine-embedding
sudo install -m 0600 /dev/null /etc/redmine-embedding/docker.env
sudoedit /etc/redmine-embedding/docker.env
```

`/etc/redmine-embedding/docker.env`（root:root 0600）:

```
POSTGRES_USER=redmine_embedding
POSTGRES_PASSWORD=<強いパスワード>
POSTGRES_DB=redmine_embedding
# POSTGRES_BIND=127.0.0.1
# POSTGRES_PORT=5432
```

```
sudo docker compose -f /opt/redmine-embedding/docker/compose.yaml --env-file /etc/redmine-embedding/docker.env up -d --wait
```

- `POSTGRES_PASSWORD` は必須です。ポートは既定で `127.0.0.1:5432` のみに公開されます。
- **ホストの 5432 が使用中の場合（Redmine と同じサーバーに置くと、Redmine の PostgreSQL が使っていることが多い）**、コンテナは起動に失敗します。`docker.env` の `POSTGRES_PORT` を空いているポート（例 `5433`）に変え、§3.2 と §6.3 の `DATABASE_URL` のポートも同じ値にしてください。
- データは名前付きボリューム `pgdata` に保存され、`docker compose ... down` で止めても残ります（`down -v` はデータも削除するので注意）。
- 詳細は `docker/README.md` を参照してください。以降、このコマンド列を `docker compose ...` と略記します（`-f` と `--env-file` は上と同じ）。

### 3.2 接続 URL

`DATABASE_URL`: `postgresql://redmine_embedding:<パスワード>@127.0.0.1:<POSTGRES_PORT>/redmine_embedding`（`docker.env` の `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_PORT` / `POSTGRES_DB` に合わせる。既定ポートは 5432。libpq のキーワード形式 `host=... dbname=...` も可）。パスワードに記号があれば URL エンコードする。

- `init-db`（§7）はテーブルと索引を作ります。pgvector 拡張はコンテナの初回起動時に作成済みなので、`init-db` の `CREATE EXTENSION IF NOT EXISTS` は何もしません。
- Docker のコンテナはホストの再起動後も `restart: unless-stopped` で自動的に起動します。

## 4. Redmine 側の準備

### 4.1 前提設定

Redmine の管理画面で、REST API の有効化（管理 → 設定 → API → 「RESTによるWebサービスを有効にする」）を確認します。OAuth2（Doorkeeper）は Redmine 7.0.2 で動作を確認しています。OAuth2 を使えるようにする操作（有効化の要否・場所）は、お使いの Redmine のバージョン・設定により異なる場合があります。画面名・メニュー名も環境により異なる場合があります。

### 4.2 バッチ用 API キー（同期用）

- 同期は**非公開データを読む必要がある**ため、専用の**管理者アカウント**を作り、そのアカウントの API キー（個人設定 → API アクセスキー）を使います（`REDMINE_API_KEY`）。
- 非公開チケット・非公開コメントはインデックスに入りません（`is_private` は常に false）。検索結果の閲覧可否は検索時に各ユーザーの OAuth トークンで Redmine に確認します。
- API キーは秘密情報です。§6 の環境ファイルにのみ書き、リポジトリ・ログ・チケットに書かないでください。

### 4.3 OAuth アプリの登録（MCP サーバー用）

管理 → **OAuth アプリケーション**（`https://redmine.example.com/oauth/applications`）→ 新規:

| 項目 | 値 |
|---|---|
| 名前 | 任意（例: redmine-embedding MCP） |
| Confidential | **オン**（クライアントシークレットを持つ） |
| リダイレクト URI | `<公開 URL><公開パス>/oauth/callback` = `https://redmine.example.com/mcp-search/oauth/callback` |
| スコープ | **同期する種別と一致させる**。`view_project` は必須。全種別（チケット・wiki・ニュース・作業時間・ファイル）を同期するなら、既定の 6 つすべて（`view_issues` / `view_wiki_pages` / `view_news` / `view_time_entries` / `view_files` / `view_project`）を付ける。設定 `mcp_oauth_scopes` と、アプリに付けるスコープを揃える |

- 登録後に表示される **クライアント ID とシークレット**を、§6 の `REDMINE_OAUTH_CLIENT_ID` / `REDMINE_OAUTH_CLIENT_SECRET` に書きます（シークレットは再表示できない場合があるので控える）。
- このアプリは本サーバー専用に作ります。ほかの MCP サーバー用のアプリと共用しないでください。

### 4.4 2FA 未設定ユーザーの初回ログインで 500 になる件（既知・回避策）

Redmine 7.0.2 では、**二要素認証（2FA）が「必須」設定で、かつ 2FA 未設定のユーザー**が OAuth の認可画面（`/oauth/authorize`）に来ると、Redmine 本体が HTTP 500 を返します（本サーバーの不具合ではなく、Redmine の `check_twofa_activation` が OAuth の画面から 2FA 設定画面へ遷移しようとして失敗するため）。

- 回避策: **初めて使うユーザーは、先に Redmine に通常ログインして 2FA を設定してから** MCP クライアントで接続する。500 になった場合も、2FA を設定した後に接続し直せば通る。
- 利用者への案内文に入れておくこと。

## 5. モデルの事前ダウンロードとオフライン運用

既定の埋め込みモデルは `cl-nagoya/ruri-v3-130m`（約 0.5GB、512 次元）で、Hugging Face から取得します。**サービスを動かす前に、実行ユーザーで取得しておきます**（systemd ユニットは `HF_HUB_OFFLINE=1` で実行時にネットワークを使わない設定）。

```
cd /opt/redmine-embedding
sudo -u redmine-embedding env HF_HOME=/opt/redmine-embedding/.cache/huggingface \
  .venv/bin/python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('cl-nagoya/ruri-v3-130m')"
```

- 実行ユーザーのキャッシュ（既定 `~/.cache/huggingface`、上のように `HF_HOME` で指定可）にモデルが置かれます。同期と MCP を**同じユーザー・同じ `HF_HOME`** で動かせば 1 回の取得で済みます。別ユーザーなら、それぞれで取得するかキャッシュを共有してください。`HF_HOME` を指定した場合は、systemd ユニットにも同じ `Environment=HF_HOME=...` を入れてください。
- `HF_HUB_OFFLINE=1` を付けないと、モデルがキャッシュ済みでも起動のたびに Hugging Face へ問い合わせます。名前解決が失敗する環境では起動に約 5.5 分かかります。**取得後は `HF_HUB_OFFLINE=1` を有効にする**のを推奨します。
- インターネットに出られないサーバー: 別の機械で上のコマンドを実行し、`$HF_HOME`（`hub/` 以下）をサーバーの同じ場所へコピーします（tar などで固めて持ち込む）。
- モデルは `embedding_model`（設定ファイル）で変えられますが、**インデックスはモデル・次元ごとに固有**です。作成後にモデルを変えると、起動時に不一致エラーで止まります（§10 参照）。

## 6. 設定

### 6.1 設定の置き場所（要点）

- 秘密と接続先の 3 項目（`REDMINE_URL` / `REDMINE_API_KEY` / `DATABASE_URL`）と OAuth の 3 項目だけ**環境変数**（環境ファイル）。それ以外は `config/` の JSON。詳細は [[設定ファイルの仕様]]。
- 配布物には環境別の設定ファイルは含まれません。`config/settings.example.json`（全項目と既定値）を元に、自環境の `config/settings.prod.json` を作ります。書く必要があるのは、既定値と変えたい項目だけです。

```
cd /opt/redmine-embedding
sudo -u redmine-embedding tee config/settings.prod.json >/dev/null <<'JSON'
{
  "redmine_request_interval": 0.2,
  "mcp_port": 8001,
  "mcp_public_base_url": "https://redmine.example.com",
  "mcp_public_path": "/mcp-search"
}
JSON
```

- `mcp_public_base_url` は外部から見える **https のオリジン**（パスなし）。空にすると OAuth2 が無効になり（開発用）、本番では使えません。
- `mcp_port` は既定 8000。Redmine 側の既存 MCP などが 8000 を使うなら変える。リバースプロキシの転送先と一致させる。
- `config/` は実行時のカレントディレクトリ基準です。systemd では `WorkingDirectory` を指定します（サンプル済み）。

### 6.2 必須項目

| 項目 | 書き場所 | 必須になる場面 | 例 |
|---|---|---|---|
| `DATABASE_URL` | 環境変数 | init-db / sync / reconcile / check / MCP | `postgresql://redmine_embedding:...@127.0.0.1:5432/redmine_embedding` |
| `REDMINE_URL` | 環境変数 | sync / reconcile / check / MCP | `https://redmine.example.com`（`user:pass@` は含められない） |
| `REDMINE_API_KEY` | 環境変数 | sync / reconcile / check（MCP は使わない） | 管理者 API キー（秘密） |
| `mcp_public_base_url` | 設定ファイル | MCP の OAuth2 モード（本番） | `https://redmine.example.com` |
| `mcp_public_path` | 設定ファイル | 任意（既定 `/mcp-search`） | `/mcp-search` |
| `REDMINE_OAUTH_CLIENT_ID` / `REDMINE_OAUTH_CLIENT_SECRET` | 環境変数 | MCP の OAuth2 モード | §4.3 の値（秘密） |
| `MCP_OAUTH_ENCRYPTION_KEY` | 環境変数 | MCP の OAuth2 モード | Fernet 鍵（下記で生成） |

上記以外の設定ファイル項目には既定値があり、必須ではありません（意味と範囲は `src/redmine_embedding/common/settings.schema.json` と [[設定ファイルの仕様]]）。

暗号鍵の生成（MCP が保存する上流トークンの暗号化用。**失うと保存済みトークンが読めなくなり、全ユーザーが再ログインになる**ため控えておく）:

```
/opt/redmine-embedding/.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 6.3 環境ファイル（秘密情報）

**MCP 用と同期用でファイルを分けます**（同期用の管理者 API キーを MCP のプロセスに渡さないため）。

```
sudo install -d -m 0750 /etc/redmine-embedding
sudo install -m 0600 /dev/null /etc/redmine-embedding/mcp.env
sudo install -m 0600 /dev/null /etc/redmine-embedding/sync.env
```

`/etc/redmine-embedding/mcp.env`（root:root 0600）:

```
DATABASE_URL=postgresql://redmine_embedding:<パスワード>@127.0.0.1:5432/redmine_embedding
REDMINE_URL=https://redmine.example.com
REDMINE_OAUTH_CLIENT_ID=<§4.3 のクライアント ID>
REDMINE_OAUTH_CLIENT_SECRET=<§4.3 のシークレット>
MCP_OAUTH_ENCRYPTION_KEY=<生成した Fernet 鍵>
```

`/etc/redmine-embedding/sync.env`（root:root 0600）:

```
DATABASE_URL=postgresql://redmine_embedding:<パスワード>@127.0.0.1:5432/redmine_embedding
REDMINE_URL=https://redmine.example.com
REDMINE_API_KEY=<管理者 API キー>
```

`EnvironmentFile=` は systemd（root）が読むので root 所有でよい。環境ファイルはリポジトリには入れません。**`/opt/redmine-embedding/.env`（WorkingDirectory の `.env`）は MCP・同期のどちらのプロセスにも読まれる**ので、通常は作らず、環境ファイルを使います。もし `.env` を使うなら、管理者 API キー（`REDMINE_API_KEY`）は書かないでください（MCP にも渡ってしまいます）。

### 6.4 設定チェック（`check`）

配備の前後に必ず実行します。値は表示されず、項目名と対処だけが出ます。外部への書き込みはしません。

環境ファイルは root 所有（0600）なので、**root 以外のシェルでも動くよう `sudo bash -c` の中で読み込みます**（確認用に sync.env と mcp.env の両方を読む）。

```
sudo bash -c 'set -a; . /etc/redmine-embedding/sync.env; . /etc/redmine-embedding/mcp.env; set +a
  cd /opt/redmine-embedding && sudo --preserve-env=DATABASE_URL,REDMINE_URL,REDMINE_API_KEY,REDMINE_OAUTH_CLIENT_ID,REDMINE_OAUTH_CLIENT_SECRET,MCP_OAUTH_ENCRYPTION_KEY -u redmine-embedding \
    .venv/bin/redmine-embedding-batch --env prod check --require-oauth'
```

- 確認項目: 設定ファイル / 環境変数 3 種の形式 / DB 接続 / pgvector 拡張とバージョン / スキーマ初期化済みか / Redmine API（`GET /users/current.json` 1 回で API キーの有効性）/ OAuth 設定（公開 URL、環境変数、暗号鍵の形式、https）。
- 各項目が OK / NG / SKIP で出て、**NG があれば終了コード 1**。DB 接続が NG のときは後続が SKIP になります。
- `--require-oauth` は、`mcp_public_base_url` 未設定（OAuth2 無効）を NG にします。**本番では付ける**。付けないと WARN（終了コード 0）です。OAuth2 を使わない開発用途では付けません。
- 必須項目が足りないままコマンドや MCP サーバーを起動すると、不足項目と書き場所の一覧を出して終了コード 2 で止まります（スタックトレースなし）。
- `init-db` の前は「スキーマ未初期化」が NG になります。次の §7 の後にもう一度実行してください。

## 7. DB の初期化と初回同期

環境ファイルは root 読み取りのため、root で読み込んでから実行ユーザーに渡します（root 以外のシェルでも動く形）。

```
sudo bash -c 'set -a; . /etc/redmine-embedding/sync.env; set +a
  cd /opt/redmine-embedding && sudo --preserve-env=DATABASE_URL,REDMINE_URL,REDMINE_API_KEY -u redmine-embedding \
    .venv/bin/redmine-embedding-batch --env prod init-db'
```

（`HF_HOME` を指定した場合は `HF_HOME` も渡す。`sudo --preserve-env` が使えない環境では、`sync.service` 経由で実行する。）

`init-db` は、テーブル `redmine_chunks` / `index_meta` / `sync_state` と HNSW インデックスを作り、使ったモデル名と次元を `index_meta` に記録します。何度実行しても安全です（既にあれば何もしない）。`sync` も内部で同じことをするので、省略しても構いません。`init-db` は `REDMINE_URL` / `REDMINE_API_KEY` が空でも成功するため、**設定漏れは `check` で確認**してください。

### 7.1 同期の所要時間と注意

- 初回は全件を読むため長くかかります。実績（1 コア・既定モデル）: 全種別の初回同期で約 3 時間 26 分。内訳はチケット約 81 分（2,542 件）、wiki 約 24 分（486 ページ）、添付ファイル約 79 分（3,796 件）、ファイル約 21 分（119 件）、作業時間・ニュース・バージョン・プロジェクトは各 1 分未満。**添付ファイルが多いと長くなります**。差分同期は、チケット・wiki だけなら変更なし〜数件で 16〜40 秒程度です。ただし**ファイル（Files）と、wiki・ニュースの添付ファイルは、変更がなくても毎回読み直す**（チケットの添付は、更新されたチケットの分だけ）ため、上と同じデータでは初回同期の次の差分同期も約 25 分かかりました（ファイル約 21 分、wiki・ニュースの添付約 3.5 分、チケット・wiki は数秒。件数が変わらなければ同程度）。夜間の定期実行の時間帯と間隔は、この所要時間を見込んで決めてください。所要時間は環境（データ量・マシン）次第です。
- **途中再開はできません**。全件同期が途中で止まると最初から読み直します（重複はしない）。
- 初回は systemd ユニットの `TimeoutStartSec=6h` でも足りない規模なら延ばしてください。

### 7.2 初回同期の実行（systemd）

§8 のユニットを置いた後に実行するのが簡単です。

```
sudo systemctl start redmine-embedding-sync.service     # 完了までブロックする。別ターミナルで進捗確認:
sudo journalctl -u redmine-embedding-sync -f
```

引数を変えて手動で実行する場合（`--type` や `--full` など）は、定期実行との多重起動を防ぐため同じロックを使います。ロック用ディレクトリ `/run/redmine-embedding/` は sync.service の `RuntimeDirectory=` が作ります（`RuntimeDirectoryPreserve=yes` なので、一度作られた後はサービスが止まっても残ります）。ただし sync.service を一度も起動していないうちは無いので、先に `install -d` で作ります（既にあっても問題ありません）。環境変数は §6.4 と同じく root で読み込んで渡します。

```
sudo install -d -o redmine-embedding -g redmine-embedding -m 0755 /run/redmine-embedding
sudo bash -c 'set -a; . /etc/redmine-embedding/sync.env; set +a; export HF_HUB_OFFLINE=1
  cd /opt/redmine-embedding && sudo --preserve-env=DATABASE_URL,REDMINE_URL,REDMINE_API_KEY,HF_HUB_OFFLINE -u redmine-embedding \
    flock -n /run/redmine-embedding/sync.lock .venv/bin/redmine-embedding-batch --env prod sync --type wiki'
```

（`--type wiki` は例。`--type` を付けなければ全種別を同期します。`--full` などは §7.3。`HF_HUB_OFFLINE=1` はモデルを §5 で取得済みの場合。初回のモデル取得を同期で行うなら外す。`HF_HOME` を指定した場合は、`--preserve-env=` の一覧と `export` にも `HF_HOME` を加える。）

### 7.3 その他のコマンド

| コマンド | 内容 |
|---|---|
| `sync` | 差分同期（既定。先に reconcile も実行） |
| `sync --full` | 前回同期時刻を無視して全件読み直し |
| `sync --type issue --type wiki` | 種別を絞る（`issue` `wiki` など。繰り返し可） |
| `sync --skip-reconcile` | 削除確認（reconcile）を飛ばす |
| `reconcile` | アーカイブ・削除されたデータのチャンクを探す（**既定は dry-run**） |
| `-v` | 詳細ログ（`-v sync` の形で先頭に付ける） |

削除の反映（`--apply-deletions` または設定 `reconcile_apply_deletions: true`）は、dry-run の内容を確認してから有効にしてください。

## 8. systemd と定期実行

`deploy/` のサンプルを `/etc/systemd/system/` にコピーし、書き換えて使います（`/opt/redmine-embedding`・ユーザー名 `redmine-embedding`・`--env prod` が既定の例）。

```
cd /opt/redmine-embedding
sudo cp deploy/systemd/redmine-embedding-mcp.service \
        deploy/systemd/redmine-embedding-sync.service \
        deploy/systemd/redmine-embedding-sync.timer /etc/systemd/system/
sudoedit /etc/systemd/system/redmine-embedding-mcp.service   # User / WorkingDirectory / EnvironmentFile / ExecStart
sudo systemctl daemon-reload
sudo systemctl enable --now redmine-embedding-mcp redmine-embedding-sync.timer
```

| ユニット | 役割 |
|---|---|
| `redmine-embedding-mcp.service` | MCP サーバーの常駐。起動に約 6〜13 秒（モデル読み込み。キャッシュ済みの場合の実測。初回やディスクが遅い環境では 30〜40 秒かかることもある）。`Restart=on-failure` |
| `redmine-embedding-sync.service` | 同期の 1 回実行（oneshot）。`flock -n` で多重起動を防止 |
| `redmine-embedding-sync.timer` | 毎日 03:00 に起動（`OnCalendar` で変更。`Persistent=true`、最大 10 分ずらす） |

- 書き換える箇所: `User` / `WorkingDirectory` / `EnvironmentFile` / `ExecStart`、`HF_HUB_OFFLINE=1`（モデル取得後に有効）、`HF_HOME`（指定した場合）、`OnCalendar`。
- メモリが小さいサーバーでは、同期と MCP を同時に動かさないよう時刻を調整してください（同期のピーク 約 1.5〜1.9GB + MCP のモデル）。
- 次回実行と状態: `systemctl list-timers redmine-embedding-sync.timer` / `systemctl status redmine-embedding-mcp` / `sudo journalctl -u redmine-embedding-mcp -f`（トークン・シークレットはログに出ません）。

## 9. リバースプロキシ

MCP サーバーは `127.0.0.1:8001`（`mcp_port`）で待ち受けます。**TLS（https）はリバースプロキシで終端**し、`<公開 URL><公開パス>` を転送します。サンプルは `deploy/apache/redmine-embedding.conf`、`deploy/nginx/redmine-embedding.conf`。既存の Redmine 用 443 の VirtualHost / server 内に追記します。

転送が必要なパス:

| パス | 内容 |
|---|---|
| `/mcp-search` 配下 | MCP エンドポイント、および `authorize` / `token` / `register` / `revoke` / `consent` / `oauth/callback`（1 行で足りる） |
| `/.well-known/oauth-protected-resource/mcp-search` | OAuth メタデータ（RFC 9728） |
| `/.well-known/oauth-authorization-server/mcp-search` | OAuth メタデータ（RFC 8414） |

Apache の例（要点。全文はサンプル参照）:

```
ProxyPass /mcp-search http://127.0.0.1:8001/mcp-search flushpackets=on timeout=3600
ProxyPass /.well-known/oauth-protected-resource/mcp-search http://127.0.0.1:8001/.well-known/oauth-protected-resource/mcp-search
ProxyPass /.well-known/oauth-authorization-server/mcp-search http://127.0.0.1:8001/.well-known/oauth-authorization-server/mcp-search
SetEnvIf Request_URI "^/mcp-search" no-gzip=1
```

- ストリーミング応答（SSE）を使うので、バッファリングと圧縮は切り、長い読み取りタイムアウトを許します（サンプル済み）。
- **注意（前置一致）**: 同じホストに別の MCP サーバーがあり、`ProxyPass /mcp ...`（nginx なら `location /mcp`）を持つ場合、前置一致で `/mcp-search` もそちらに飲み込まれます。
  - Apache: `ProxyPass` は先に書いた行が優先。**本サンプルの行を、既存の `/mcp`・`/.well-known/oauth-*`・`/authorize` などより前に書く**。
  - nginx: 最長一致の prefix location が優先されるので `location /mcp-search` を書けば足りる（順序は問わない）。ただし `location /` や正規表現 location が優先される構成では想定どおりにならないので、反映後に確認する。
  - 回避の別案: `mcp_public_path` を `/mcp` で始まらない名前にする。
- 適用前に構文確認、反映は graceful / reload: `sudo apachectl configtest && sudo apachectl graceful`、`sudo nginx -t && sudo systemctl reload nginx`。
- Apache は `proxy`・`proxy_http` モジュールが必要です。

## 10. 動作確認

### 10.1 サーバー側（クライアントを使わない確認）

```
# 認証なしで 401（WWW-Authenticate に resource_metadata が付く）
curl -si -X POST https://redmine.example.com/mcp-search | head -5
# OAuth メタデータが 200
curl -s https://redmine.example.com/.well-known/oauth-protected-resource/mcp-search
curl -s https://redmine.example.com/.well-known/oauth-authorization-server/mcp-search
# 設定の最終チェック（§6.4）。環境変数を読み込んだ状態で。スキーマ初期化済み・Redmine API が OK になる
sudo bash -c 'set -a; . /etc/redmine-embedding/sync.env; . /etc/redmine-embedding/mcp.env; set +a
  cd /opt/redmine-embedding && sudo --preserve-env=DATABASE_URL,REDMINE_URL,REDMINE_API_KEY,REDMINE_OAUTH_CLIENT_ID,REDMINE_OAUTH_CLIENT_SECRET,MCP_OAUTH_ENCRYPTION_KEY -u redmine-embedding \
    .venv/bin/redmine-embedding-batch --env prod check --require-oauth'
# 同期済みの件数（例）
sudo docker compose -f /opt/redmine-embedding/docker/compose.yaml --env-file /etc/redmine-embedding/docker.env exec postgres \
  psql -U redmine_embedding redmine_embedding -c "SELECT source_type, count(*) FROM redmine_chunks GROUP BY 1"
# （POSTGRES_USER / POSTGRES_DB を変えた場合は読み替える）
```

### 10.2 MCP クライアントから検索

1. MCP クライアント（Claude など、リモート MCP + OAuth に対応したもの）に、リモート MCP サーバーとして `https://redmine.example.com/mcp-search` を追加する。
2. クライアントが自動登録（DCR）し、ブラウザで本サーバーの同意画面が開く → 「承認」→ Redmine のログイン（2FA 設定済みであること。§4.4）→ Redmine の許可画面 → クライアントに戻る。
3. クライアントのツール一覧に `search_redmine` が出る。自然文で検索し、チケット・wiki などが返ることを確認する（例:「ログイン画面のタイムアウトに関するチケット」）。
4. 権限の確認: 閲覧権限のないプロジェクトのデータが、そのユーザーの検索結果に出ないこと。

動作を確認しているのは Claude 系のクライアントです。ほかのクライアントでの接続可否や設定画面の手順は、クライアントにより異なります。

## 11. 更新手順

配備済みの環境を新しいリリースタグに更新します（DB やデータは消えません）。

```
# 0. DB のバックアップ（必ず先に。保存先は §12 参照。リポジトリ内に置かない）
sudo install -d -m 0700 /var/backups/redmine-embedding
sudo docker compose -f /opt/redmine-embedding/docker/compose.yaml --env-file /etc/redmine-embedding/docker.env exec -T postgres \
  pg_dump --clean --if-exists -U redmine_embedding redmine_embedding \
  | sudo sh -c 'umask 077; cat > /var/backups/redmine-embedding/backup-'"$(date +%Y%m%d)"'.sql'
sudo sh -c 'chmod 0600 /var/backups/redmine-embedding/backup-*.sql'   # グロブは root 側で展開する
# バックアップが最後まで取れたか確認する（パイプの途中で pg_dump が失敗しても上のコマンドはエラーにならない）。
# 「OK」が表示されなければ、更新に進まずにバックアップを取り直す。
# 末尾付近の行全体だけを見る（ファイル全体を探すと、本文データに同じ文字列があるとき途中で切れたダンプでも OK になる。
# pg_dump 17 は完了行の後に数行を足すので tail -n 20 で足りる）
sudo tail -n 20 /var/backups/redmine-embedding/backup-"$(date +%Y%m%d)".sql | grep -qx -e '-- PostgreSQL database dump complete' && echo OK

cd /opt/redmine-embedding
sudo -u redmine-embedding git fetch --tags
sudo -u redmine-embedding git tag -l 'v*' | tail                       # 更新先を選ぶ
sudo -u redmine-embedding git checkout v0.2.0
sudo -u redmine-embedding .venv/bin/pip install ".[batch,mcp]"
# 更新後の確認は §6.4 の check コマンドを実行する
sudo systemctl restart redmine-embedding-mcp    # 起動に 6〜13 秒（初回やディスクが遅いと 30〜40 秒）
```

- 手順 0 のバックアップには Redmine のデータが含まれるので、保存先の権限（0700 / 0600）を保つ。
- 更新前に、`CHANGELOG.md` でリリースの変更内容（設定項目の追加・変更、スキーマ変更の有無）を確認する。**設定ファイルは配布物に含まれない**ので、`config/settings.prod.json` はそのまま残る。新しい項目は既定値で動く。`config/settings.example.json` との差分を見ておく。
- 同期は次回の timer で新しいコードになる（実行中の同期は止めない。止めるなら `sudo systemctl stop redmine-embedding-sync.service`）。
- DB スキーマは `init-db`（同期・MCP 起動時にも実行）が `CREATE ... IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` で追加変更します。MCP は起動時に OAuth 用の表（`mcp_oauth_*`）も作ります。
- **モデルまたは次元は変えないでください**: インデックスにはモデル名と次元が記録されており、違うモデルで起動すると `IndexMismatchError`（不一致）で止まります。**モデルの切り替え（再埋め込み）は v0.1 ではサポート外**です。
- リリースごとの変更点（設定項目の追加・変更、スキーマ変更の有無）は、リポジトリ直下の `CHANGELOG.md` を見てください。

## 12. ロールバック

更新後に問題が出たら、前のタグに戻します。**更新の前に必ず `pg_dump` でバックアップを取っておいてください**（下記）。

```
cd /opt/redmine-embedding
sudo -u redmine-embedding git checkout v0.1.0      # 前のタグ
sudo -u redmine-embedding .venv/bin/pip install ".[batch,mcp]"
```

- コードだけ戻す場合は、最後に `sudo systemctl restart redmine-embedding-mcp` を実行する。**DB も復元する場合は、ここでは再起動せず、下の手順 4 で起動する**（止めた MCP が復元前に起動すると、DROP と競合します）。
- 設定ファイルと環境ファイルは配布物の外なので影響を受けません。
- **DB は自動では戻りません**。更新前のバックアップ（§11 の手順 0。`pg_dump --clean --if-exists` 形式なので、既存の表を削除してから作り直します）から戻します。
  1. サービスを止める: `sudo systemctl stop redmine-embedding-mcp redmine-embedding-sync.timer redmine-embedding-sync.service`
  2. 前のタグに戻す（上のコマンド。`systemctl restart` は実行しない）。
  3. DB を復元する:
     ```
     sudo cat /var/backups/redmine-embedding/backup-YYYYMMDD.sql | sudo docker compose -f /opt/redmine-embedding/docker/compose.yaml --env-file /etc/redmine-embedding/docker.env exec -T postgres \
       psql -v ON_ERROR_STOP=1 -U redmine_embedding -d redmine_embedding
     ```
  4. サービスを起動する: `sudo systemctl start redmine-embedding-mcp redmine-embedding-sync.timer`
  - バックアップが無い／復元に失敗したときの最終手段は全件再同期です。**手順 1 のとおりサービスを止めた状態で**、DB を作り直してから `sync`（初回同期と同じ。数時間かかる）を実行します。
    ```
    sudo docker compose -f /opt/redmine-embedding/docker/compose.yaml --env-file /etc/redmine-embedding/docker.env exec postgres psql -U redmine_embedding -d postgres \
      -c 'DROP DATABASE redmine_embedding' -c 'CREATE DATABASE redmine_embedding OWNER redmine_embedding'
    sudo docker compose -f /opt/redmine-embedding/docker/compose.yaml --env-file /etc/redmine-embedding/docker.env exec postgres psql -U redmine_embedding -d redmine_embedding -c 'CREATE EXTENSION IF NOT EXISTS vector'
    ```
    （MCP を使うユーザーのログイン情報（`mcp_oauth_*`）も消えるので、全員が再ログインになります。その後 §7 の `init-db` と初回同期を行う。コンテナごと消してよいなら `docker compose ... down -v` のあと §3.1 からやり直す方法もあります。）
- リリースごとの変更点・スキーマ変更の有無は、リポジトリ直下の `CHANGELOG.md` を見てください。
- 同期の `sync --full` は、DB の内容を検証したいときに使えます（重複はしません）。

### 12.1 サービスの停止・撤去

```
sudo systemctl disable --now redmine-embedding-mcp redmine-embedding-sync.timer
```

リバースプロキシの追記を戻し（変更前のバックアップを取っておく）、`configtest` → `graceful` / reload。OAuth 用の表を消すなら `DROP TABLE mcp_oauth_*`（5 表）、全データなら DB ごと削除します（元に戻せません）。

## 13. ディレクトリ構成（利用者向け）

| パス | 内容 |
|---|---|
| `src/redmine_embedding/batch/` | 同期バッチ（`redmine-embedding-batch`） |
| `src/redmine_embedding/mcp_server/` | MCP サーバー（`redmine-embedding-mcp`）。OAuth2 は `auth/` |
| `src/redmine_embedding/common/` | 設定・Redmine クライアント・埋め込み・DB の共通部分 |
| `config/` | 設定ファイル。`settings.example.json`（全項目と既定値）。自環境用 `settings.<環境>.json` をここに作る |
| `deploy/` | systemd ユニット・timer、Apache / nginx のサンプル（`deploy/README.md` に書き換え箇所） |
| `docker/` | PostgreSQL + pgvector のコンテナ定義（`compose.yaml`）。DB の標準構成（§3）。本番では設定を `/etc/redmine-embedding/docker.env` に置く（`docker/README.md` の `.env` は開発用） |
| `scripts/` | `release.sh`（リリース作成。メンテナー用。環境変数 `RELEASE_GIT_NAME` / `RELEASE_GIT_EMAIL` が必須で、未指定はエラー）、`probe_redmine_api.py`（Redmine API の確認用）など |
| `.env.example` | 環境変数の例。**開発用**（リポジトリ直下の `.env` にコピーして使う。コミットしない）。本番は §3.1・§6.3 の環境ファイル（`/etc/redmine-embedding/`）を使い、WorkingDirectory に `.env` は作らない |
| `tests/` | テスト（`uv run pytest`。DB テストは `TEST_DATABASE_URL` が要る） |

## 14. トラブルシューティング

| 症状 | 原因・対処 |
|---|---|
| コマンド・MCP が「必須項目が足りない」で終了コード 2 | 表示された項目を §6.2 の場所に設定する。`check` で確認 |
| 起動に 5 分以上かかる | モデルが未取得、またはオフライン指定なしで Hugging Face に接続できない。§5 |
| 同期が OOM（終了コード 137）で落ちる | メモリ不足。4GB 以上にするか、他のサービスと同時に動かさない |
| OAuth ログインで Redmine が 500 | §4.4（2FA 未設定ユーザー）。先に Redmine で 2FA を設定する |
| 認可後に callback まで戻らない / 404 | リダイレクト URI（§4.3）と `mcp_public_base_url` + `mcp_public_path` の一致、プロキシの転送パス（§9）、`/mcp` 前置一致の飲み込みを確認 |
| MCP が 401 になる（認証付き） | トークン期限切れ → クライアントが再認可する。続くなら `check --require-oauth` で OAuth 設定を確認 |
| 設定ファイルの値が効かない | JSON の項目を環境変数・`.env` に書いても無視される（警告が出る）。逆に 3 項目は JSON に書けない |


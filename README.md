# redmine-embedding

[English](README.md) | [日本語](README.ja.md)

An open-source tool that vectorizes Redmine issues, wikis and more, so that MCP clients such as Claude can run similarity search over them.

- `redmine_embedding.batch`: a batch job that fetches data through the Redmine REST API, creates embeddings and syncs them into pgvector
- `redmine_embedding.mcp_server`: an MCP server that determines, via OAuth2, which projects the user may view and returns vector search results
- `redmine_embedding.common`: settings, Redmine client, embedding and DB access shared by both

It is not published on PyPI. Check out a release tag (e.g. `v0.1.0`) and install from source.

```
git clone <public repository URL> redmine-embedding && cd redmine-embedding
git checkout v0.1.0
pip install ".[batch]"   # sync batch
pip install ".[mcp]"     # MCP server
```

For deployment to a server (PostgreSQL + pgvector, Redmine-side preparation, systemd and reverse proxy, updates and rollback), see [docs/install.md](docs/install.md) (in Japanese). The change history is in [CHANGELOG.md](CHANGELOG.md).

## Configuration

Settings are basically JSON. The exception is the items you do not want in git (`REDMINE_URL`, `REDMINE_API_KEY`, `DATABASE_URL`), which must be given as environment variables (`export` or `.env`). All items and their defaults are in `config/settings.example.json`; their meaning and ranges are in `src/redmine_embedding/common/settings.schema.json`.

```
redmine-embedding-batch --env prod sync                # uses config/settings.prod.json (create it yourself)
redmine-embedding-batch --config /path/to/x.json sync  # specify a path
echo prod > config/active                              # environment name (or path) used when no argument is given
```

- Precedence of the file used: `--config` / `--env` > `config/active` > `config/settings.json` (the last one may be absent; defaults are used).
- The three exceptional items cannot be written in JSON (doing so is an error). If both `export` and `.env` define them, `export` wins (see `.env.example`). `REDMINE_URL` must not contain `user:pass@` (if the `.env` value is invalid, it is an error even when the exported one is valid).
- JSON items cannot be overridden by environment variables or `.env` (they are ignored with a warning if set).
- Relative paths for `config/` and `--config` are resolved from the current working directory. For cron / systemd, set WorkingDirectory or use absolute paths.
- Per-environment files (`config/settings.<env>.json`, e.g. `config/settings.prod.json`) are not part of the distribution. Copy `config/settings.example.json` (reference with all keys and defaults), adapt it to your environment and use it with `--env <env>`. Do not put the three exceptional items or any secrets in JSON (the public URL `mcp_public_base_url` does go in JSON).
- Do not commit `config/active` (per-environment switch) or `config/*.local.json` (temporary local overrides).

For starting PostgreSQL + pgvector and running the DB tests, see `docker/README.md`.

## License

Apache License 2.0 (see `LICENSE` and `NOTICE`).

Developed by [satis-corp.com](https://satis-corp.com/). Contact: info@team.satis-corp.com

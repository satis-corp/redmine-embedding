"""``redmine-embedding-batch`` command line."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta
from typing import Any

from ..common.config import add_config_arguments, configure_from_args, get_settings
from ..common.requirements import missing_items, report_and_exit_code
from ..common.settings_file import SettingsFileError
from ..common.db import ChunkRepository, connect, init_schema
from ..common.embedding import create_embedder
from ..common.redmine.client import RedmineClient
from .reconcile import reconcile
from .sources import CONNECTORS, build_connectors
from .sync import sync_source


def _parser() -> argparse.ArgumentParser:
    deletions = argparse.ArgumentParser(add_help=False)
    deletions.add_argument(
        "--apply-deletions", action="store_true", default=None,
        help="really delete chunks of archived projects / deleted records "
        "(default: dry run, unless reconcile_apply_deletions is true in the settings file)",
    )
    p = argparse.ArgumentParser(prog="redmine-embedding-batch", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    add_config_arguments(p)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="create the pgvector schema (also done by sync)")
    c = sub.add_parser("check", help="pre-flight check: settings, DB, pgvector, schema, Redmine API (read-only; exit 1 on any NG)")
    c.add_argument("--require-oauth", action="store_true", help="treat missing OAuth2 setup (mcp_public_base_url) as NG (for production)")
    sub.add_parser(
        "reconcile", parents=[deletions], help="drop chunks of archived projects / deleted records (dry run by default)"
    )
    s = sub.add_parser("sync", parents=[deletions], help="sync Redmine data into pgvector")
    s.add_argument("--skip-reconcile", action="store_true", help="do not compare the index with Redmine first")
    s.add_argument("--full", action="store_true", help="ignore the last sync time and re-read everything")
    s.add_argument(
        "--type", dest="types", action="append", choices=sorted(CONNECTORS),
        help="record type to sync (repeatable; default: all)",
    )
    return p


def _connector_options(settings, repo=None) -> dict[str, dict[str, Any]]:
    limits = {"max_bytes": settings.attachment_max_bytes, "max_chars": settings.attachment_text_max_chars}
    options: dict[str, dict[str, Any]] = {"attachment": dict(limits), "file": dict(limits)}
    if repo is not None:
        # renamed wiki pages keep their old updated_on: also pick up titles not indexed yet
        options["wiki"] = {"indexed_ids": lambda: repo.list_source_ids("wiki")}
    return options


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        configure_from_args(args)
        settings = get_settings()
    except SettingsFileError as e:
        print(e, file=sys.stderr)
        return 2
    if args.command == "check":
        from .check import run_check

        return run_check(settings, require_oauth=args.require_oauth)
    code = report_and_exit_code(missing_items(settings, args.command), args.command)
    if code:
        return code

    embedder = create_embedder(settings)
    # autocommit: each repo.transaction() block is then a real, committed transaction.
    conn = connect(settings.database_url, autocommit=True)
    try:
        init_schema(conn, model_name=embedder.model_name, dimension=embedder.dimension)
        if args.command == "init-db":
            return 0

        repo = ChunkRepository(conn)
        failures = 0
        apply = settings.reconcile_apply_deletions if args.apply_deletions is None else args.apply_deletions
        with RedmineClient(
            settings.redmine_url,
            api_key=settings.redmine_api_key.get_secret_value(),
            request_interval=settings.redmine_request_interval,
            timeout=settings.redmine_timeout,
            max_retries=settings.redmine_max_retries,
        ) as client:
            project_ids = None
            if args.command == "reconcile" or not args.skip_reconcile:
                outcome = reconcile(client, repo, apply=apply)
                if args.command == "reconcile":
                    return 1 if outcome.aborted else 0
                project_ids = outcome.valid_project_ids or None  # empty = listing failed: no restriction
            for connector in build_connectors(
                client, args.types, project_ids=project_ids, options=_connector_options(settings, repo)
            ):
                result = sync_source(
                    connector,
                    repo=repo,
                    embedder=embedder,
                    full=args.full,
                    overlap=timedelta(minutes=settings.sync_overlap_minutes),
                    chunk_max_chars=settings.chunk_max_chars,
                    flush_size=settings.embed_flush_size,
                )
                failures += result.failures
        return 1 if failures else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

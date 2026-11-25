#!/usr/bin/env python3
"""Check which Redmine REST APIs this instance offers (read-only GETs).

    REDMINE_URL=https://redmine.example.com REDMINE_API_KEY=... \\
        uv run python scripts/probe_redmine_api.py [--archived-project ID] [--sample 5]

Prints a Markdown report with no secrets (API key, issue text) in it, so it is
safe to paste back. Answers these open questions:
  * which record types have an API (documents, forums, files, ...)
  * do archived projects show up
  * does the API expose is_private / private_notes (needed to keep private data out)
Use the admin account that the sync batch will use.
"""

from __future__ import annotations

import argparse
import os
import sys

from redmine_embedding.common.redmine.client import RedmineClient
from redmine_embedding.common.redmine.errors import RedmineError

rows: list[tuple[str, str, str]] = []


def record(check: str, result: str, note: str = "") -> None:
    rows.append((check, result, note))


def probe(client: RedmineClient, check: str, path: str, key: str | None = None, params: dict | None = None):
    """GET path; record 'OK (n items)' or the HTTP status. Returns the JSON or None."""
    try:
        data = client.get(path, params)
    except RedmineError as e:
        record(check, f"HTTP {e.status_code}" if e.status_code else "error", path)
        return None
    n = f" ({len(data[key])} items on first page)" if key and isinstance(data.get(key), list) else ""
    record(check, f"OK{n}", path)
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archived-project", type=int, help="id of an archived test project")
    ap.add_argument("--sample", type=int, default=5, help="projects to probe per-project APIs on")
    args = ap.parse_args()

    url, key = os.environ.get("REDMINE_URL"), os.environ.get("REDMINE_API_KEY")
    if not url or not key:
        sys.exit("set REDMINE_URL and REDMINE_API_KEY")

    with RedmineClient(url, api_key=key) as c:
        me = probe(c, "authentication (current user)", "/users/current.json")
        if me is None:
            print_report()
            return 1
        record("account is admin", str(me["user"].get("admin", "field absent")), "the sync batch needs an admin account")

        # --- projects by status (ProjectQuery filter, Redmine 5.1+) -----------
        by_status: dict[str, list[dict]] = {}
        for label, statuses in [("default", None), ("active(1)", [1]), ("closed(5)", [5]), ("archived(9)", [9])]:
            try:
                by_status[label] = c.list_projects(statuses=statuses)
                record(f"projects: {label}", f"{len(by_status[label])} projects", "/projects.json")
            except RedmineError as e:
                record(f"projects: {label}", f"HTTP {e.status_code}", "/projects.json")
        if "archived(9)" in by_status:
            record("archived projects returned by list", "yes" if by_status["archived(9)"] else "no",
                   "0 may also just mean there are none")

        if args.archived_project:
            pid = args.archived_project
            probe(c, f"archived project #{pid}: project", f"/projects/{pid}.json")
            probe(c, f"archived project #{pid}: issues", "/issues.json",
                  "issues", {"project_id": pid, "status_id": "*", "limit": 1})

        # --- issues: privacy fields ------------------------------------------------
        listing = probe(c, "issues (all statuses)", "/issues.json", "issues", {"status_id": "*", "limit": 25})
        if listing and listing["issues"]:
            sample = listing["issues"][0]
            record("issue list has is_private", str("is_private" in sample))
            detail = c.get(f"/issues/{sample['id']}.json", {"include": "journals"})["issue"]
            record("issue show has is_private", str("is_private" in detail),
                   "REQUIRED: the batch refuses to index without it")
            journals = detail.get("journals") or []
            if journals:
                record("journal has private_notes", str("private_notes" in journals[0]),
                       "REQUIRED to skip private comments")
            else:
                record("journal has private_notes", "not checked", "first issue had no journals")

        # --- global collections ---------------------------------------------------
        news = probe(c, "news", "/news.json", "news")
        if news and news["news"]:
            record("news item has description", str("description" in news["news"][0]), "body text for indexing")
            probe(c, "news item (with attachments)", f"/news/{news['news'][0]['id']}.json", None,
                  {"include": "attachments"})
        probe(c, "time entries", "/time_entries.json", "time_entries")
        probe(c, "search API", "/search.json", "results", {"q": "a", "limit": 1})

        # --- per-project APIs, on a sample incl. closed projects --------------------
        pool = by_status.get("active(1)", []) [: args.sample] + by_status.get("closed(5)", [])[:2]
        for p in pool:
            ident, tag = p["identifier"], f"project #{p['id']}"
            probe(c, f"{tag}: wiki index", f"/projects/{ident}/wiki/index.json", "wiki_pages")
            probe(c, f"{tag}: files", f"/projects/{ident}/files.json", "files")
            probe(c, f"{tag}: versions", f"/projects/{ident}/versions.json", "versions")
            docs = probe(c, f"{tag}: documents", f"/projects/{ident}/documents.json", "documents")
            if docs and docs["documents"]:
                d = docs["documents"][0]
                record(f"{tag}: document has description / updated_on",
                       f"{'description' in d} / {'updated_on' in d}", "created_on is the fallback for incremental sync")
                probe(c, f"{tag}: document (with attachments)", f"/documents/{d['id']}.json", None,
                      {"include": "attachments"})
            boards = probe(c, f"{tag}: boards (forums)", f"/projects/{ident}/boards.json", "boards")
            if boards and boards["boards"]:
                b = boards["boards"][0]
                probe(c, f"{tag}: board topics", f"/boards/{b['id']}.json", None)
                probe(c, f"{tag}: board messages", f"/projects/{ident}/boards/{b['id']}/topics.json", "topics")
                probe(c, f"{tag}: messages", "/messages.json", "messages", {"limit": 1})

        # --- attachments (text extraction source) -----------------------------------
        if listing and listing["issues"]:
            issue_id = listing["issues"][0]["id"]
            detail = probe(c, "issue with attachments", f"/issues/{issue_id}.json", None, {"include": "attachments"})
            atts = (detail or {}).get("issue", {}).get("attachments") or []
            if atts:
                probe(c, "attachment metadata", f"/attachments/{atts[0]['id']}.json", None)
                record("attachment has content_url", str("content_url" in atts[0]), "download path for extraction")
            else:
                record("attachment metadata", "not checked", f"issue #{issue_id} has no attachments")

    print_report()
    return 0


def print_report() -> None:
    print("| check | result | note |\n|---|---|---|")
    for check, result, note in rows:
        print(f"| {check} | {result} | {note} |")


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Selectively wipe one ingestion channel from a Graphiti group.

Reads every episode in the target group, classifies each by its
source_description shape, deletes the episodes matching the requested
channel via the MCP `delete_episode` tool. Optional Neo4j cleanup pass
to prune orphaned entities (requires direct Neo4j HTTP credentials —
the MCP doesn't expose raw cypher).

Channels — each value matches a source_description prefix shape:

  github-tickets        https://github.com/...
  email                 mid:<Message-ID>
  precompact-hook       claude-code-session://... [precompact ...]
  task-completed-hook   claude-code-session://... [task-completed ...]
  claude-conversation   claude-code-conversation://... [add_memory ...]
  claude-self-writes    any of the above three (precompact + task-completed + conversation)
  slack-permalinked     https://<workspace>.slack.com/...
  slack-opaque          slack:<channel>:<date>
  slack                 either Slack variant
  magento-module        file:///... ending with /
  folder-doc            file:///... not ending with /

Usage:
    python wipe_channel.py \\
        --url http://localhost:8765/mcp \\
        --group-id <project-id> \\
        --channel github-tickets \\
        [--dry-run] [--yes]

Optional — orphan entity cleanup pass after episodes are gone:
    [--neo4j-url http://localhost:7474]
    [--neo4j-user neo4j] [--neo4j-password-env NEO4J_PASSWORD]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Callable

# Allow `python wipe_channel.py` from anywhere — find graphiti_client beside us.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from graphiti_client import GraphitiClient, GraphitiError


def is_github_ticket(src: str) -> bool:
    return src.startswith("https://github.com/")


def is_email(src: str) -> bool:
    return src.startswith("mid:")


def is_precompact_hook(src: str) -> bool:
    # PreCompact and TaskCompleted both use claude-code-session:// — distinguish
    # via the bracketed suffix. PreCompact's suffix contains 'precompact'.
    return src.startswith("claude-code-session://") and "[precompact" in src


def is_task_completed_hook(src: str) -> bool:
    return src.startswith("claude-code-session://") and "[task-completed" in src


def is_claude_conversation(src: str) -> bool:
    return src.startswith("claude-code-conversation://")


def is_claude_self_write(src: str) -> bool:
    """Any Claude-self-write (precompact + task-completed + ad-hoc conversation)."""
    return src.startswith("claude-code-session://") or src.startswith("claude-code-conversation://")


def is_slack_permalink(src: str) -> bool:
    return src.startswith("https://") and ".slack.com/" in src


def is_slack_opaque(src: str) -> bool:
    return src.startswith("slack:")


def is_slack(src: str) -> bool:
    return is_slack_permalink(src) or is_slack_opaque(src)


def is_magento_module(src: str) -> bool:
    return src.startswith("file://") and src.endswith("/")


def is_folder_doc(src: str) -> bool:
    return src.startswith("file://") and not src.endswith("/")


CHANNELS: dict[str, Callable[[str], bool]] = {
    "github-tickets": is_github_ticket,
    "email": is_email,
    "precompact-hook": is_precompact_hook,
    "task-completed-hook": is_task_completed_hook,
    "claude-conversation": is_claude_conversation,
    "claude-self-writes": is_claude_self_write,
    "slack-permalinked": is_slack_permalink,
    "slack-opaque": is_slack_opaque,
    "slack": is_slack,
    "magento-module": is_magento_module,
    "folder-doc": is_folder_doc,
}


def fetch_all_episodes(client: GraphitiClient, group_id: str) -> list[dict]:
    """Walk all episodes in a group. Graphiti's get_episodes paginates via
    max_episodes — we ask for a large cap on a single call."""
    result = client.call_tool(
        "get_episodes",
        {"group_ids": [group_id], "max_episodes": 100000},
    )
    if isinstance(result, dict):
        eps = result.get("episodes") or []
        if isinstance(eps, list):
            return eps
    return []


def delete_episode(client: GraphitiClient, uuid: str) -> tuple[bool, str]:
    try:
        client.call_tool("delete_episode", {"uuid": uuid})
        return True, ""
    except GraphitiError as e:
        return False, str(e)


def prune_orphans(neo4j_url: str, user: str, password: str, group_id: str) -> int | None:
    """Run the orphan-entity cleanup cypher via Neo4j HTTP. Returns the count
    deleted, or None if the request failed."""
    creds = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    body = json.dumps({
        "statements": [{
            "statement": (
                "MATCH (e:Entity) WHERE e.group_id = $gid "
                "AND NOT EXISTS { MATCH (:Episodic)-[:MENTIONS]->(e) } "
                "DETACH DELETE e RETURN count(e) AS deleted"
            ),
            "parameters": {"gid": group_id},
        }]
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{neo4j_url.rstrip('/')}/db/neo4j/tx/commit",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {creds}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as e:
        print(f"WARN: orphan cleanup HTTP call failed: {e}", file=sys.stderr)
        return None
    errors = payload.get("errors") or []
    if errors:
        print(f"WARN: orphan cleanup cypher errored: {errors}", file=sys.stderr)
        return None
    # Returned count is in results[0].data[0].row[0]
    try:
        return int(payload["results"][0]["data"][0]["row"][0])
    except (KeyError, IndexError, TypeError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="Graphiti MCP URL")
    ap.add_argument("--group-id", required=True, help="Group to wipe FROM (e.g. lcd-mageos). Other groups are not touched.")
    ap.add_argument("--channel", required=True, choices=list(CHANNELS),
                    help="Which ingestion channel's episodes to delete")
    ap.add_argument("--dry-run", action="store_true", help="Preview only; do not delete")
    ap.add_argument("--yes", action="store_true", help="Skip confirmation prompt (use in non-interactive contexts)")
    # Optional Neo4j direct connection for orphan entity cleanup
    ap.add_argument("--neo4j-url", default=None,
                    help="Neo4j HTTP base URL (e.g. http://localhost:7474). If set, runs orphan-entity cleanup after episode delete.")
    ap.add_argument("--neo4j-user", default="neo4j")
    ap.add_argument("--neo4j-password-env", default="NEO4J_PASSWORD",
                    help="Env var holding the Neo4j password (read at runtime; never on the CLI). Default NEO4J_PASSWORD.")
    args = ap.parse_args()

    matcher = CHANNELS[args.channel]

    client = GraphitiClient(args.url)
    try:
        client.initialize()
    except GraphitiError as e:
        print(f"ERROR initializing MCP session: {e}", file=sys.stderr)
        return 1

    print(f"fetching episodes in group_id={args.group_id!r}...", file=sys.stderr)
    eps = fetch_all_episodes(client, args.group_id)
    print(f"  {len(eps)} total episode(s) in this group", file=sys.stderr)

    targets = [ep for ep in eps if matcher(ep.get("source_description") or "")]
    print(f"  {len(targets)} match channel={args.channel!r}")

    if args.dry_run:
        for ep in targets[:15]:
            print(f"  - {ep.get('name', '(unnamed)')} — {ep.get('source_description', '')[:80]}")
        if len(targets) > 15:
            print(f"  ... +{len(targets) - 15} more")
        print("\n(dry-run; no deletions performed)")
        return 0

    if not targets:
        print("nothing to do")
        return 0

    if not args.yes:
        print(f"\nAbout to DELETE {len(targets)} episode(s) from group_id={args.group_id!r}.")
        print("This cannot be undone. Re-ingest is possible (may incur cost).")
        confirm = input("Type 'wipe' to proceed: ").strip()
        if confirm != "wipe":
            print("aborted")
            return 0

    failed = 0
    for i, ep in enumerate(targets, 1):
        uuid = ep.get("uuid")
        if not uuid:
            failed += 1
            continue
        ok, err = delete_episode(client, uuid)
        if ok:
            if i % 25 == 0:
                print(f"  ... {i}/{len(targets)}", file=sys.stderr)
        else:
            failed += 1
            print(f"  ! {ep.get('name', uuid)}: {err}", file=sys.stderr)

    deleted = len(targets) - failed
    print(f"\ndeleted {deleted} episode(s); {failed} failed")

    # Optional orphan pass
    if args.neo4j_url:
        password = os.environ.get(args.neo4j_password_env)
        if not password:
            print(f"WARN: --neo4j-url given but ${args.neo4j_password_env} is empty; skipping orphan cleanup")
        else:
            orphans = prune_orphans(args.neo4j_url, args.neo4j_user, password, args.group_id)
            if orphans is None:
                print("WARN: orphan cleanup did not complete (see warnings above)")
            else:
                print(f"orphan entities removed: {orphans}")

    print("\nReminder: if you ran an ingest command earlier and intend to re-ingest")
    print("the same source, delete '.pb-graphiti-ingest.json' in that cwd (or pass")
    print("--reingest), otherwise the next run will skip everything as 'already up to date'.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

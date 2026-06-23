#!/usr/bin/env python3
"""
Ingest GitHub issues + pull requests (with comments) into Graphiti.

One episode per ticket. The episode body contains:
  - title
  - state, labels, author
  - body (the issue/PR description)
  - chronological comment thread (bot comments filtered out by default)

source_description is the ticket's html_url, so recalled facts cite back to
the exact GitHub thread.

Auth: relies on the `gh` CLI being authenticated (or $GH_TOKEN set). This
script shells out to `gh api --paginate` rather than reimplementing the
REST client; gh handles auth, pagination, rate-limit backoff for us.

Usage:
    python ingest_tickets.py \\
        --url http://localhost:8765/mcp \\
        --group-id <project-id> \\
        --repo owner/name \\
        --since 2024 \\
        [--state all|open|closed] \\
        [--include-labels 'bug,decision'] \\
        [--exclude-labels 'dependencies,duplicate'] \\
        [--min-comments 0] \\
        [--include-bots] \\
        [--dry-run] [--reingest]
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Iterable

from graphiti_client import GraphitiClient, GraphitiError

STATE_FILE = ".pb-graphiti-ingest.json"
MAX_EPISODE_CHARS = 30000  # Cap per-ticket body to keep embedding cost sane


def load_state(state_path: Path) -> dict[str, list[str]]:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text())
        except json.JSONDecodeError:
            print(f"WARN: corrupt state file {state_path}, starting fresh", file=sys.stderr)
    return {}


def save_state(state_path: Path, state: dict[str, list[str]]) -> None:
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True))


def gh_api(path: str, *extra_args: str) -> list[dict] | dict:
    """Shell out to `gh api --paginate <path> <extra-args>`. Returns parsed JSON.

    Raises CalledProcessError on non-zero exit. Caller should handle.
    """
    cmd = ["gh", "api", "--paginate", path, *extra_args]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    # gh --paginate joins consecutive JSON arrays. For single-object responses
    # it just emits the object.
    out = result.stdout.strip()
    if not out:
        return []
    return json.loads(out)


def parse_since(since_arg: str) -> datetime.datetime:
    """Accept YYYY (year only) or full ISO date. Returns UTC datetime at midnight."""
    if len(since_arg) == 4 and since_arg.isdigit():
        return datetime.datetime(int(since_arg), 1, 1, tzinfo=datetime.timezone.utc)
    # try full ISO
    try:
        return datetime.datetime.fromisoformat(since_arg.replace("Z", "+00:00"))
    except ValueError:
        # try YYYY-MM-DD
        return datetime.datetime.fromisoformat(f"{since_arg}T00:00:00+00:00")


def is_bot(user: dict | None) -> bool:
    if not user:
        return True
    if user.get("type") == "Bot":
        return True
    login = user.get("login", "")
    return login.endswith("[bot]") or login in {"github-actions", "dependabot", "codecov"}


def short_iso(s: str) -> str:
    if not s:
        return "?"
    return s.split("T", 1)[0]


def build_episode_body(
    ticket: dict, comments: list[dict], *, include_bots: bool
) -> tuple[str, str]:
    """Return (body, reference_time_iso)."""
    is_pr = "pull_request" in ticket
    kind = "PR" if is_pr else "issue"
    num = ticket["number"]
    title = ticket.get("title", "(no title)")
    state = ticket.get("state", "?")
    author = (ticket.get("user") or {}).get("login", "?")
    labels = [l.get("name", "") for l in ticket.get("labels", []) if l.get("name")]
    created = ticket.get("created_at", "")
    closed = ticket.get("closed_at")
    merged = ticket.get("pull_request", {}).get("merged_at") if is_pr else None
    body = (ticket.get("body") or "").strip()

    lines: list[str] = []
    lines.append(f"{kind} #{num}: {title}")
    lines.append(f"State: {state}" + (" (merged)" if merged else ""))
    lines.append(f"Author: @{author}")
    if labels:
        lines.append(f"Labels: {', '.join(labels)}")
    lines.append(f"Created: {short_iso(created)}" + (f", closed: {short_iso(closed)}" if closed else ""))
    lines.append(f"URL: {ticket.get('html_url', '')}")
    lines.append("")
    if body:
        lines.append("## Description")
        lines.append(body)
        lines.append("")

    # Filter + sort comments chronologically
    filtered = [
        c for c in comments
        if include_bots or not is_bot(c.get("user"))
    ]
    filtered.sort(key=lambda c: c.get("created_at", ""))
    if filtered:
        lines.append(f"## Comments ({len(filtered)})")
        for c in filtered:
            user = (c.get("user") or {}).get("login", "?")
            when = short_iso(c.get("created_at", ""))
            text = (c.get("body") or "").strip()
            if not text:
                continue
            lines.append(f"\n### @{user} ({when})")
            lines.append(text)

    body_text = "\n".join(lines).strip()
    if len(body_text) > MAX_EPISODE_CHARS:
        body_text = body_text[:MAX_EPISODE_CHARS] + f"\n\n[... truncated to {MAX_EPISODE_CHARS} chars from {len(body_text)} total ...]"
    return body_text, created or datetime.datetime.now(datetime.timezone.utc).isoformat()


def passes_label_gate(labels: list[str], include: set[str] | None, exclude: set[str] | None) -> bool:
    label_set = {l.lower() for l in labels}
    if include and not (label_set & {i.lower() for i in include}):
        return False
    if exclude and (label_set & {e.lower() for e in exclude}):
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--group-id", required=True, help="Use the project id (tickets are per-project)")
    ap.add_argument("--repo", required=True, help="owner/name, e.g. ITToolsAU/LaptopLCDScreen")
    ap.add_argument("--since", required=True, help="Year (YYYY) or ISO date — only ingest tickets created on/after")
    ap.add_argument("--state", default="all", choices=("open", "closed", "all"))
    ap.add_argument("--include-labels", default="", help="Comma-separated — only ingest tickets with at least one matching label")
    ap.add_argument("--exclude-labels", default="dependencies,duplicate,invalid,wontfix",
                    help="Comma-separated — drop tickets with any matching label (default skips dep updates, dupes, invalid, wontfix)")
    ap.add_argument("--min-comments", type=int, default=0,
                    help="Drop tickets with fewer than N (non-bot) comments — useful for skipping trivial bug reports")
    ap.add_argument("--include-bots", action="store_true",
                    help="Include comments from bot accounts (dependabot etc.). Default OFF.")
    ap.add_argument("--include-code-entities", action="store_true",
                    help="Allow Graphiti to extract file paths / class names as Component entities. Default OFF — code refs belong in GitNexus, not here.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reingest", action="store_true")
    ap.add_argument("--state-file", default=STATE_FILE)
    args = ap.parse_args()

    since_dt = parse_since(args.since)
    since_iso = since_dt.isoformat()
    include_labels = {l.strip() for l in args.include_labels.split(",") if l.strip()} or None
    exclude_labels = {l.strip() for l in args.exclude_labels.split(",") if l.strip()} or None

    state_path = Path(args.state_file).resolve()
    state = {} if args.reingest else load_state(state_path)
    seen = set(state.get(args.group_id, []))

    print(f"fetching issues+PRs from {args.repo} since {since_iso[:10]} (state={args.state})...", file=sys.stderr)
    try:
        # The /issues endpoint returns BOTH issues and PRs. Filter later if needed.
        tickets = gh_api(
            f"/repos/{args.repo}/issues",
            "-X", "GET",
            "-f", f"state={args.state}",
            "-f", f"since={since_iso}",
            "-f", "per_page=100",
        )
    except subprocess.CalledProcessError as e:
        print(f"ERROR: gh api failed: {e.stderr.strip()}", file=sys.stderr)
        return 2
    except FileNotFoundError:
        print("ERROR: `gh` CLI not found. Install it or run from a shell where it's on PATH.", file=sys.stderr)
        return 2

    if not isinstance(tickets, list):
        tickets = [tickets]

    # Filter by since (issues `since` param is updated_at; we want created_at strictly)
    tickets = [t for t in tickets if t.get("created_at", "") >= since_iso]

    print(f"  {len(tickets)} ticket(s) match initial filter", file=sys.stderr)

    plan: list[dict] = []
    skipped_label = 0
    skipped_comments = 0
    skipped_dedup = 0

    for t in tickets:
        num = t.get("number")
        if num is None:
            continue
        labels = [l.get("name", "") for l in t.get("labels", []) if l.get("name")]
        if not passes_label_gate(labels, include_labels, exclude_labels):
            skipped_label += 1
            continue

        # Fetch comments separately (the /issues listing doesn't include them)
        try:
            comments = gh_api(
                f"/repos/{args.repo}/issues/{num}/comments",
                "-X", "GET",
                "-f", "per_page=100",
            )
        except subprocess.CalledProcessError:
            comments = []
        if not isinstance(comments, list):
            comments = []

        non_bot_count = sum(1 for c in comments if not is_bot(c.get("user")))
        if non_bot_count < args.min_comments:
            skipped_comments += 1
            continue

        body, ref_time = build_episode_body(t, comments, include_bots=args.include_bots)
        h = hashlib.sha256(f"{args.repo}#{num}:{body[:500]}".encode("utf-8")).hexdigest()[:16]
        if h in seen:
            skipped_dedup += 1
            continue

        kind = "PR" if "pull_request" in t else "issue"
        plan.append({
            "hash": h,
            "name": f"{args.repo}#{num} ({kind}): {t.get('title', '')[:100]}",
            "body": body,
            "source_description": t.get("html_url", ""),
            "reference_time": ref_time,
        })

    summary_parts: list[str] = []
    if skipped_label:
        summary_parts.append(f"{skipped_label} by label")
    if skipped_comments:
        summary_parts.append(f"{skipped_comments} below --min-comments")
    if skipped_dedup:
        summary_parts.append(f"{skipped_dedup} dedup")
    summary = (" (filtered: " + ", ".join(summary_parts) + ")") if summary_parts else ""

    print(f"plan: {len(plan)} ticket(s) to write to group_id={args.group_id!r} from {args.repo}{summary}")
    if args.dry_run:
        for ep in plan[:15]:
            print(f"  - {ep['name']} ({len(ep['body'])} chars)")
        if len(plan) > 15:
            print(f"  ... +{len(plan) - 15} more")
        return 0

    if not plan:
        print("nothing to do (all up to date)")
        return 0

    client = GraphitiClient(args.url)
    try:
        client.initialize()
    except GraphitiError as e:
        print(f"ERROR initializing MCP session: {e}", file=sys.stderr)
        return 1

    # By default, suppress extraction of code references — tickets are full of
    # file paths and class names but those belong in GitNexus, not Graphiti.
    extract_kwargs: dict = {}
    if not args.include_code_entities:
        extract_kwargs["excluded_entity_types"] = ["Component"]
        extract_kwargs["custom_extraction_instructions"] = (
            "Extract proper-noun concepts: people / authors / contributors, "
            "vendor names, third-party services, business features, decisions "
            "with rationale, root causes of incidents, client/customer references. "
            "DO NOT extract as entities: PHP class names (anything resembling "
            "Magento\\Foo\\Bar or Vendor\\Module\\Class), file paths "
            "(*.php, *.xml, *.js, paths with slashes), function/method names, "
            "issue numbers as bare identifiers, or Magento core module names "
            "like 'Magento_Catalog'. Those code-structural references belong "
            "to the code-graph index (GitNexus), not the domain knowledge graph."
        )

    written = 0
    failed = 0
    for ep in plan:
        try:
            client.add_memory(
                group_id=args.group_id,
                name=ep["name"],
                episode_body=ep["body"],
                source="text",
                source_description=ep["source_description"],
                reference_time=ep["reference_time"],
                **extract_kwargs,
            )
            seen.add(ep["hash"])
            written += 1
            print(f"  + {ep['name']}")
            state[args.group_id] = sorted(seen)
            save_state(state_path, state)
        except GraphitiError as e:
            failed += 1
            print(f"  ! {ep['name']}: {e}", file=sys.stderr)

    print(f"\ndone: wrote {written}, failed {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

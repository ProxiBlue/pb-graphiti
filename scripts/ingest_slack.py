#!/usr/bin/env python3
"""
Ingest a Slack workspace export into Graphiti.

Input: the .zip Slack produces from "Workspace settings → Import/Export Data
→ Export". Layout inside the zip:

    channels.json       — channel metadata
    users.json          — user metadata (id → display name)
    <channel-name>/<YYYY-MM-DD>.json   — messages per channel per day

Episode shape: ONE episode per channel-per-day, format='message', with the
day's messages flattened into a single transcript ("HH:MM @user: text").
Threads are inlined under their parent post (indented).

Dedupe: every (channel, date) pair is recorded in
.pb-graphiti-ingest.json. Re-runs skip already-ingested days unless
--reingest is passed.

Usage:
    python ingest_slack.py \\
        --url http://localhost:8765/mcp \\
        --group-id fleet \\
        --export ./slack-export.zip \\
        [--channels '#engineering,#general'] [--since 2025-01-01] \\
        [--dry-run] [--reingest]
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

from graphiti_client import GraphitiClient, GraphitiError

STATE_FILE = ".pb-graphiti-ingest.json"


def load_state(state_path: Path) -> dict[str, list[str]]:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text())
        except json.JSONDecodeError:
            print(f"WARN: corrupt state file {state_path}, starting fresh", file=sys.stderr)
    return {}


def save_state(state_path: Path, state: dict[str, list[str]]) -> None:
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True))


def slack_permalink(workspace: str | None, channel_id: str | None, ts: str) -> str | None:
    """Build a Slack archive permalink for a single message. Returns None if missing pieces."""
    if not workspace or not channel_id or not ts:
        return None
    # Slack permalink format: https://<workspace>.slack.com/archives/<channel-id>/p<ts-no-dot>
    return f"https://{workspace}.slack.com/archives/{channel_id}/p{ts.replace('.', '')}"


def format_message(
    msg: dict,
    user_map: dict[str, str],
    *,
    workspace: str | None = None,
    channel_id: str | None = None,
    min_words: int = 0,
    include_users: set[str] | None = None,
    exclude_users: set[str] | None = None,
) -> str | None:
    """Render one slack message as 'HH:MM @user: text [permalink]'. Returns None for noise or filtered messages.

    Filters applied in order:
    - subtype-based noise (channel_join/leave/topic/purpose)
    - empty body
    - user filters (include_users wins if both set)
    - min_words (counts whitespace-split tokens in body)
    """
    if msg.get("subtype") in {"channel_join", "channel_leave", "channel_topic", "channel_purpose"}:
        return None
    text = msg.get("text", "")
    if not text:
        return None

    user_id = msg.get("user") or msg.get("bot_id") or "unknown"
    user = user_map.get(user_id, user_id)
    user_keys = {user_id, user}
    if include_users and not (user_keys & include_users):
        return None
    if exclude_users and (user_keys & exclude_users):
        return None

    if min_words and len(text.split()) < min_words:
        return None

    ts = msg.get("ts", "0")
    try:
        when = datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc)
        time_str = when.strftime("%H:%M")
    except (ValueError, OSError):
        time_str = "??:??"

    link = slack_permalink(workspace, channel_id, ts)
    suffix = f" [{link}]" if link else ""
    return f"{time_str} @{user}: {text}{suffix}"


def render_day(
    messages: list[dict],
    user_map: dict[str, str],
    *,
    workspace: str | None = None,
    channel_id: str | None = None,
    min_words: int = 0,
    include_users: set[str] | None = None,
    exclude_users: set[str] | None = None,
) -> tuple[str, int]:
    """Render a day's messages with threads inlined under parents.

    Returns (rendered_text, surviving_message_count). The count is used by
    callers to apply per-day thresholds (e.g., --min-day-messages).
    """
    threads: dict[str | None, list[dict]] = defaultdict(list)
    for m in messages:
        tts = m.get("thread_ts")
        if tts and tts != m.get("ts"):
            threads[tts].append(m)
        else:
            threads[None].append(m)

    lines: list[str] = []
    surviving = 0
    fmt_kwargs = dict(
        workspace=workspace, channel_id=channel_id,
        min_words=min_words, include_users=include_users, exclude_users=exclude_users,
    )
    for parent in sorted(threads[None], key=lambda m: float(m.get("ts", "0"))):
        rendered = format_message(parent, user_map, **fmt_kwargs)
        if not rendered:
            continue
        lines.append(rendered)
        surviving += 1
        replies = sorted(threads.get(parent.get("ts"), []), key=lambda m: float(m.get("ts", "0")))
        for reply in replies:
            r = format_message(reply, user_map, **fmt_kwargs)
            if r:
                lines.append(f"  └─ {r}")
                surviving += 1
    return "\n".join(lines), surviving


def _parse_csv(s: str) -> set[str]:
    return {x.strip().lstrip("#@") for x in s.split(",") if x.strip()}


def passes_keyword_gate(
    text: str,
    *,
    include: set[str] | None = None,
    exclude: set[str] | None = None,
) -> bool:
    """Day-level content gate. Lowercase substring match — cheap and predictable."""
    lower = text.lower()
    if include and not any(k.lower() in lower for k in include):
        return False
    if exclude and any(k.lower() in lower for k in exclude):
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="Graphiti MCP URL (e.g. http://localhost:8765/mcp)")
    ap.add_argument("--group-id", required=True)
    ap.add_argument("--export", required=True, help="Path to slack-export.zip")
    ap.add_argument("--channels", default="", help="Comma-separated channel names to include (default: all)")
    ap.add_argument("--since", default=None, help="ISO date — skip days before this (e.g. 2025-01-01)")
    ap.add_argument("--workspace-slug", default=None,
                    help="Slack workspace slug, e.g. 'acmeco' for https://acmeco.slack.com. When set, every rendered message includes its Slack permalink so recalled facts can be cited back to the source thread.")
    # --- Content filters (drop noise before ingesting) ---
    ap.add_argument("--min-words", type=int, default=3,
                    help="Drop messages with fewer than N words (default 3 — filters 'lgtm', 'thanks', emoji-only)")
    ap.add_argument("--include-users", default="",
                    help="Comma-separated user ids OR display names — only keep messages from these. Default: all")
    ap.add_argument("--exclude-users", default="",
                    help="Comma-separated user ids OR display names — drop messages from these. Default: none")
    ap.add_argument("--include-keywords", default="",
                    help="Comma-separated keywords — day must contain at least one (case-insensitive substring). Default: no filter")
    ap.add_argument("--exclude-keywords", default="",
                    help="Comma-separated keywords — drop days containing any. Default: no filter")
    ap.add_argument("--min-day-messages", type=int, default=3,
                    help="Drop days with fewer than N surviving messages after per-message filters (default 3)")
    ap.add_argument("--include-code-entities", action="store_true",
                    help="Allow extraction of file paths / class names as Component entities. Default OFF — code belongs in GitNexus.")
    # --- IO / dedupe ---
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reingest", action="store_true")
    ap.add_argument("--state-file", default=STATE_FILE)
    args = ap.parse_args()

    export_path = Path(args.export).expanduser().resolve()
    if not export_path.is_file():
        print(f"ERROR: not a file: {export_path}", file=sys.stderr)
        return 2

    channel_filter = {c.strip().lstrip("#") for c in args.channels.split(",") if c.strip()} or None
    since = datetime.date.fromisoformat(args.since) if args.since else None
    include_users = _parse_csv(args.include_users) or None
    exclude_users = _parse_csv(args.exclude_users) or None
    include_kw = _parse_csv(args.include_keywords) or None
    exclude_kw = _parse_csv(args.exclude_keywords) or None
    state_path = Path(args.state_file).resolve()
    state = {} if args.reingest else load_state(state_path)
    seen = set(state.get(args.group_id, []))

    dropped_keyword = 0
    dropped_too_short = 0

    with zipfile.ZipFile(export_path, "r") as zf:
        names = zf.namelist()
        # users.json: id -> display name
        user_map: dict[str, str] = {}
        if "users.json" in names:
            users = json.loads(zf.read("users.json"))
            for u in users:
                user_map[u["id"]] = u.get("profile", {}).get("display_name") or u.get("name") or u["id"]

        # channels.json: build channel-name -> channel-id map for permalinks
        channel_id_map: dict[str, str] = {}
        if "channels.json" in names:
            try:
                channels = json.loads(zf.read("channels.json"))
                for c in channels:
                    if c.get("name") and c.get("id"):
                        channel_id_map[c["name"]] = c["id"]
            except (json.JSONDecodeError, KeyError):
                pass

        # Group message files by channel
        per_channel: dict[str, list[str]] = defaultdict(list)
        for n in names:
            if "/" not in n or not n.endswith(".json"):
                continue
            channel, fname = n.split("/", 1)
            if fname.count("-") != 2 or not fname.endswith(".json"):
                continue
            try:
                datetime.date.fromisoformat(fname[:-5])
            except ValueError:
                continue
            per_channel[channel].append(n)

        plan: list[dict] = []
        for channel, files in sorted(per_channel.items()):
            if channel_filter and channel not in channel_filter:
                continue
            ch_id = channel_id_map.get(channel)
            for fpath in sorted(files):
                date_str = Path(fpath).stem
                day_date = datetime.date.fromisoformat(date_str)
                if since and day_date < since:
                    continue
                key = f"slack:{channel}:{date_str}"
                h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
                if h in seen:
                    continue
                messages = json.loads(zf.read(fpath))
                rendered, surviving = render_day(
                    messages, user_map,
                    workspace=args.workspace_slug,
                    channel_id=ch_id,
                    min_words=args.min_words,
                    include_users=include_users,
                    exclude_users=exclude_users,
                )
                if not rendered.strip():
                    continue
                if surviving < args.min_day_messages:
                    dropped_too_short += 1
                    continue
                if not passes_keyword_gate(rendered, include=include_kw, exclude=exclude_kw):
                    dropped_keyword += 1
                    continue
                # Build a source_description that points at the channel for that
                # date — when --workspace-slug is given this is a clickable
                # archive URL, otherwise the structured slack: key.
                if args.workspace_slug and ch_id:
                    source_desc = f"https://{args.workspace_slug}.slack.com/archives/{ch_id} ({date_str})"
                else:
                    source_desc = key
                plan.append({
                    "hash": h,
                    "key": key,
                    "channel": channel,
                    "date": date_str,
                    "body": rendered,
                    "source_description": source_desc,
                })

    filter_summary_parts: list[str] = []
    if dropped_too_short:
        filter_summary_parts.append(f"{dropped_too_short} day(s) below --min-day-messages")
    if dropped_keyword:
        filter_summary_parts.append(f"{dropped_keyword} day(s) failed keyword gate")
    filter_summary = (" (filtered: " + ", ".join(filter_summary_parts) + ")") if filter_summary_parts else ""

    print(f"plan: {len(plan)} channel-day episode(s) to write to group_id={args.group_id!r} from {export_path.name}{filter_summary}")
    if args.dry_run:
        for ep in plan[:10]:
            line_count = ep["body"].count("\n") + 1
            print(f"  - {ep['key']} ({line_count} lines)")
        if len(plan) > 10:
            print(f"  ... +{len(plan) - 10} more")
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

    extract_kwargs: dict = {}
    if not args.include_code_entities:
        extract_kwargs["excluded_entity_types"] = ["Component"]
        extract_kwargs["custom_extraction_instructions"] = (
            "Extract ALL domain knowledge from this Slack channel-day. Be thorough — "
            "this graph is the project brain. Capture:\n"
            "- People: senders, mentioned users, vendor contacts\n"
            "- Vendors and third-party services\n"
            "- Business features and product decisions with their rationale\n"
            "- Root causes of bugs and incidents, and how they were resolved\n"
            "- Client/customer requirements and preferences\n"
            "- Deployment procedures, sequencing constraints, and prerequisites "
            "(e.g. 'module must be disabled before running migration', "
            "'run X before enabling Y or Z will break')\n"
            "- Operational runbooks: specific commands, flags, and the order they "
            "must be run in\n"
            "- Warnings and 'do this before that' constraints\n"
            "- Rollback procedures and known failure modes\n"
            "- Configuration decisions: which config keys, what values, why\n"
            "- Integration decisions: which systems talk to which, in what order\n"
            "- Scope decisions: what was explicitly included or excluded and why\n"
            "DO NOT extract as entities: PHP class names (anything resembling "
            "Magento\\Foo\\Bar or Vendor\\Module\\Class), file paths, function/method "
            "names, error messages as standalone entities, or URLs as entities. "
            "Code-structural detail belongs in GitNexus, not Graphiti."
        )

    written = 0
    failed = 0
    for ep in plan:
        try:
            client.add_memory(
                group_id=args.group_id,
                name=f"#{ep['channel']} {ep['date']}",
                episode_body=ep["body"],
                source="message",
                source_description=ep["source_description"],
                reference_time=f"{ep['date']}T00:00:00+00:00",
                **extract_kwargs,
            )
            seen.add(ep["hash"])
            written += 1
            print(f"  + {ep['key']}")
            state[args.group_id] = sorted(seen)
            save_state(state_path, state)
        except GraphitiError as e:
            failed += 1
            print(f"  ! {ep['key']}: {e}", file=sys.stderr)

    print(f"\ndone: wrote {written}, failed {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

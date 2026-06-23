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


def format_message(msg: dict, user_map: dict[str, str]) -> str | None:
    """Render one slack message as 'HH:MM @user: text'. Returns None for noise."""
    if msg.get("subtype") in {"channel_join", "channel_leave", "channel_topic", "channel_purpose"}:
        return None
    text = msg.get("text", "")
    if not text:
        return None
    ts = msg.get("ts", "0")
    try:
        when = datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc)
        time_str = when.strftime("%H:%M")
    except (ValueError, OSError):
        time_str = "??:??"
    user_id = msg.get("user") or msg.get("bot_id") or "unknown"
    user = user_map.get(user_id, user_id)
    return f"{time_str} @{user}: {text}"


def render_day(messages: list[dict], user_map: dict[str, str]) -> str:
    """Render a day's messages with threads inlined under parents."""
    # Group by thread_ts (None = root post)
    threads: dict[str | None, list[dict]] = defaultdict(list)
    for m in messages:
        tts = m.get("thread_ts")
        # Slack sets thread_ts == ts on the parent; treat that as root.
        if tts and tts != m.get("ts"):
            threads[tts].append(m)
        else:
            threads[None].append(m)

    lines: list[str] = []
    for parent in sorted(threads[None], key=lambda m: float(m.get("ts", "0"))):
        rendered = format_message(parent, user_map)
        if not rendered:
            continue
        lines.append(rendered)
        replies = sorted(threads.get(parent.get("ts"), []), key=lambda m: float(m.get("ts", "0")))
        for reply in replies:
            r = format_message(reply, user_map)
            if r:
                lines.append(f"  └─ {r}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="Graphiti MCP URL (e.g. http://localhost:8765/mcp)")
    ap.add_argument("--group-id", required=True)
    ap.add_argument("--export", required=True, help="Path to slack-export.zip")
    ap.add_argument("--channels", default="", help="Comma-separated channel names to include (default: all)")
    ap.add_argument("--since", default=None, help="ISO date — skip days before this (e.g. 2025-01-01)")
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
    state_path = Path(args.state_file).resolve()
    state = {} if args.reingest else load_state(state_path)
    seen = set(state.get(args.group_id, []))

    with zipfile.ZipFile(export_path, "r") as zf:
        names = zf.namelist()
        # users.json: id -> display name
        user_map: dict[str, str] = {}
        if "users.json" in names:
            users = json.loads(zf.read("users.json"))
            for u in users:
                user_map[u["id"]] = u.get("profile", {}).get("display_name") or u.get("name") or u["id"]

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
                rendered = render_day(messages, user_map)
                if not rendered.strip():
                    continue
                plan.append({
                    "hash": h,
                    "key": key,
                    "channel": channel,
                    "date": date_str,
                    "body": rendered,
                })

    print(f"plan: {len(plan)} channel-day episode(s) to write to group_id={args.group_id!r} from {export_path.name}")
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

    written = 0
    failed = 0
    for ep in plan:
        try:
            client.add_memory(
                group_id=args.group_id,
                name=f"#{ep['channel']} {ep['date']}",
                episode_body=ep["body"],
                source="message",
                source_description=ep["key"],
                reference_time=f"{ep['date']}T00:00:00+00:00",
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

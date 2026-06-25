#!/usr/bin/env python3
"""
SessionStart hook script: query Graphiti for top-N facts about the current
project + fleet, output as Claude Code hook JSON so the facts get injected
as additionalContext.

Resolution rules for the project group_id:
  1. $DDEV_PROJECT env var (set by DDEV inside the container)
  2. basename of `git rev-parse --show-toplevel` from CWD
  3. None — query fleet scope only

Connection URL:
  --url argument, else $GRAPHITI_URL env var, else http://localhost:8765/mcp.

Output shape (printed to stdout for the hook runtime to consume):
  {
    "hookSpecificOutput": {
      "hookEventName": "SessionStart",
      "additionalContext": "<markdown summary of recalled facts>"
    },
    "suppressOutput": true
  }

If anything goes wrong (server unreachable, empty result, etc.) the script
exits 0 with no additionalContext — never blocks session start.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Allow `python session_start_recall.py` from this script's directory OR
# from another cwd; locate graphiti_client.py alongside this file.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    from graphiti_client import GraphitiClient, GraphitiError
except ImportError as e:
    # Fail silently — the hook must never block session start.
    print(json.dumps({"suppressOutput": True}))
    sys.exit(0)


DEFAULT_URL = "http://localhost:8765/mcp"
TOP_N = 8
PINNED_GROUP_ID = "initial_ingest"
PINNED_MAX = 20


def resolve_project_id(cwd: Path) -> str | None:
    env_pid = os.environ.get("DDEV_PROJECT")
    if env_pid:
        return env_pid
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip()).name
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    return None


def format_pinned(episodes: list[dict]) -> str:
    if not episodes:
        return ""
    lines: list[str] = []
    lines.append(f"## Always-loaded (pinned via group_id={PINNED_GROUP_ID!r})")
    lines.append("")
    lines.append(f"{len(episodes)} permanent fact(s):")
    lines.append("")
    for e in episodes:
        name = e.get("name", "(unnamed)")
        content = (e.get("content") or "").strip().replace("\n", " ")
        if len(content) > 300:
            content = content[:297] + "..."
        src = e.get("source_description", "")
        src_str = f" [src: {src}]" if src else ""
        lines.append(f"- **{name}** — {content}{src_str}")
    lines.append("")
    return "\n".join(lines)


def format_recall(project_id: str | None, nodes: list[dict]) -> str:
    if not nodes:
        return ""
    lines: list[str] = []
    scope_str = f"project={project_id} + fleet" if project_id else "host + fleet"
    lines.append(f"## Graphiti recall ({scope_str})")
    lines.append("")
    lines.append(f"Top {len(nodes)} relevant facts from prior sessions:")
    lines.append("")
    for n in nodes:
        name = n.get("name", "(unnamed)")
        labels = [l for l in n.get("labels", []) if l != "Entity"]
        label_str = f" [{'/'.join(labels)}]" if labels else ""
        summary = (n.get("summary") or "").strip().replace("\n", " ")
        if len(summary) > 240:
            summary = summary[:237] + "..."
        gid = n.get("group_id", "?")
        lines.append(f"- **{name}**{label_str} (`{gid}`) — {summary}")
    lines.append("")
    lines.append("_Query Graphiti directly via the `graphiti` MCP for more (`search_nodes`, `search_memory_facts`, `get_episodes`)._")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=os.environ.get("GRAPHITI_URL", DEFAULT_URL))
    ap.add_argument("--query", default="recent decisions vendors incidents quirks",
                    help="Semantic query for top-N node retrieval")
    ap.add_argument("--top-n", type=int, default=TOP_N)
    args = ap.parse_args()

    cwd = Path.cwd()
    project_id = resolve_project_id(cwd)
    # From a project: search [project, fleet] — surfaces project-local facts +
    # genuine cross-project methodology, no host-ops noise.
    # From the host (no project): search [host, fleet] — surfaces host agent's
    # own operational memory (plugin internals, ingest tuning, fleet-mgmt) plus
    # the same fleet methodology layer.
    group_ids = [project_id, "fleet"] if project_id else ["host", "fleet"]

    pinned_episodes: list[dict] = []
    nodes: list[dict] = []
    try:
        client = GraphitiClient(args.url, timeout=10.0)
        client.initialize()

        # Tier 1 — pinned facts (always-loaded). Fetched via get_episodes
        # because we want the raw written episode bodies, not extracted entities.
        try:
            pinned_result = client.call_tool(
                "get_episodes",
                {"group_ids": [PINNED_GROUP_ID], "max_episodes": PINNED_MAX},
            )
            if isinstance(pinned_result, dict):
                pinned_episodes = pinned_result.get("episodes", []) or []
            if not isinstance(pinned_episodes, list):
                pinned_episodes = []
        except GraphitiError:
            pinned_episodes = []  # group may not exist yet; fine

        # Tier 2 — dynamic recall scoped to current project + fleet
        try:
            result = client.call_tool(
                "search_nodes",
                {"group_ids": group_ids, "query": args.query, "max_nodes": args.top_n},
            )
            if isinstance(result, dict):
                nodes = result.get("nodes", []) or []
            if not isinstance(nodes, list):
                nodes = []
        except GraphitiError:
            nodes = []
    except GraphitiError:
        print(json.dumps({"suppressOutput": True}))
        return 0
    except Exception:
        print(json.dumps({"suppressOutput": True}))
        return 0

    parts: list[str] = []
    pinned_block = format_pinned(pinned_episodes[:PINNED_MAX])
    if pinned_block:
        parts.append(pinned_block)
    recall_block = format_recall(project_id, nodes[: args.top_n])
    if recall_block:
        parts.append(recall_block)
    additional_context = "\n".join(parts)
    if not additional_context.strip():
        print(json.dumps({"suppressOutput": True}))
        return 0

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": additional_context,
        },
        "suppressOutput": True,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Ingest a folder of documents into Graphiti as episodes.

Walks the folder, chunks each document (markdown by ## headings; plain text
by paragraph cluster, ≤target_words per chunk), and posts each chunk to the
MCP server's add_memory tool.

Dedupe: every (file_path, chunk_index) pair is hashed and recorded in
.pb-graphiti-ingest.json next to the script's CWD. Re-runs skip already-
ingested chunks unless --reingest is passed.

Reference time: file mtime is used unless --reference-time is given.

Usage:
    python ingest_folder.py \\
        --url http://localhost:8765/mcp \\
        --group-id myproject \\
        --path ./docs \\
        [--dry-run] [--reingest] [--target-words 1500] \\
        [--include '*.md,*.txt'] [--source-prefix 'doc:']
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable

from graphiti_client import GraphitiClient, GraphitiError

DEFAULT_INCLUDE = ("*.md", "*.markdown", "*.txt", "*.rst")
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


def chunk_hash(file_rel: str, chunk_index: int, body: str) -> str:
    h = hashlib.sha256()
    h.update(file_rel.encode("utf-8"))
    h.update(str(chunk_index).encode("utf-8"))
    h.update(body.encode("utf-8"))
    return h.hexdigest()[:16]


def chunk_markdown(text: str, target_words: int) -> list[tuple[str, str]]:
    """Split markdown on ## (and lower) headings. Falls back to paragraph clusters if no headings.
    Returns list of (heading_or_first_line, body) tuples."""
    parts = re.split(r"^(#{2,6}\s+.*)$", text, flags=re.MULTILINE)
    out: list[tuple[str, str]] = []
    if len(parts) <= 1:
        return chunk_paragraphs(text, target_words)
    # parts alternates: preamble, heading, body, heading, body, ...
    preamble = parts[0].strip()
    if preamble:
        out.extend(chunk_paragraphs(preamble, target_words, title_prefix="(preamble)"))
    for i in range(1, len(parts), 2):
        heading = parts[i].strip().lstrip("#").strip()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        if not body.strip():
            continue
        # If the section is still too big, recursively split it.
        for sub_title, sub_body in chunk_paragraphs(body, target_words, title_prefix=heading):
            out.append((sub_title, sub_body))
    return out


def chunk_paragraphs(text: str, target_words: int, title_prefix: str = "") -> list[tuple[str, str]]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[tuple[str, str]] = []
    buf: list[str] = []
    word_count = 0
    for para in paragraphs:
        para_words = len(para.split())
        if word_count + para_words > target_words and buf:
            body = "\n\n".join(buf)
            title = title_prefix or buf[0][:80]
            chunks.append((title, body))
            buf = []
            word_count = 0
        buf.append(para)
        word_count += para_words
    if buf:
        body = "\n\n".join(buf)
        title = title_prefix or buf[0][:80]
        chunks.append((title, body))
    return chunks


def iter_files(root: Path, patterns: Iterable[str]) -> Iterable[Path]:
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(fnmatch.fnmatch(p.name, pat) for pat in patterns):
            yield p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="Graphiti MCP URL (e.g. http://localhost:8765/mcp)")
    ap.add_argument("--group-id", required=True, help="Graphiti group_id (use 'fleet' or your project id)")
    ap.add_argument("--path", required=True, help="Folder to ingest")
    ap.add_argument("--include", default=",".join(DEFAULT_INCLUDE), help="Comma-separated glob patterns (default: *.md,*.markdown,*.txt,*.rst)")
    ap.add_argument("--target-words", type=int, default=1500, help="Target words per chunk")
    ap.add_argument("--source-prefix", default="doc:", help="Prefix added to source_description")
    ap.add_argument("--reference-time", default=None, help="ISO timestamp for all episodes (default: file mtime)")
    ap.add_argument("--dry-run", action="store_true", help="Plan only, do not write")
    ap.add_argument("--reingest", action="store_true", help="Ignore dedupe state, re-write everything")
    ap.add_argument("--state-file", default=STATE_FILE, help=f"Dedupe state file (default: {STATE_FILE} in CWD)")
    args = ap.parse_args()

    root = Path(args.path).expanduser().resolve()
    if not root.is_dir():
        print(f"ERROR: not a directory: {root}", file=sys.stderr)
        return 2

    patterns = [p.strip() for p in args.include.split(",") if p.strip()]
    state_path = Path(args.state_file).resolve()
    state = {} if args.reingest else load_state(state_path)
    seen = set(state.get(args.group_id, []))

    # Build the full plan first.
    plan: list[dict] = []
    for f in iter_files(root, patterns):
        rel = str(f.relative_to(root))
        text = f.read_text(encoding="utf-8", errors="replace")
        chunks = chunk_markdown(text, args.target_words) if f.suffix.lower() in {".md", ".markdown"} else chunk_paragraphs(text, args.target_words)
        if not chunks:
            continue
        mtime = datetime.datetime.fromtimestamp(f.stat().st_mtime, tz=datetime.timezone.utc).isoformat()
        for idx, (title, body) in enumerate(chunks):
            h = chunk_hash(rel, idx, body)
            if h in seen:
                continue
            plan.append({
                "hash": h,
                "name": f"{rel}#{idx} — {title}"[:120],
                "body": body,
                "source_description": f"{args.source_prefix}{rel}",
                "reference_time": args.reference_time or mtime,
            })

    print(f"plan: {len(plan)} episode(s) to write to group_id={args.group_id!r} from {root}")
    if args.dry_run:
        for ep in plan[:10]:
            print(f"  - {ep['name']} ({len(ep['body'].split())} words)")
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
                name=ep["name"],
                episode_body=ep["body"],
                source="text",
                source_description=ep["source_description"],
                reference_time=ep["reference_time"],
            )
            seen.add(ep["hash"])
            written += 1
            print(f"  + {ep['name']}")
            # Flush state after every write so a Ctrl-C doesn't re-ingest the world.
            state[args.group_id] = sorted(seen)
            save_state(state_path, state)
        except GraphitiError as e:
            failed += 1
            print(f"  ! {ep['name']}: {e}", file=sys.stderr)

    print(f"\ndone: wrote {written}, failed {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

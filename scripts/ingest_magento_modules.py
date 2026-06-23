#!/usr/bin/env python3
"""
Ingest Magento 2 / Mage-OS module documentation into Graphiti.

Walks app/code/<Vendor>/<Module>/ (and optionally vendor/*/*/) looking for
module.xml. For each module found, builds one episode containing:
  - canonical name (Vendor_Module from module.xml)
  - sequence dependencies (from <sequence><module name=.../></sequence>)
  - composer.json description + version (if present)
  - README.md content (if present)
  - CHANGELOG.md head (if present, last ~5 entries)
  - di.xml preference summary (which classes the module overrides)
  - events.xml summary (which events it observes)

Pairs with GitNexus: GitNexus indexes code structure, this captures the
"why" — design rationale, business purpose, vendor verdicts. Recall surfaces
"we have module X in project Y that does Z" without re-reading the code.

Source description: file:// URI of the module's root directory, so
recalled facts cite back to the exact module dir.

Usage:
    python ingest_magento_modules.py \\
        --url http://localhost:8765/mcp \\
        --group-id <project-id> \\
        --project-root <path-to-magento-project> \\
        [--include-vendor] \\
        [--dry-run] [--reingest]
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
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


def find_modules(roots: list[Path]) -> list[Path]:
    """Return module directories (parents of etc/module.xml) under each root."""
    out: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for module_xml in root.rglob("etc/module.xml"):
            # The module dir is the grandparent of module.xml
            module_dir = module_xml.parent.parent
            out.append(module_dir)
    # Dedupe + stable order
    return sorted(set(out))


def parse_module_xml(path: Path) -> dict:
    """Parse Magento module.xml. Returns {name, setup_version, sequence: [names]}."""
    info = {"name": None, "setup_version": None, "sequence": []}
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError):
        return info
    root = tree.getroot()
    # Strip namespaces aggressively — Magento sometimes namespaces config.xml schemas
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    mod = root.find("module")
    if mod is None:
        return info
    info["name"] = mod.get("name")
    info["setup_version"] = mod.get("setup_version")
    seq = mod.find("sequence")
    if seq is not None:
        info["sequence"] = [m.get("name") for m in seq.findall("module") if m.get("name")]
    return info


def parse_composer_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return {
        "name": data.get("name"),
        "description": data.get("description"),
        "version": data.get("version"),
        "type": data.get("type"),
        "require": data.get("require", {}),
        "license": data.get("license"),
    }


def parse_events_xml(path: Path) -> list[str]:
    """Return list of observed event names (deduped)."""
    if not path.is_file():
        return []
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError):
        return []
    return sorted({e.get("name") for e in tree.iter("event") if e.get("name")})


def parse_di_xml(path: Path) -> dict:
    """Return high-level di.xml summary: preferences, plugins, virtualTypes."""
    if not path.is_file():
        return {}
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError):
        return {}
    preferences = [
        f"{p.get('for')} → {p.get('type')}"
        for p in tree.iter("preference")
        if p.get("for") and p.get("type")
    ]
    plugin_targets = sorted({
        t.get("name")
        for t in tree.iter("type")
        if t.find("plugin") is not None and t.get("name")
    })
    virtual_types = sorted({
        v.get("name")
        for v in tree.iter("virtualType")
        if v.get("name")
    })
    return {
        "preferences": preferences,
        "plugin_targets": plugin_targets,
        "virtual_types": virtual_types,
    }


def head_of_file(path: Path, max_chars: int = 4000) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[... truncated, file is {len(text)} chars total ...]"
    return text.strip()


def changelog_head(path: Path, max_entries: int = 5) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    # Split on H2 headings (## ...) — standard changelog format
    parts = re.split(r"^(##\s+.*)$", text, flags=re.MULTILINE)
    if len(parts) < 3:
        return text[:1500].strip()
    out: list[str] = []
    count = 0
    for i in range(1, len(parts), 2):
        if count >= max_entries:
            break
        heading = parts[i].strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        out.append(f"{heading}\n{body}")
        count += 1
    return "\n\n".join(out)


def build_episode(module_dir: Path, project_root: Path) -> dict | None:
    """Assemble one episode for a module directory. Returns None if no useful data."""
    module_xml = module_dir / "etc" / "module.xml"
    mod_info = parse_module_xml(module_xml)
    if not mod_info["name"]:
        return None  # Not a valid Magento module

    composer = parse_composer_json(module_dir / "composer.json")
    events = parse_events_xml(module_dir / "etc" / "events.xml")
    events_frontend = parse_events_xml(module_dir / "etc" / "frontend" / "events.xml")
    events_adminhtml = parse_events_xml(module_dir / "etc" / "adminhtml" / "events.xml")
    di = parse_di_xml(module_dir / "etc" / "di.xml")
    di_frontend = parse_di_xml(module_dir / "etc" / "frontend" / "di.xml")

    readme = head_of_file(module_dir / "README.md") or head_of_file(module_dir / "readme.md")
    changelog = changelog_head(module_dir / "CHANGELOG.md") or changelog_head(module_dir / "changelog.md")

    abs_dir = module_dir.resolve()
    rel_dir = module_dir.resolve().relative_to(project_root.resolve()) if project_root else module_dir
    file_uri = f"file://{abs_dir}/"

    # Build episode body
    parts: list[str] = []
    parts.append(f"Module: {mod_info['name']}")
    parts.append(f"Path: {rel_dir}")
    parts.append(f"Source: {file_uri}")
    if mod_info["setup_version"]:
        parts.append(f"setup_version: {mod_info['setup_version']}")
    if mod_info["sequence"]:
        parts.append(f"Depends on (sequence): {', '.join(mod_info['sequence'])}")
    if composer.get("description"):
        parts.append(f"Composer description: {composer['description']}")
    if composer.get("version"):
        parts.append(f"Composer version: {composer['version']}")
    if composer.get("require"):
        deps = ", ".join(f"{k}@{v}" for k, v in composer["require"].items())
        parts.append(f"Composer require: {deps}")

    # Wiring summary — terse, just headlines (GitNexus handles the structure)
    if di.get("preferences"):
        parts.append("\n## DI preferences (class overrides)")
        for p in di["preferences"]:
            parts.append(f"- {p}")
    all_plugins = (di.get("plugin_targets") or []) + (di_frontend.get("plugin_targets") or [])
    if all_plugins:
        parts.append("\n## Plugin targets (classes intercepted)")
        for t in sorted(set(all_plugins)):
            parts.append(f"- {t}")
    all_events = sorted(set(events + events_frontend + events_adminhtml))
    if all_events:
        parts.append("\n## Events observed")
        for e in all_events:
            parts.append(f"- {e}")

    if readme:
        parts.append("\n## README\n" + readme)
    if changelog:
        parts.append("\n## CHANGELOG (recent)\n" + changelog)

    body = "\n".join(parts).strip()
    if len(body) < 80:
        return None  # Bare module.xml with nothing else — not worth an episode

    mtime = datetime.datetime.fromtimestamp(module_xml.stat().st_mtime, tz=datetime.timezone.utc).isoformat()

    return {
        "name": mod_info["name"],
        "rel_path": str(rel_dir),
        "body": body,
        "source_description": file_uri,
        "reference_time": mtime,
        "hash": hashlib.sha256(body.encode("utf-8")).hexdigest()[:16],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--group-id", required=True, help="Graphiti group_id — use the project id, NOT 'fleet' (modules are per-project)")
    ap.add_argument("--project-root", required=True, help="Magento project root (containing app/code/ and optionally vendor/)")
    ap.add_argument("--include-vendor", action="store_true",
                    help="Also walk vendor/*/*/ for composer-installed modules (off by default — usually too noisy)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reingest", action="store_true")
    ap.add_argument("--state-file", default=STATE_FILE)
    args = ap.parse_args()

    project_root = Path(args.project_root).expanduser().resolve()
    if not project_root.is_dir():
        print(f"ERROR: project root not a directory: {project_root}", file=sys.stderr)
        return 2

    roots = [project_root / "app" / "code"]
    if args.include_vendor:
        vendor_dir = project_root / "vendor"
        if vendor_dir.is_dir():
            roots.append(vendor_dir)

    state_path = Path(args.state_file).resolve()
    state = {} if args.reingest else load_state(state_path)
    seen = set(state.get(args.group_id, []))

    modules = find_modules(roots)
    plan: list[dict] = []
    skipped_empty = 0
    skipped_dedup = 0

    for mdir in modules:
        ep = build_episode(mdir, project_root)
        if ep is None:
            skipped_empty += 1
            continue
        if ep["hash"] in seen:
            skipped_dedup += 1
            continue
        plan.append(ep)

    summary_parts: list[str] = []
    if skipped_dedup:
        summary_parts.append(f"{skipped_dedup} dedup")
    if skipped_empty:
        summary_parts.append(f"{skipped_empty} empty")
    summary = (" (skipped: " + ", ".join(summary_parts) + ")") if summary_parts else ""

    print(f"plan: {len(plan)} module(s) to write to group_id={args.group_id!r} from {project_root}{summary}")
    if args.dry_run:
        for ep in plan[:15]:
            print(f"  - {ep['name']} ({ep['rel_path']}, {len(ep['body'])} chars)")
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
            state[args.group_id] = sorted(seen)
            save_state(state_path, state)
        except GraphitiError as e:
            failed += 1
            print(f"  ! {ep['name']}: {e}", file=sys.stderr)

    print(f"\ndone: wrote {written}, failed {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

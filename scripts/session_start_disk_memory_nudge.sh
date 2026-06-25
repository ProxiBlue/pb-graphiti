#!/usr/bin/env bash
# pb-graphiti SessionStart hook (disk-memory side):
#   1. Always emits a 1-line policy reminder that memory recall is dual-source
#      (disk auto-memory + graphiti MCP). Complements session_start_recall.py
#      which dumps top-N graphiti facts.
#   2. If the disk auto-memory dir has *.md files modified in the last 7 days
#      (excluding MEMORY.md, the index), also emits a count + latest filename
#      so the user knows recall is worth invoking.
#
# Auto-memory dir resolution:
#   - $CLAUDE_AUTO_MEMORY_DIR (env override) if set
#   - else $HOME/.claude/projects/<sanitized-cwd>/memory/
#     where sanitized-cwd = cwd with / replaced by - (Claude Code default).
#
# Output: hook JSON on stdout (additionalContext + suppressOutput).
# Always exits 0 — must never block session start.
set -euo pipefail

DAYS=7
POLICY="Recall = dual-source: disk auto-memory AND graphiti MCP. Neither is a superset. When user says 'recall last session' / 'what did we remember about X', query BOTH (Read on the memory dir + search_memory_facts/get_episodes on graphiti), then reconcile."

stdin="$(cat 2>/dev/null || true)"
cwd="$(printf '%s' "$stdin" | jq -r '.cwd // empty' 2>/dev/null || true)"
[ -z "$cwd" ] && cwd="$PWD"

if [ -n "${CLAUDE_AUTO_MEMORY_DIR:-}" ]; then
  mem_dir="$CLAUDE_AUTO_MEMORY_DIR"
else
  sanitized="$(printf '%s' "$cwd" | tr / -)"
  mem_dir="$HOME/.claude/projects/${sanitized}/memory"
fi

nudge=""
if [ -d "$mem_dir" ]; then
  mapfile -t recent < <(
    find "$mem_dir" -maxdepth 1 -type f -name '*.md' ! -name 'MEMORY.md' -mtime -"$DAYS" -printf '%T@\t%p\n' 2>/dev/null \
      | sort -rn
  )
  count=${#recent[@]}
  if [ "$count" -gt 0 ]; then
    latest_line="${recent[0]}"
    latest_path="${latest_line#*$'\t'}"
    latest_name="$(basename "$latest_path")"
    latest_date="$(date -d "@${latest_line%%$'\t'*}" +%Y-%m-%d 2>/dev/null || echo '?')"
    nudge=" [memory] ${count} disk-memory file(s) modified ≤${DAYS}d. Latest: ${latest_name} (${latest_date}). Recall is worth invoking."
  fi
fi

msg="${POLICY}${nudge}"
printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":%s},"suppressOutput":true}\n' \
  "$(printf '%s' "$msg" | jq -Rs .)"

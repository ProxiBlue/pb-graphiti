---
description: Ingest a folder of documents (markdown, text, transcripts) into Graphiti as episodes. Walks the folder, chunks each file, dedupes via state file, and writes via add_memory. Pass --dry-run first to see the plan.
argument-hint: <path-to-folder> [--group-id <id>] [--dry-run]
---

You are about to ingest a folder of documents into the shared Graphiti knowledge graph. Follow this procedure exactly — do not skip steps.

## Step 1 — confirm scope with the user

Output ONE line in this exact form and wait for confirmation:

```
Ingest folder $ARGUMENTS — scope: [fleet | project=<id>]. Reply g / p / correction.
```

- `g` (or `global` / `fleet`) → use `--group-id fleet`.
- `p` (or `project`) → resolve the project id deterministically:
  - prefer `$DDEV_PROJECT` env var if set
  - else `basename $(git rev-parse --show-toplevel 2>/dev/null)` from the user's current cwd
- Any other reply → treat as a correction; re-classify and re-confirm.

Default suggestion: **project**, unless the folder name contains "fleet", "global", "policy", or otherwise reads as cross-project.

## Step 2 — dry-run the plan

Run the ingest script with `--dry-run` so the user sees what would be written before any episodes are created. The plugin ships a Python helper at `${CLAUDE_PLUGIN_ROOT}/scripts/ingest_folder.py`. The MCP URL comes from this plugin's userConfig.

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/ingest_folder.py" \
  --url "${user_config.graphiti_url}" \
  --group-id "<resolved-from-step-1>" \
  --path "$ARGUMENTS" \
  --dry-run
```

Show the user the plan output (episode count, group_id, first ~10 episode names). Wait for confirmation: "proceed? y/n".

## Step 3 — write

If the user confirmed, re-run without `--dry-run`. The script flushes its dedupe state after every successful write, so Ctrl-C is safe — resume by re-running the same command.

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/ingest_folder.py" \
  --url "${user_config.graphiti_url}" \
  --group-id "<resolved-from-step-1>" \
  --path "$ARGUMENTS"
```

Report the final summary line (`done: wrote N, failed M`) to the user.

## Notes

- Default file types ingested: `*.md`, `*.markdown`, `*.txt`, `*.rst`. Override with `--include '*.md,*.org'` etc.
- Default chunk size: ~1500 words. Override with `--target-words 1000`.
- State file `.pb-graphiti-ingest.json` is written in the CURRENT working directory. Re-runs from the same cwd skip already-ingested chunks. Pass `--reingest` to force a full re-write.
- Reference time on each episode defaults to file mtime (bi-temporal: Graphiti records both ingestion time AND when the source content was created). Override globally with `--reference-time 2026-01-15T00:00:00Z`.

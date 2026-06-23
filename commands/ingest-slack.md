---
description: Ingest a Slack workspace export zip into Graphiti. One episode per channel-per-day, message format, threads inlined. Dry-run first to see the plan.
argument-hint: <path-to-slack-export.zip> [--channels '#x,#y'] [--since YYYY-MM-DD] [--dry-run]
---

You are about to ingest Slack history into the shared Graphiti knowledge graph. Slack chats often contain decisions, vendor verdicts, and incident root causes — exactly the domain facts Graphiti is for. Follow this procedure.

## Step 1 — confirm scope

Output ONE line and wait for confirmation:

```
Ingest Slack export $ARGUMENTS — scope: [fleet | project=<id>]. Reply g / p / correction.
```

Default suggestion for Slack history: **fleet** (most Slack chatter is cross-project), unless the user names a specific channel that is project-scoped.

Resolve project id the same way as `ingest-folder`: `$DDEV_PROJECT` → fallback to git toplevel basename.

## Step 2 — confirm scope of content

Slack exports can be large. Before ingesting:

- Ask the user which channels to include if they haven't specified `--channels`. Default = ALL channels in the export, which is rarely what they want.
- Ask whether to bound the time range with `--since YYYY-MM-DD`. Reasonable default: last 12 months from today.

## Step 3 — dry-run

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/ingest_slack.py" \
  --url "${user_config.graphiti_url}" \
  --group-id "<resolved-from-step-1>" \
  --export "$ARGUMENTS" \
  --channels "<comma-separated-from-step-2>" \
  --since "<YYYY-MM-DD-from-step-2>" \
  --dry-run
```

Show the user: episode count, group_id, first ~10 episode keys. Heads-up the cost — every episode is one Anthropic Haiku entity-extraction call. ~365 episodes ≈ $0.50 in Haiku.

Wait for confirmation: "proceed? y/n".

## Step 4 — write

Re-run without `--dry-run`. The script flushes state after every successful write — safe to Ctrl-C and resume.

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/ingest_slack.py" \
  --url "${user_config.graphiti_url}" \
  --group-id "<resolved-from-step-1>" \
  --export "$ARGUMENTS" \
  --channels "<from-step-2>" \
  --since "<from-step-2>"
```

Report the final summary line to the user.

## Notes

- Episode shape: `format=message`, one per channel-per-day. Threads are inlined under their parent post with `└─` markers. Channel-join/leave/topic-change noise is filtered out.
- Reference time: midnight UTC on the day the messages were posted (bi-temporal — Graphiti knows this is historical content, not freshly observed).
- Source description: `slack:<channel>:<YYYY-MM-DD>` — easy to grep / cite back when an agent recalls a fact.
- State file `.pb-graphiti-ingest.json` in cwd dedupes by `(channel, date)` pair. Pass `--reingest` to force re-write.
- If the export is missing `users.json`, user IDs (e.g. `U02ABCD1234`) will appear instead of display names. Re-export from Slack with full member list.

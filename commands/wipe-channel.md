---
description: Selectively wipe one ingestion channel's episodes (and orphaned entities) from a Graphiti group. Safe alternative to the manual cypher recipes in the README — preview → confirm → delete via MCP → optional orphan prune via Neo4j HTTP → state-file reminder.
argument-hint: <channel-name>  (one of github-tickets, email, slack, magento-module, folder-doc, precompact-hook, slack-permalinked, slack-opaque)
---

You are about to delete a class of episodes from one Graphiti group. This is destructive and cannot be undone — re-ingest costs Haiku calls. Follow the procedure carefully.

## Step 1 — confirm scope

`$ARGUMENTS` is the channel to wipe. Validate it's one of: `github-tickets`, `email`, `slack`, `slack-permalinked`, `slack-opaque`, `magento-module`, `folder-doc`, `precompact-hook`. If not, list the options and stop.

Group_id MUST be the project id (you can't accidentally wipe across projects this way):
- `$DDEV_PROJECT` env var, else
- `basename $(git rev-parse --show-toplevel)`

Confirm in ONE line:

```
Wipe channel=<channel> from group_id=<id>. Proceed to dry-run? y/n
```

If `n`, stop. If `y`, continue.

## Step 2 — dry-run

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/wipe_channel.py" \
  --url "${user_config.graphiti_url}" \
  --group-id "<resolved>" \
  --channel "$ARGUMENTS" \
  --dry-run
```

Show the user the dry-run output: total episode count in the group, count matching the requested channel, and the first ~15 episode names + source URIs.

If the count is much higher or lower than expected, STOP and ask. Common surprises:
- High count when the user expected few: their `source_description` patterns may overlap (e.g., `slack-permalinked` includes only Slack-with-workspace-slug; `slack` covers both).
- Zero matches: the channel name is right but they may have ingested with a different source format. Run `python ... --channel <other-variant> --dry-run` to look around.

Wait for `proceed? y/n` before continuing.

## Step 3 — destructive delete

The script requires the user to type `wipe` at its prompt as a final guard. Run it WITHOUT `--yes` so they have to confirm interactively:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/wipe_channel.py" \
  --url "${user_config.graphiti_url}" \
  --group-id "<resolved>" \
  --channel "$ARGUMENTS"
```

(If the user explicitly asks for non-interactive, append `--yes` — but default is interactive.)

The script deletes episodes one-by-one via the MCP `delete_episode` tool. Progress is printed every 25 deletions.

## Step 4 — orphan entity cleanup (optional but recommended)

After episodes are gone, some entities may have no remaining `MENTIONS` edges (they only appeared in the wiped channel). The MCP doesn't expose raw cypher, so this step needs direct Neo4j HTTP access.

If the user has the Neo4j credentials available (env var `NEO4J_PASSWORD` set), re-run the script with the orphan-cleanup arguments:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/wipe_channel.py" \
  --url "${user_config.graphiti_url}" \
  --group-id "<resolved>" \
  --channel "$ARGUMENTS" \
  --neo4j-url "http://localhost:7474" \
  --yes
```

(`--yes` skips the prompt; the wipe has already happened, this just prunes orphans.)

If they don't have Neo4j credentials handy, paste them the manual cypher from the README's Troubleshooting section — same effect.

## Step 5 — dedupe state file reminder

If they intend to re-ingest from the same source, the script reminds them about `.pb-graphiti-ingest.json`. Confirm they've either deleted it or that they'll pass `--reingest` on the next run.

## Hard rules

1. **Never bypass the dry-run.** Even if the user is confident, the dry-run count is the only way to confirm the right channel pattern matched.
2. **Never delete across groups.** The script enforces single `--group-id`; the slash command enforces it by resolving from cwd. Other projects' data is protected by structure, not by user vigilance.
3. **Cite the user's exact channel before deleting.** "Wiping `github-tickets` from `lcd-mageos` — 186 episodes" — not "wiping that thing you said".
4. **If anything errors during deletion, stop and surface.** A partial wipe leaves the graph inconsistent; the user needs to know the state before proceeding.

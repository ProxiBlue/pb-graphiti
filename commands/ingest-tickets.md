---
description: Ingest GitHub issues + pull requests (with comment threads) into Graphiti. One episode per ticket. Filters by cutoff year, label, state, and minimum comment count. Pairs with the modules ingest — modules answer "what exists", tickets answer "why we built it that way".
argument-hint: <since-year-or-iso-date> [--repo owner/name] [--state all|open|closed] [--dry-run]
---

You are about to ingest GitHub issues and pull requests into Graphiti as project history. Follow this procedure.

## Step 1 — resolve repo + group_id

If `--repo` not given:
- Read it from the current git repo: `gh repo view --json nameWithOwner -q .nameWithOwner` (from the user's cwd).
- If that fails (no origin, not a git repo), STOP and ask the user for `--repo owner/name`.

Group_id MUST be the project id (tickets are per-project artefacts):
- `$DDEV_PROJECT` env var, else
- `basename $(git rev-parse --show-toplevel)`

Confirm in ONE line:

```
Ingest tickets from <repo> since <year> — group_id=<id>. State=<all|open|closed>. Proceed? y/n
```

`$ARGUMENTS` should be the since-year. Default `--state` to `all` (open + closed) — closed tickets carry the most "settled knowledge".

## Step 2 — dry-run

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/ingest_tickets.py" \
  --url "${user_config.graphiti_url}" \
  --group-id "<resolved>" \
  --repo "<resolved>" \
  --since "<year-from-args>" \
  --state all \
  --dry-run
```

Show ticket count + first ~15 entries with sizes. Heads-up the user on cost — each ticket = one Haiku call. ~100 tickets ≈ $0.50-1.00 in Haiku.

If the count is overwhelming, suggest tightening with:
- `--include-labels 'decision,bug,seo'` — only tickets carrying these labels
- `--exclude-labels 'dependencies,duplicate,wontfix'` — default already drops these
- `--min-comments 2` — skip tickets with no discussion (usually less valuable)
- `--since YYYY-MM-DD` — narrower than year boundary
- `--state closed` — only settled tickets

Wait for confirmation: "proceed? y/n".

## Step 3 — write

Same command, no `--dry-run`. Script flushes state after every successful write — Ctrl-C safe.

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/ingest_tickets.py" \
  --url "${user_config.graphiti_url}" \
  --group-id "<resolved>" \
  --repo "<resolved>" \
  --since "<year-from-args>" \
  --state all
```

Report the summary (`done: wrote N, failed M`).

## What each episode looks like

ONE episode per ticket containing:

- `<repo>#<num> (issue|PR): <title>` as the episode name
- State, author, labels, created/closed dates
- ticket URL as the citation
- Description (issue body)
- Chronological comment thread, **bot comments filtered** by default (dependabot, github-actions, codecov etc.). Pass `--include-bots` to keep them.

`source_description` = the ticket's html_url. When Claude recalls a fact from a ticket, it can cite back to the exact thread.

Cap: 30,000 chars per episode (long discussions truncated with a marker). For the rare ticket exceeding this, agents can fetch the full thread via the URL.

## Notes

- Auth comes from the `gh` CLI (uses `$GH_TOKEN` or `gh auth login` creds). No separate token needed.
- The `since` argument accepts a YEAR (`2024`) or full ISO date (`2024-06-15`). Year-only means Jan 1 of that year.
- Both issues AND PRs are ingested via `/repos/X/issues` — that endpoint returns both. PR-vs-issue is distinguished in the episode name.
- Dedupe state file: `.pb-graphiti-ingest.json` in cwd. Hash includes a slice of the episode body, so edits to a ticket's body create a new episode on re-run (intentional — preserves history; older versions of the fact remain).
- For GitLab: a parallel `ingest_tickets_gitlab.py` is not yet shipped; use `glab` directly to export then `ingest-folder` as a fallback, or wait for the GitLab variant.

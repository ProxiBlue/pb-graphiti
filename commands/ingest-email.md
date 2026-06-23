---
description: Ingest emails from an IMAP mailbox into Graphiti. One episode per email thread. Filter by sender/recipient address, since-date, and project keywords. Citations via `mid:<Message-ID>` URI. HTML stripped, quoted replies trimmed, auto-replies filtered by minimum word count.
argument-hint: <address-to-filter> --since YYYY-MM-DD [--folder INBOX] [--include-keywords ...]
---

You are about to ingest emails into Graphiti. Follow this procedure.

## Step 1 — collect credentials

The script needs (in order):
- IMAP host (e.g. `imap.gmail.com`, `imap.fastmail.com`, `mail.example.com`)
- IMAP user (the email address)
- Password — **MUST come from an env var**, not a CLI flag. Default env name `IMAP_PASSWORD`. **Use an app password**, not your real account password. Most providers offer this in security settings; Gmail requires 2FA + app passwords.

If the user hasn't set the env var, prompt them to do so first (don't echo the password back). Example:
```bash
read -s -p "IMAP password: " IMAP_PASSWORD && export IMAP_PASSWORD
```

## Step 2 — resolve scope

`$ARGUMENTS` should be the address to filter on (client@example.com etc.). Group_id MUST be the project id, not 'fleet':
- `$DDEV_PROJECT` env var, else
- `basename $(git rev-parse --show-toplevel)`

Confirm:
```
Ingest emails from/to <address> in <folder> since <date> — group_id=<id>. Proceed? y/n
```

Ask the user:
- What date to start from (`--since YYYY-MM-DD`, required)
- Which folder (default `INBOX`; for Gmail-wide search use `[Gmail]/All Mail`)
- Any project keywords to gate on (`--include-keywords`) — strongly recommended for client mailboxes to drop unrelated correspondence

## Step 3 — dry-run

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/ingest_email.py" \
  --url "${user_config.graphiti_url}" \
  --group-id "<resolved>" \
  --imap-host "<from-step-1>" \
  --imap-user "<from-step-1>" \
  --address "$ARGUMENTS" \
  --since "<YYYY-MM-DD>" \
  --folder "<INBOX or specific>" \
  --include-keywords "<comma-separated>" \
  --dry-run
```

Show: thread count, group_id, first ~15 entries with sizes. Heads-up the cost — each thread = one Anthropic Haiku call. 100 threads ≈ $0.50-1.50.

If the count is huge, tighten with `--include-keywords` (most leverage), `--min-thread-messages 2` (skip one-off notifications), or narrow the date range.

Wait for confirmation: "proceed? y/n".

## Step 4 — write

Same command, no `--dry-run`. Script flushes state after every successful write — Ctrl-C safe.

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/ingest_email.py" \
  --url "${user_config.graphiti_url}" \
  --group-id "<resolved>" \
  --imap-host "<host>" \
  --imap-user "<user>" \
  --address "$ARGUMENTS" \
  --since "<YYYY-MM-DD>" \
  --folder "<folder>" \
  --include-keywords "<keywords>"
```

Report `done: wrote N, failed M`.

## What each episode looks like

ONE episode per email thread (grouped by `References`/`In-Reply-To` headers, falling back to normalized subject). The episode body renders each message in chronological order as:

```
From: <sender>
Date: YYYY-MM-DD HH:MM

<body, HTML stripped, quoted replies removed>

---

From: <next sender>
Date: ...
```

- **name**: `📧 <subject> (N msg)` — first message's subject
- **source_description**: `mid:<Message-ID>` of the thread root (RFC 2392 URI; clickable in mail clients that support it)
- **reference_time**: earliest message in the thread
- **HTML stripped** to plain text via stdlib `html.parser`
- **Quoted replies trimmed** (lines starting `>` and "On <date> wrote:" markers)
- **Attachments**: filenames noted in headers but contents not extracted

## Notes

- **Code-entity suppression is ON by default** (same as ticket ingest). Emails often forward error logs and stack traces — without suppression Graphiti picks up file paths and class names as Component entities. Override with `--include-code-entities` if you specifically want them.
- **min-words filter** (default 10) drops messages too short to carry useful content — auto-replies, OOO notices, simple acknowledgements.
- **Threading** uses RFC 2822 References/In-Reply-To when present; falls back to normalized subject when missing. Subject normalization strips Re:/Fwd:/AW:/etc prefixes.
- **Dedupe** by thread-root Message-ID. Re-runs skip already-ingested threads. If a thread gains new messages, the dedupe still skips (hash includes only the root id). To force re-ingest of evolving threads, pass `--reingest`.
- **Server-side date filter** uses IMAP's `SINCE` (day granularity). Combined with the date range, sender/recipient filter, and keyword gate, large mailboxes can be safely scoped.
- **Performance**: large mailboxes (thousands of matched UIDs) take time — every message requires a separate IMAP FETCH. Plan for minutes, not seconds.

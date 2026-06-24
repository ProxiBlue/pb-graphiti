---
description: Ingest emails from an IMAP mailbox into Graphiti. One episode per email thread. Filter by sender/recipient address, since-date, and project keywords. Citations via `mid:<Message-ID>` URI. HTML stripped, quoted replies trimmed, auto-replies filtered by minimum word count.
argument-hint: <address-to-filter> --since YYYY-MM-DD [--folder INBOX] [--include-keywords ...]
---

You are about to ingest emails into Graphiti. Follow this procedure.

## Step 1 — confirm IMAP connection is configured

The connection details are stored as plugin `userConfig` (set at plugin-enable time or via `/plugin config pb-graphiti`):

- `imap_host` (e.g. `imap.gmail.com`, `imap.fastmail.com`, `mail.example.com`)
- `imap_port` (default `993` — IMAP4 over SSL)
- `imap_user` (the email address)
- `imap_folder` (default `INBOX`; for Gmail-wide search use `[Gmail]/All Mail`)
- `imap_password_env` (default `IMAP_PASSWORD` — env var NAME, not value)

If `imap_host` or `imap_user` is empty, STOP. The user hasn't configured the connection. Direct them:

```
IMAP connection not configured. Run /plugin config pb-graphiti to set imap_host and imap_user,
or edit ~/.claude/settings.json → pluginConfigs["pb-graphiti@pb-graphiti"].options to set:
  imap_host, imap_port, imap_user, imap_folder, imap_password_env
```

The password itself is read from the env var named by `imap_password_env` (default `$IMAP_PASSWORD`). The user must set it in their shell BEFORE running the ingest. Example, without echoing:

```bash
read -s -p "IMAP password: " IMAP_PASSWORD && export IMAP_PASSWORD
```

**Use an app password**, not the real account password. Most providers require this for IMAP-from-external-clients; Gmail/Microsoft require 2FA + app passwords.

## Step 2 — resolve scope + relevance gate

`$ARGUMENTS` is the **comma-separated address allowlist** for the project. Each entry is either a full email address (`alice@client.com`) or a domain match (`@client.com` matches anyone at the client's domain). Domain matches are usually the right starting point — they cover the client team without you having to enumerate every individual.

Group_id MUST be the project id, not 'fleet':
- `$DDEV_PROJECT` env var, else
- `basename $(git rev-parse --show-toplevel)`

Confirm:
```
Ingest emails matching <addresses> in <folder> since <date> — group_id=<id>. Proceed? y/n
```

Ask the user:
- What date to start from (`--since YYYY-MM-DD`, required)
- Which folder (default `INBOX`; for Gmail-wide search use `[Gmail]/All Mail`)
- Any project keywords for an ADDITIONAL content gate (`--include-keywords`) — useful when the address allowlist alone isn't tight enough (e.g., the client's emails also include unrelated chitchat and you want to keep only project-related threads)

### Relevance is required

**Always pass `--require-relevance`** unless the user explicitly opts out. It blocks the run if NEITHER an address allowlist NOR keywords is set, which prevents accidentally ingesting a whole mailbox.

Address matching covers every participant: `From`, `To`, `Cc`, `Bcc`, `Reply-To`. A thread where the client is `Cc`'d alongside an internal-only `To` still matches — that's the right behavior for capturing "discussions that touched the client."

## Step 3 — dry-run

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/ingest_email.py" \
  --url "${user_config.graphiti_url}" \
  --group-id "<resolved>" \
  --imap-host "${user_config.imap_host}" \
  --imap-port "${user_config.imap_port}" \
  --imap-user "${user_config.imap_user}" \
  --folder "${user_config.imap_folder}" \
  --password-env "${user_config.imap_password_env}" \
  --addresses "$ARGUMENTS" \
  --require-relevance \
  --since "<YYYY-MM-DD>" \
  --include-keywords "<comma-separated>" \
  --batch-days 30 \
  --parallel-workers 1 \
  --dry-run
```

**Picking `--batch-days` and `--parallel-workers`:**

The script splits the `[--since, today]` range into windows and uses a fresh IMAP session per window. This is necessary on big mailboxes — providers (Zoho, Gmail, Microsoft) close the socket when a single session fetches more than ~2-3k messages.

- Default `--batch-days 30` works for typical inboxes (~50-200 messages/month).
- For very dense mailboxes (>1000 messages/month), lower to `--batch-days 7` or `--batch-days 3`.
- For back-fills covering multiple years, bump `--parallel-workers` to 2-4. Respect your provider's concurrent connection cap: Zoho ≈5, Gmail ≈15, Fastmail ≈2/IP.
- For first-run smoke tests on a new mailbox, keep `--parallel-workers 1` so logs are sequential and easy to read.

If the script reports a "batch ... failed" warning with a socket error, halve `--batch-days` and re-run. The dedupe state file means already-ingested threads from successful batches will be skipped.

Show: thread count, group_id, first ~15 entries with sizes. The script reports how many messages were dropped by the address gate vs. the keyword gate vs. the min-words/min-thread filters — relay that summary so the user can see whether the filters are too loose or too tight. Heads-up the cost — each thread = one Anthropic Haiku call. 100 threads ≈ $0.50-1.50.

If the count is huge:
- Tighten `--addresses` — narrow domain matches to specific people
- Add or tighten `--include-keywords` (substring match against the whole rendered thread)
- `--min-thread-messages 2` skips one-off notifications
- Narrow the date range

If the count is suspiciously low: the address allowlist may be too tight, or the user's folder selection is wrong (Gmail puts archived mail under `[Gmail]/All Mail`, not `INBOX`).

Wait for confirmation: "proceed? y/n".

## Step 4 — write

Same command, no `--dry-run`. Script flushes state after every successful write — Ctrl-C safe.

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/ingest_email.py" \
  --url "${user_config.graphiti_url}" \
  --group-id "<resolved>" \
  --imap-host "${user_config.imap_host}" \
  --imap-port "${user_config.imap_port}" \
  --imap-user "${user_config.imap_user}" \
  --folder "${user_config.imap_folder}" \
  --password-env "${user_config.imap_password_env}" \
  --addresses "$ARGUMENTS" \
  --require-relevance \
  --since "<YYYY-MM-DD>" \
  --include-keywords "<keywords>" \
  --batch-days <same-as-dry-run> \
  --parallel-workers <same-as-dry-run>
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

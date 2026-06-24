---
description: Set up cron-driven periodic ingestion for tickets and email. Detects whether you're running on a host or inside a DDEV container and installs cron in the right place. Prints a recommended crontab snippet for review before any cron is installed.
argument-hint: (no args — interactive)
---

You are about to wire up periodic cron-driven ingestion. Follow this procedure carefully — the goal is "everything keeps the graph fresh without manual intervention."

## Step 1 — detect environment

Run:

```bash
echo "DDEV_PROJECT=${DDEV_PROJECT:-}"
echo "in container: $([ -f /.dockerenv ] && echo yes || echo no)"
echo "uname: $(uname -s)"
which crontab
```

Interpretation:
- `DDEV_PROJECT` non-empty AND `/.dockerenv` present → **inside a DDEV container.** The cron should be installed in the container so it has `$DDEV_PROJECT` auto-set and reaches the MCP via `host.docker.internal`.
- `DDEV_PROJECT` empty AND `/.dockerenv` absent → **on the host.** Install in the host's crontab.
- macOS host: `crontab` works but `launchd` is more idiomatic — flag this to the user.

Tell the user where you'll install:

```
Detected: <DDEV container | Linux host | macOS host>. Cron will be installed at: <location>. Proceed? y/n
```

## Step 2 — verify the env file exists

The cron wrappers source credentials from `$HOME/.pb-graphiti/env` (override via `PB_GRAPHITI_ENV`). Without it, the wrappers refuse to run.

```bash
test -f "${PB_GRAPHITI_ENV:-$HOME/.pb-graphiti/env}" && echo "env file present" || echo "env file MISSING"
```

If missing, tell the user to:

1. Copy the template: `cp "${CLAUDE_PLUGIN_ROOT}/scripts/cron/env.example" "$HOME/.pb-graphiti/env"`
2. Edit and fill in: `GH_TOKEN`, `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD`, `PB_ADDRESSES`
3. `chmod 600 "$HOME/.pb-graphiti/env"` so it's only readable by the user
4. Re-run `/pb-graphiti:automate`

STOP here if the env file is missing — installing cron without credentials produces silent nightly failures.

## Step 3 — pick which sources to schedule

Ask the user which to enable:

| Source | Recommended cadence | Why |
|---|---|---|
| **tickets** | every 6 hours (`0 */6 * * *`) | New issues / PRs land continuously; want them in recall by next session |
| **email** | daily at 02:00 (`0 2 * * *`) | Threads land continuously; daily granularity is enough |
| modules (manual) | not scheduled | Modules change rarely; ingest on-demand after composer updates |
| folder docs (manual) | not scheduled | Docs change rarely; ingest on-demand |

Default suggestion: tickets every 6h + email daily 02:00. Confirm with the user.

## Step 4 — write the cron entries

Construct the cron lines using the wrapper paths under `${CLAUDE_PLUGIN_ROOT}/scripts/cron/`:

```
# pb-graphiti — keep the graph fresh
0 */6 * * * ${CLAUDE_PLUGIN_ROOT}/scripts/cron/ingest-tickets.sh
0 2 * * *   ${CLAUDE_PLUGIN_ROOT}/scripts/cron/ingest-email.sh
```

Show these to the user FIRST. Wait for confirmation: "install? y/n".

### Installing on host (Linux / macOS)

```bash
# Read existing crontab, append our lines, write it back
( crontab -l 2>/dev/null | grep -v 'pb-graphiti' ; cat <<'EOF'

# pb-graphiti — keep the graph fresh
0 */6 * * * ${CLAUDE_PLUGIN_ROOT}/scripts/cron/ingest-tickets.sh
0 2 * * *   ${CLAUDE_PLUGIN_ROOT}/scripts/cron/ingest-email.sh
EOF
) | crontab -
```

The `grep -v 'pb-graphiti'` line is important — it strips any prior pb-graphiti entries so re-runs of `/pb-graphiti:automate` don't accumulate duplicates.

### Installing in a DDEV container

DDEV's web container's crontab does not survive `ddev restart` unless persisted via `.ddev/web-build/`. Use the `/etc/cron.d/` drop-in pattern:

1. Write `.ddev/web-build/pb-graphiti-cron`:

```bash
cat > .ddev/web-build/pb-graphiti-cron <<'EOF'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
0 */6 * * * lucas ${CLAUDE_PLUGIN_ROOT}/scripts/cron/ingest-tickets.sh
0 2 * * *   lucas ${CLAUDE_PLUGIN_ROOT}/scripts/cron/ingest-email.sh
EOF
```

2. Add an install step to `.ddev/web-build/Dockerfile` (or create it):

```dockerfile
COPY pb-graphiti-cron /etc/cron.d/pb-graphiti-cron
RUN chmod 644 /etc/cron.d/pb-graphiti-cron && service cron restart
```

3. Rebuild and restart: `ddev rebuild && ddev restart`.

(The user's username inside the DDEV web container is usually their host username; verify with `ddev exec whoami` if unsure.)

## Step 5 — verify

```bash
# On host:
crontab -l | grep pb-graphiti

# In DDEV:
ddev exec "ls -l /etc/cron.d/pb-graphiti-cron && cat /etc/cron.d/pb-graphiti-cron"
```

Tell the user where logs will land:

- `$HOME/.pb-graphiti/logs/ingest-tickets.log`
- `$HOME/.pb-graphiti/logs/ingest-email.log`

Suggest watching the first scheduled run with `tail -f` to confirm it actually fires.

## Step 6 — disabling / editing later

- Host: `crontab -e` and remove the pb-graphiti block.
- DDEV: delete `.ddev/web-build/pb-graphiti-cron` and the `COPY`/`RUN` lines from the Dockerfile, then `ddev rebuild`.

State files (`$HOME/.pb-graphiti/state/*.json`) and logs persist independently — delete them only if you want a clean re-ingest.

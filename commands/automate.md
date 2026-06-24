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

## Step 2 — pick the env file location, then verify it exists

The cron wrappers source credentials from a single env file. WHERE that file lives depends on environment — `$HOME` inside a DDEV web container is an **overlay filesystem that does NOT survive `ddev restart`**. A file at `$HOME/.pb-graphiti/env` disappears on every restart, and cron then silently fails on every run after.

Pick the location based on what Step 1 detected:

| Environment | Recommended `PB_GRAPHITI_ENV` | Why |
|---|---|---|
| **Linux / macOS host** | `$HOME/.pb-graphiti/env` | Host filesystem persists across reboots. Standard place. |
| **DDEV web container** | `/var/www/html/.pb-graphiti/env` (the project docroot, which is host-mounted) | `$HOME` is overlay and is wiped on `ddev restart`. The project mount survives. |
| Other containers (devcontainer / generic Docker) | A bind-mounted path under the project, NOT a writable layer under `$HOME` | Same reasoning as DDEV. |

Verify it exists for the chosen location:

```bash
# Resolve the path you decided on, then:
test -f "$PB_GRAPHITI_ENV_PATH" && echo "env file present" || echo "env file MISSING"
```

If missing, tell the user to:

1. Pick the right path per the table above. Inside a DDEV container, use `/var/www/html/.pb-graphiti/env`.
2. `mkdir -p "$(dirname "$PB_GRAPHITI_ENV_PATH")"`
3. Copy the template: `cp "${CLAUDE_PLUGIN_ROOT}/scripts/cron/env.example" "$PB_GRAPHITI_ENV_PATH"`
4. Edit and fill in: `GH_TOKEN`, `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD`, `PB_ADDRESSES`
5. `chmod 600 "$PB_GRAPHITI_ENV_PATH"` so it's only readable by the user
6. **If inside a project repo**, add the env path to `.gitignore`. For DDEV the convention is:
   ```
   /.pb-graphiti/env
   ```
   (Or just `/.pb-graphiti/` if you want to gitignore the whole state directory.)
7. Re-run `/pb-graphiti:automate`

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

#### 2a. Check for an existing cron Dockerfile in the project

Before writing a new Dockerfile, check whether this project already has a pattern that copies `*.cron` files into `/etc/cron.d/`. Many DDEV-based projects ship a `Dockerfile.ddev-cron` (or similarly-named) that does exactly that:

```bash
ls .ddev/web-build/Dockerfile* 2>&1
# Then for each candidate:
grep -l 'COPY \*\.cron\|/etc/cron.d/' .ddev/web-build/Dockerfile* 2>&1
```

If you find a file that already copies `*.cron` from `.ddev/web-build/` into `/etc/cron.d/`, **do NOT create another Dockerfile.** Skip ahead to 2c (drop the cron file with the right extension and the existing pattern picks it up).

If nothing exists, go to 2b.

#### 2b. Create a Dockerfile.ddev-cron (only if none exists)

Write `.ddev/web-build/Dockerfile.ddev-cron`:

```dockerfile
# Install pb-graphiti cron entries. Any *.cron file in this directory is
# copied into /etc/cron.d/. Persists across `ddev restart`.
COPY *.cron /etc/cron.d/
RUN chmod 644 /etc/cron.d/*.cron && service cron restart
```

(If you prefer the generic-Dockerfile pattern instead of a side file, put the `COPY` / `RUN` block at the bottom of your existing `.ddev/web-build/Dockerfile`.)

#### 2c. Drop the cron file

Write `.ddev/web-build/pb-graphiti.cron`. Two critical env vars at the top — they redirect `PB_GRAPHITI_HOME` and `PB_GRAPHITI_ENV` to the host-mounted project directory, because `$HOME` in DDEV web containers is an overlay that gets wiped on restart:

```bash
cat > .ddev/web-build/pb-graphiti.cron <<'EOF'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Overlay-safe paths: $HOME inside a DDEV container is wiped on `ddev restart`.
# Point pb-graphiti at the host-mounted project directory so env, state, and
# logs all persist.
PB_GRAPHITI_HOME=/var/www/html/.pb-graphiti
PB_GRAPHITI_ENV=/var/www/html/.pb-graphiti/env

0 */6 * * * <username> ${CLAUDE_PLUGIN_ROOT}/scripts/cron/ingest-tickets.sh
0 2 * * *   <username> ${CLAUDE_PLUGIN_ROOT}/scripts/cron/ingest-email.sh
EOF
```

Resolve `<username>` to the user inside the DDEV container (`ddev exec whoami` — usually the host username).

#### 2d. Rebuild + verify

```bash
ddev rebuild     # installs the cron file into /etc/cron.d/
ddev restart     # (rebuild usually restarts, but be sure)
ddev exec "ls -l /etc/cron.d/pb-graphiti.cron && cat /etc/cron.d/pb-graphiti.cron"
```

#### 2e. Gitignore the secrets

The env file at `/var/www/html/.pb-graphiti/env` lives inside the project mount and IS visible to git. Add to the project's `.gitignore`:

```
/.pb-graphiti/env
```

(Or `/.pb-graphiti/` to ignore the whole directory — env + state + logs — if you don't want any of them in git.)

## Step 5 — verify

```bash
# On host:
crontab -l | grep pb-graphiti

# In DDEV:
ddev exec "ls -l /etc/cron.d/pb-graphiti.cron && cat /etc/cron.d/pb-graphiti.cron"
```

Tell the user where logs will land — this depends on `PB_GRAPHITI_HOME`:

- **Host install** → `$HOME/.pb-graphiti/logs/ingest-{tickets,email}.log`
- **DDEV install** → `/var/www/html/.pb-graphiti/logs/ingest-{tickets,email}.log` (visible from the host at `<project-root>/.pb-graphiti/logs/`)

Suggest watching the first scheduled run with `tail -f` to confirm it actually fires.

## Step 6 — disabling / editing later

- **Host:** `crontab -e` and remove the pb-graphiti block.
- **DDEV:** delete `.ddev/web-build/pb-graphiti.cron` (and the `Dockerfile.ddev-cron` if you created it just for pb-graphiti and aren't using it for other cron files), then `ddev rebuild`.

State files (`<PB_GRAPHITI_HOME>/state/*.json`) and logs persist independently — delete them only if you want a clean re-ingest. In DDEV, that's `/var/www/html/.pb-graphiti/state/` and `/var/www/html/.pb-graphiti/logs/`.

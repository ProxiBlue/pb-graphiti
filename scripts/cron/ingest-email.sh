#!/usr/bin/env bash
# Cron-friendly wrapper for /pb-graphiti:ingest-email.
#
# Defaults (override via env):
#   LOOKBACK_DAYS   Window to fetch (default 7 — covers a week, dedupe handles overlap)
#   IMAP_HOST       Required (e.g. imappro.zoho.com, imap.gmail.com)
#   IMAP_PORT       Default 993
#   IMAP_USER       Required (the account login)
#   IMAP_FOLDER     Default INBOX
#   IMAP_PASSWORD   Required — usually exported from $PB_GRAPHITI_ENV
#   PB_ADDRESSES    Comma-separated address allowlist (required for --require-relevance)
#   PB_KEYWORDS     Optional comma-separated content keywords
#   PB_BATCH_DAYS   Default 7 (one week per IMAP session)
#   PB_PARALLEL     Default 2 — workers, respect provider concurrent-connection cap
#
# Usage from cron (daily at 02:00):
#   0 2 * * * /path/to/pb-graphiti/scripts/cron/ingest-email.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_common.sh
source "$SCRIPT_DIR/_common.sh"

pb_init "ingest-email"

: "${LOOKBACK_DAYS:=7}"
: "${IMAP_PORT:=993}"
: "${IMAP_FOLDER:=INBOX}"
: "${PB_BATCH_DAYS:=7}"
: "${PB_PARALLEL:=2}"

GROUP_ID=$(resolve_group_id || true)
if [ -z "$GROUP_ID" ]; then
    pb_log "ERROR: could not resolve group_id — set PB_GRAPHITI_GROUP_ID or run from a git repo"
    pb_done 2
fi

if [ -z "${IMAP_HOST:-}" ] || [ -z "${IMAP_USER:-}" ]; then
    pb_log "ERROR: IMAP_HOST and IMAP_USER must be set (via $PB_GRAPHITI_ENV)"
    pb_done 2
fi

if [ -z "${IMAP_PASSWORD:-}" ]; then
    pb_log "ERROR: IMAP_PASSWORD not set"
    pb_done 2
fi

if [ -z "${PB_ADDRESSES:-}" ] && [ -z "${PB_KEYWORDS:-}" ]; then
    pb_log "ERROR: PB_ADDRESSES or PB_KEYWORDS must be set (avoids whole-mailbox ingest)"
    pb_done 2
fi

if SINCE=$(date -u -d "${LOOKBACK_DAYS} days ago" +%Y-%m-%d 2>/dev/null); then
    :
elif SINCE=$(date -u -v "-${LOOKBACK_DAYS}d" +%Y-%m-%d 2>/dev/null); then
    :
else
    pb_log "ERROR: could not compute --since (LOOKBACK_DAYS=$LOOKBACK_DAYS)"
    pb_done 2
fi

pb_log "group_id=$GROUP_ID host=$IMAP_HOST user=$IMAP_USER folder=$IMAP_FOLDER since=$SINCE batch=$PB_BATCH_DAYS workers=$PB_PARALLEL"

args=(
    --url "$GRAPHITI_URL"
    --group-id "$GROUP_ID"
    --imap-host "$IMAP_HOST"
    --imap-port "$IMAP_PORT"
    --imap-user "$IMAP_USER"
    --folder "$IMAP_FOLDER"
    --password-env IMAP_PASSWORD
    --since "$SINCE"
    --require-relevance
    --batch-days "$PB_BATCH_DAYS"
    --parallel-workers "$PB_PARALLEL"
    --state-file "$PB_GRAPHITI_HOME/state/email.json"
)
[ -n "${PB_ADDRESSES:-}" ] && args+=(--addresses "$PB_ADDRESSES")
[ -n "${PB_KEYWORDS:-}" ] && args+=(--include-keywords "$PB_KEYWORDS")

if python3 "$PB_GRAPHITI_ROOT/ingest_email.py" "${args[@]}"; then
    pb_done 0
else
    rc=$?
    pb_log "ingest_email.py exited $rc"
    pb_done "$rc"
fi

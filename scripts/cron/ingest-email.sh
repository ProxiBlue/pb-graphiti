#!/usr/bin/env bash
# Cron-friendly wrapper for /pb-graphiti:ingest-email.
#
# Two-phase run:
#   1. Forward pass — fetch last LOOKBACK_DAYS, dedupe handles overlap.
#   2. If forward wrote 0 threads → backlog pass: walk mailbox newest→oldest
#      using a per-group cursor file, capped at PB_BACKLOG_PER_DAY threads.
#      Self-paces: busy weeks skip backlog, quiet weeks chew backfill steadily.
#
# Defaults (override via env):
#   LOOKBACK_DAYS         Forward window (default 7 — dedupe handles overlap)
#   IMAP_HOST             Required (e.g. imappro.zoho.com, imap.gmail.com)
#   IMAP_PORT             Default 993
#   IMAP_USER             Required (the account login)
#   IMAP_FOLDER           Default INBOX
#   IMAP_PASSWORD         Required — usually exported from $PB_GRAPHITI_ENV
#   PB_ADDRESSES          Comma-separated address allowlist (required)
#   PB_KEYWORDS           Optional comma-separated content keywords
#   PB_BATCH_DAYS         Default 7 (one week per IMAP session)
#   PB_PARALLEL           Default 2 — workers (provider concurrent-connection cap)
#   PB_BACKLOG_PER_DAY    Max threads written by backlog pass per run (default 20)
#   PB_BACKLOG_WINDOW_DAYS Days per backlog window (default 30)
#   PB_BACKLOG_DISABLE    Set to 1 to skip the backlog pass entirely
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
: "${PB_BACKLOG_PER_DAY:=20}"
: "${PB_BACKLOG_WINDOW_DAYS:=30}"
: "${PB_BACKLOG_DISABLE:=0}"

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

pb_log "group_id=$GROUP_ID host=$IMAP_HOST user=$IMAP_USER folder=$IMAP_FOLDER since=$SINCE batch=$PB_BATCH_DAYS workers=$PB_PARALLEL backlog_cap=$PB_BACKLOG_PER_DAY backlog_window=$PB_BACKLOG_WINDOW_DAYS"

common_args=(
    --url "$GRAPHITI_URL"
    --group-id "$GROUP_ID"
    --imap-host "$IMAP_HOST"
    --imap-port "$IMAP_PORT"
    --imap-user "$IMAP_USER"
    --folder "$IMAP_FOLDER"
    --password-env IMAP_PASSWORD
    --require-relevance
    --batch-days "$PB_BATCH_DAYS"
    --parallel-workers "$PB_PARALLEL"
    --state-file "$PB_GRAPHITI_HOME/state/email.json"
)
[ -n "${PB_ADDRESSES:-}" ] && common_args+=(--addresses "$PB_ADDRESSES")
[ -n "${PB_KEYWORDS:-}" ] && common_args+=(--include-keywords "$PB_KEYWORDS")

# parse_written_from_output: takes a path, echoes the last WRITTEN=N value or 0.
parse_written_from_output() {
    local path="$1"
    grep -oE 'WRITTEN=[0-9]+' "$path" | tail -1 | cut -d= -f2 || echo 0
}

# run_phase NAME args... — runs python, captures output to log AND parses WRITTEN.
# Sets the global PHASE_WRITTEN and PHASE_RC. Stdout is preserved via cat-back.
run_phase() {
    local name="$1"; shift
    local tmpout
    tmpout=$(mktemp)
    pb_log "==== phase: $name ===="
    set +e
    python3 "$PB_GRAPHITI_ROOT/ingest_email.py" "$@" > "$tmpout" 2>&1
    PHASE_RC=$?
    set -e
    cat "$tmpout"
    PHASE_WRITTEN=$(parse_written_from_output "$tmpout")
    rm -f "$tmpout"
    pb_log "phase $name: rc=$PHASE_RC written=$PHASE_WRITTEN"
}

# Phase 1: forward pass
run_phase forward --since "$SINCE" "${common_args[@]}"
forward_rc=$PHASE_RC
forward_written=$PHASE_WRITTEN

# Phase 2: backlog pass (only if forward wrote nothing AND backlog not disabled)
backlog_rc=0
backlog_written=0
if [ "$PB_BACKLOG_DISABLE" = "1" ]; then
    pb_log "backlog pass disabled (PB_BACKLOG_DISABLE=1)"
elif [ "$forward_rc" -ne 0 ]; then
    pb_log "skipping backlog pass: forward pass exited $forward_rc"
elif [ "$forward_written" -gt 0 ]; then
    pb_log "skipping backlog pass: forward wrote $forward_written thread(s)"
else
    run_phase backlog \
        --backlog-mode \
        --max-threads "$PB_BACKLOG_PER_DAY" \
        --backlog-window-days "$PB_BACKLOG_WINDOW_DAYS" \
        "${common_args[@]}"
    backlog_rc=$PHASE_RC
    backlog_written=$PHASE_WRITTEN
fi

pb_log "summary: forward_written=$forward_written backlog_written=$backlog_written"

# Surface the worst rc — but a phase that wrote nothing isn't a failure.
final_rc=0
[ "$forward_rc" -ne 0 ] && final_rc=$forward_rc
[ "$backlog_rc" -ne 0 ] && final_rc=$backlog_rc
pb_done "$final_rc"

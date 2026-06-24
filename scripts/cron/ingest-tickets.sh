#!/usr/bin/env bash
# Cron-friendly wrapper for /pb-graphiti:ingest-tickets.
#
# Defaults (override via env):
#   LOOKBACK_DAYS   How far back to pull (default 2 — cron runs frequently, dedupe handles overlap)
#   PB_REPO         GitHub repo in owner/name form. If unset, derived from `gh repo view` in cwd.
#   GH_TOKEN        Required for the gh CLI; usually exported from $PB_GRAPHITI_ENV
#
# Usage from cron (every 6 hours):
#   0 */6 * * * /path/to/pb-graphiti/scripts/cron/ingest-tickets.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_common.sh
source "$SCRIPT_DIR/_common.sh"

pb_init "ingest-tickets"

: "${LOOKBACK_DAYS:=2}"

GROUP_ID=$(resolve_group_id || true)
if [ -z "$GROUP_ID" ]; then
    pb_log "ERROR: could not resolve group_id — set PB_GRAPHITI_GROUP_ID or run from a git repo"
    pb_done 2
fi

if [ -z "${PB_REPO:-}" ]; then
    if ! PB_REPO=$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null); then
        pb_log "ERROR: could not derive repo from \`gh repo view\` — set PB_REPO=owner/name"
        pb_done 2
    fi
fi

# Compute --since as N days ago in ISO format (cross-platform date math)
if SINCE=$(date -u -d "${LOOKBACK_DAYS} days ago" +%Y-%m-%d 2>/dev/null); then
    :
elif SINCE=$(date -u -v "-${LOOKBACK_DAYS}d" +%Y-%m-%d 2>/dev/null); then
    # macOS BSD date
    :
else
    pb_log "ERROR: could not compute --since (LOOKBACK_DAYS=$LOOKBACK_DAYS)"
    pb_done 2
fi

pb_log "group_id=$GROUP_ID repo=$PB_REPO since=$SINCE url=$GRAPHITI_URL"

if python3 "$PB_GRAPHITI_ROOT/ingest_tickets.py" \
    --url "$GRAPHITI_URL" \
    --group-id "$GROUP_ID" \
    --repo "$PB_REPO" \
    --since "$SINCE" \
    --state-file "$PB_GRAPHITI_HOME/state/tickets.json"; then
    pb_done 0
else
    rc=$?
    pb_log "ingest_tickets.py exited $rc"
    pb_done "$rc"
fi

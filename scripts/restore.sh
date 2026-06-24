#!/usr/bin/env bash
# Restore the Graphiti Neo4j database from a dump file.
#
# This OVERWRITES the current database. All in-graph data is replaced
# with the contents of the dump. Required-confirmation prompt at runtime.
#
# Usage:
#   ./restore.sh <path-to-dump-file>
#   ./restore.sh                          # lists available dumps
#
# Defaults — override via env vars:
#   BACKUP_DIR        Where to look for dumps when listing (default: $HOME/backups/graphiti)
#   CONTAINER         Neo4j container name (default: graphiti-neo4j)
#   COMPOSE_DIR       Directory containing docker-compose.yml (default: <plugin>/infra)
#   VOLUME            Named Docker volume holding /data (default: graphiti-fleet_neo4j_data)
#   NEO4J_IMAGE       Image used for the offline-load helper (default: neo4j:5.26.0)
#
# Exit codes:
#   0  success
#   1  bad arguments
#   2  dump file missing
#   3  user did not confirm
#   4  load failed
#
# Procedure:
#   1. Verify dump exists; show its size and date
#   2. Prompt for typed confirmation
#   3. Stage the dump into a tempdir named neo4j.dump (the name neo4j-admin expects)
#   4. Stop the neo4j container
#   5. Run a transient neo4j container with the data volume mounted, execute
#      neo4j-admin database load --overwrite-destination=true
#   6. Start the neo4j container back up
#   7. Clean up the tempdir

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/graphiti}"
CONTAINER="${CONTAINER:-graphiti-neo4j}"
VOLUME="${VOLUME:-graphiti-fleet_neo4j_data}"
NEO4J_IMAGE="${NEO4J_IMAGE:-neo4j:5.26.0}"

# Resolve compose dir relative to this script's location, unless overridden
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="${COMPOSE_DIR:-${SCRIPT_DIR}/../infra}"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

DUMP_PATH="${1:-}"
if [ -z "$DUMP_PATH" ]; then
    cat <<EOF
Usage: $0 <path-to-dump-file>

Available dumps in $BACKUP_DIR:
EOF
    if [ -d "$BACKUP_DIR" ]; then
        ls -lh "$BACKUP_DIR"/*.dump 2>/dev/null | head -20 | sed 's/^/  /' || echo "  (none)"
    else
        echo "  (directory does not exist)"
    fi
    exit 1
fi

if [ ! -f "$DUMP_PATH" ]; then
    log "ERROR: dump file not found: $DUMP_PATH"
    exit 2
fi

if [ ! -f "$COMPOSE_DIR/docker-compose.yml" ]; then
    log "ERROR: docker-compose.yml not found at $COMPOSE_DIR"
    log "       set COMPOSE_DIR=/path/to/infra and retry"
    exit 1
fi

SIZE=$(du -h "$DUMP_PATH" | cut -f1)
MTIME=$(date -r "$DUMP_PATH" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || stat -c '%y' "$DUMP_PATH" 2>/dev/null || echo "unknown")
log "About to RESTORE from:"
log "  file:    $DUMP_PATH"
log "  size:    $SIZE"
log "  mtime:   $MTIME"
log "  target:  Docker volume '$VOLUME' (mounted as /data in the neo4j container)"
log ""
log "This OVERWRITES the current database. All current Graphiti data will be replaced."
log "Type 'restore' to proceed, anything else to abort:"
read -r CONFIRM
if [ "$CONFIRM" != "restore" ]; then
    log "aborted by user"
    exit 3
fi

# Stage the dump with the filename neo4j-admin expects
TEMPDIR=$(mktemp -d)
trap 'rm -rf "$TEMPDIR"' EXIT
cp "$DUMP_PATH" "$TEMPDIR/neo4j.dump"

log "stopping neo4j container..."
(cd "$COMPOSE_DIR" && docker compose stop neo4j) || {
    log "WARN: docker compose stop returned non-zero (may have been already stopped)"
}

log "loading dump into volume via transient container..."
if ! docker run --rm \
    -v "${VOLUME}:/data" \
    -v "${TEMPDIR}:/tmp/restore:ro" \
    "${NEO4J_IMAGE}" \
    neo4j-admin database load neo4j --from-path=/tmp/restore --overwrite-destination=true; then
    log "ERROR: neo4j-admin load failed"
    log "       starting neo4j again so the stack is not left down"
    (cd "$COMPOSE_DIR" && docker compose start neo4j) || true
    exit 4
fi

log "starting neo4j container..."
(cd "$COMPOSE_DIR" && docker compose start neo4j)

log "waiting for neo4j to become healthy..."
for _ in $(seq 1 30); do
    if docker exec "$CONTAINER" wget -qO- http://localhost:7474 >/dev/null 2>&1; then
        log "neo4j is up"
        break
    fi
    sleep 2
done

log "restore complete. Verify with a search_nodes call or the Neo4j browser at http://localhost:7474"

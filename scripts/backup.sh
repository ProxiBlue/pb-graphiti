#!/usr/bin/env bash
# Back up the Graphiti Neo4j database to a single-file dump on the host.
#
# Defaults — override via env vars:
#   BACKUP_DIR        Where to write dumps (default: $HOME/backups/graphiti)
#   RETENTION_DAYS    Delete dumps older than this (default: 30; 0 = keep all)
#   CONTAINER         Neo4j container name (default: graphiti-neo4j)
#   DUMP_PREFIX       Filename prefix; suffix is appended as -YYYY-MM-DD.dump (default: graphiti)
#
# Usage:
#   ./backup.sh                                  # use defaults
#   BACKUP_DIR=/mnt/nas/backups ./backup.sh      # custom directory
#   RETENTION_DAYS=90 ./backup.sh                # longer retention
#
# Cron example (nightly at 02:00, with 30-day rotation):
#   0 2 * * * /path/to/pb-graphiti/scripts/backup.sh >> $HOME/.local/state/graphiti-backup.log 2>&1
#
# Exit codes:
#   0  success
#   1  container not running
#   2  neo4j-admin dump failed
#   3  copy out of container failed

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/graphiti}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
CONTAINER="${CONTAINER:-graphiti-neo4j}"
DUMP_PREFIX="${DUMP_PREFIX:-graphiti}"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

if ! docker ps --filter "name=^${CONTAINER}$" --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
    log "ERROR: container '${CONTAINER}' is not running"
    log "       check 'docker ps' and start the stack before running backup"
    exit 1
fi

mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date +%F-%H%M%S)
DUMP_FILE="${BACKUP_DIR}/${DUMP_PREFIX}-${TIMESTAMP}.dump"

# Neo4j Community Edition requires the database to be stopped before dump.
# We read the password from the container's NEO4J_AUTH env var (set at start
# from infra/.env) so the user doesn't have to pass it again here.
log "reading neo4j credentials from container env ..."
NEO4J_AUTH=$(docker exec "$CONTAINER" printenv NEO4J_AUTH 2>/dev/null || echo "")
NEO4J_USER=$(echo "$NEO4J_AUTH" | cut -d/ -f1)
NEO4J_PASS=$(echo "$NEO4J_AUTH" | cut -d/ -f2-)
if [ -z "$NEO4J_USER" ] || [ -z "$NEO4J_PASS" ]; then
    log "ERROR: could not read NEO4J_AUTH from container env (expected 'user/password' format)"
    exit 2
fi

log "stopping 'neo4j' database (system database stays online) ..."
if ! docker exec "$CONTAINER" cypher-shell -u "$NEO4J_USER" -p "$NEO4J_PASS" -d system "STOP DATABASE neo4j WAIT" >/dev/null; then
    log "ERROR: STOP DATABASE neo4j failed"
    exit 2
fi

log "dumping neo4j database to volume ..."
docker exec "$CONTAINER" mkdir -p /data/backups
docker exec "$CONTAINER" rm -f /data/backups/neo4j.dump 2>/dev/null || true
DUMP_OK=1
if ! docker exec "$CONTAINER" neo4j-admin database dump neo4j --to-path=/data/backups; then
    log "ERROR: neo4j-admin dump failed"
    DUMP_OK=0
fi

log "restarting 'neo4j' database ..."
if ! docker exec "$CONTAINER" cypher-shell -u "$NEO4J_USER" -p "$NEO4J_PASS" -d system "START DATABASE neo4j WAIT" >/dev/null; then
    log "ERROR: START DATABASE neo4j failed — manual intervention needed"
    log "       run: docker exec $CONTAINER cypher-shell -u $NEO4J_USER -p '<pwd>' -d system 'START DATABASE neo4j WAIT'"
    exit 2
fi

if [ "$DUMP_OK" -ne 1 ]; then
    exit 2
fi

log "copying dump to host: $DUMP_FILE"
if ! docker cp "${CONTAINER}:/data/backups/neo4j.dump" "$DUMP_FILE"; then
    log "ERROR: docker cp out of container failed"
    exit 3
fi

SIZE=$(du -h "$DUMP_FILE" | cut -f1)
log "backup OK: $DUMP_FILE ($SIZE)"

if [ "$RETENTION_DAYS" -gt 0 ]; then
    DELETED=$(find "$BACKUP_DIR" -maxdepth 1 -name "${DUMP_PREFIX}-*.dump" -mtime +"$RETENTION_DAYS" -print -delete 2>/dev/null | wc -l)
    if [ "$DELETED" -gt 0 ]; then
        log "rotation: deleted ${DELETED} dump(s) older than ${RETENTION_DAYS} day(s)"
    fi
fi

# Show the most recent few so cron logs are useful
log "recent dumps:"
ls -lh "$BACKUP_DIR"/${DUMP_PREFIX}-*.dump 2>/dev/null | tail -5 | sed 's/^/  /'

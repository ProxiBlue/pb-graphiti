#!/usr/bin/env bash
# Shared environment for cron-driven pb-graphiti ingest wrappers.
# Sourced (not executed) by the per-source wrappers in this directory.

# === State / log / env layout (override any of these in your env or wrapper) ===

# Where state files (.pb-graphiti-ingest.json) and logs go. Defaults to a
# user-local location that works the same in DDEV containers and on hosts.
: "${PB_GRAPHITI_HOME:=$HOME/.pb-graphiti}"

# Where credentials live — bash file with `export VAR=value` lines.
# Loaded BEFORE the ingest script runs so IMAP_PASSWORD, GH_TOKEN, etc. are
# present. Stays out of crontab and out of the plugin's repo.
: "${PB_GRAPHITI_ENV:=$PB_GRAPHITI_HOME/env}"

# Plugin root — resolved relative to this script if not overridden. Lets
# wrappers find the ingest_*.py scripts and the python helpers regardless of
# where the plugin is mounted.
: "${PB_GRAPHITI_ROOT:=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# MCP URL — the ingest scripts default to localhost:8765/mcp; override here
# if your Graphiti server is somewhere else (e.g., host.docker.internal:8765
# when running inside a container).
: "${GRAPHITI_URL:=http://localhost:8765/mcp}"

# Group id resolution. Order: env override → DDEV_PROJECT → git toplevel
# basename → unset (caller must pass --group-id).
resolve_group_id() {
    if [ -n "${PB_GRAPHITI_GROUP_ID:-}" ]; then
        echo "$PB_GRAPHITI_GROUP_ID"
        return 0
    fi
    if [ -n "${DDEV_PROJECT:-}" ]; then
        echo "$DDEV_PROJECT"
        return 0
    fi
    local toplevel
    if toplevel=$(git rev-parse --show-toplevel 2>/dev/null); then
        basename "$toplevel"
        return 0
    fi
    echo ""
    return 1
}

# Logging — every wrapper logs to $PB_GRAPHITI_HOME/logs/<source>.log
# Caller sets LOG_NAME before calling pb_log.
pb_log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

# Wrapper entry — call this from each per-source script after setting LOG_NAME.
# Handles env-file sourcing, log redirection, state dir, and the "before
# running" boilerplate.
#
# If PB_GRAPHITI_FLEET_LOGS is set and writable, also tees into
# $PB_GRAPHITI_FLEET_LOGS/<group_id>/<log_name>.log so the graphiti-fleet
# nginx log-viewer (http://localhost:7475) can surface this project's runs.
pb_init() {
    local log_name="${1:-pb-graphiti}"
    mkdir -p "$PB_GRAPHITI_HOME/state" "$PB_GRAPHITI_HOME/logs"

    # Build tee target list: primary per-project log, plus fleet mirror if
    # configured. Source the env file BEFORE resolving fleet mirror so the
    # env can export PB_GRAPHITI_FLEET_LOGS.
    if [ -f "$PB_GRAPHITI_ENV" ]; then
        # shellcheck disable=SC1090
        set -a; source "$PB_GRAPHITI_ENV"; set +a
    fi

    local primary_log="$PB_GRAPHITI_HOME/logs/${log_name}.log"
    local tee_targets=("$primary_log")

    if [ -n "${PB_GRAPHITI_FLEET_LOGS:-}" ]; then
        local group_id
        group_id=$(resolve_group_id 2>/dev/null || true)
        if [ -n "$group_id" ]; then
            local fleet_dir="$PB_GRAPHITI_FLEET_LOGS/$group_id"
            if mkdir -p "$fleet_dir" 2>/dev/null && [ -w "$fleet_dir" ]; then
                tee_targets+=("$fleet_dir/${log_name}.log")
            fi
        fi
    fi

    # Redirect everything from this point on into all tee targets.
    exec > >(tee -a "${tee_targets[@]}") 2>&1

    pb_log "==== ${log_name} START ===="

    if [ -f "$PB_GRAPHITI_ENV" ]; then
        pb_log "loaded env from $PB_GRAPHITI_ENV"
    else
        pb_log "WARN: $PB_GRAPHITI_ENV not present — credentials must come from the cron env"
    fi

    if [ ${#tee_targets[@]} -gt 1 ]; then
        pb_log "fleet-log mirror: ${tee_targets[1]}"
    fi
}

pb_done() {
    local rc="${1:-0}"
    pb_log "==== exit $rc ===="
    exit "$rc"
}

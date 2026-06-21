#!/usr/bin/env bash
# Paper layer fetcher — OpenAlex + Semantic Scholar via Docker.
# Standalone-runnable; also invoked by run_autoFetcher.sh. Exits with the fetch status.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_PREFIX="[$(date -u +'%Y-%m-%dT%H:%M:%SZ') autofetch/paper]"

if ! command -v docker >/dev/null 2>&1; then
    echo "$LOG_PREFIX docker not on PATH; skipping." >&2
    exit 127
fi

# cron has no GUI, so a stopped OrbStack would fail the docker.sock connect
# (observed 2026-06-11). Auto-start + poll up to ~30s before giving up.
if ! docker info >/dev/null 2>&1; then
    echo "$LOG_PREFIX OrbStack not running; launching..."
    open -a OrbStack 2>/dev/null || true
    for _ in $(seq 1 30); do
        if docker info >/dev/null 2>&1; then
            echo "$LOG_PREFIX OrbStack up."
            break
        fi
        sleep 1
    done
fi

if ! docker info >/dev/null 2>&1; then
    echo "$LOG_PREFIX OrbStack failed to start within 30s; skipping." >&2
    exit 127
fi

echo "$LOG_PREFIX start"
docker compose -f "$REPO_ROOT/docker-compose.yml" run --rm app
status=$?
echo "$LOG_PREFIX exit=$status"
exit $status

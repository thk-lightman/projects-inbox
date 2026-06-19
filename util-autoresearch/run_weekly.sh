#!/usr/bin/env bash
# Weekly autoresearch driver — invoked by ~/Library/LaunchAgents/com.mori.autoresearch.plist.
#
# Sequence:
#   1. paper layer (Docker)   — fetch_papers.py: PI + venue → vault/inbox/auto/paper-*.md
#   2. dev layer   (host)     — fetch_dev.py:   topic watchlist → last30days → vault/inbox/auto/dev-{briefing,raw/*}.md
#
# Both layers share ~/.cache/autoresearch/dedup.sqlite. The dev layer ran on the host
# (not Docker) because last30days has its own runtime requirements (yt-dlp, optional
# Brave/X keys) that don't belong in the paper-fetch image.
#
# Stdout/stderr are captured by launchd into ~/.claude/autoresearch/_launchd.{out,err}.

set -uo pipefail  # NOT -e: dev failure must not kill paper success and vice versa.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_PREFIX="[$(date -u +'%Y-%m-%dT%H:%M:%SZ') autoresearch]"

paper_status=0
dev_status=0

echo "$LOG_PREFIX paper layer start"
if command -v docker >/dev/null 2>&1; then
    # Ensure OrbStack daemon is up. cron has no GUI, so a stopped
    # OrbStack would fail the docker.sock connect (observed 2026-06-11).
    # Auto-start + poll up to ~30s before giving up.
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
    if docker info >/dev/null 2>&1; then
        docker compose -f "$REPO_ROOT/docker-compose.yml" run --rm app
        paper_status=$?
    else
        echo "$LOG_PREFIX OrbStack failed to start within 30s; skipping paper layer."
        paper_status=127
    fi
else
    echo "$LOG_PREFIX docker not on PATH; skipping paper layer."
    paper_status=127
fi
echo "$LOG_PREFIX paper layer exit=$paper_status"

echo "$LOG_PREFIX dev layer start"
PYTHON="${PYTHON:-/usr/bin/env python3}"
$PYTHON "$REPO_ROOT/fetch_dev.py"
dev_status=$?
echo "$LOG_PREFIX dev layer exit=$dev_status"

if [[ "$paper_status" -ne 0 && "$dev_status" -ne 0 ]]; then
    exit 1
fi
exit 0

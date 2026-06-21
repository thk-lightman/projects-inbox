#!/usr/bin/env bash
# Dev layer fetcher — last30days topic synthesis on the host.
# Standalone-runnable; also invoked by run_autoFetcher.sh. Exits with the fetch status.
#
# Runs on the HOST (not Docker) because the Claude Code CLI + last30days plugin
# marketplace live on the host and the skill has its own runtime needs (yt-dlp,
# optional Brave/X keys) that don't belong in the paper-fetch image.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_PREFIX="[$(date -u +'%Y-%m-%dT%H:%M:%SZ') autofetch/dev]"
PYTHON="${PYTHON:-/usr/bin/env python3}"

echo "$LOG_PREFIX start"
$PYTHON "$REPO_ROOT/fetch_dev.py"
status=$?
echo "$LOG_PREFIX exit=$status"
exit $status

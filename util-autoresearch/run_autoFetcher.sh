#!/usr/bin/env bash
# Auto-fetch orchestrator — invoked by ~/Library/LaunchAgents/com.mori.autoresearch.plist.
#
# Runs both fetch layers as independent units; one layer's failure must not kill
# the other. Each sub-script is also runnable standalone for manual/partial fetch:
#
#   run_paper.sh  — paper layer (Docker): OpenAlex + Semantic Scholar
#   run_dev.sh    — dev layer (host):     last30days topic synthesis
#
# Both share ~/.cache/autoresearch/dedup.sqlite. Stdout/stderr are captured by
# launchd into ~/.claude/autoresearch/_launchd.{out,err}.

set -uo pipefail  # NOT -e: a failing layer must not abort the other.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_PREFIX="[$(date -u +'%Y-%m-%dT%H:%M:%SZ') autofetch]"

bash "$REPO_ROOT/run_paper.sh"
paper_status=$?

bash "$REPO_ROOT/run_dev.sh"
dev_status=$?

echo "$LOG_PREFIX paper=$paper_status dev=$dev_status"

# Fail the cron run only on a total outage (both layers failed); otherwise succeed.
if [[ "$paper_status" -ne 0 && "$dev_status" -ne 0 ]]; then
    exit 1
fi
exit 0

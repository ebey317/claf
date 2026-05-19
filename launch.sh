#!/usr/bin/env bash
# CLAF launcher — points Claude Code at the local orchestrator proxy.
#
# Usage (run from a SEPARATE terminal after `python3 orchestrator.py` is up):
#   bash ~/projects/claf/launch.sh
#
# What it does:
#   1. Sets ANTHROPIC_BASE_URL to localhost:8000 (the proxy).
#   2. Sets a dummy ANTHROPIC_AUTH_TOKEN (proxy ignores it; real key is
#      only used if/when the proxy escalates a request — not in v0).
#   3. Launches `claude --chrome --strict-mcp-config`.
#
# To bail out and use real Anthropic again, just open a fresh terminal —
# these env vars only live in the shell that sources this script.

set -euo pipefail

PROXY_URL="${CLAF_PROXY_URL:-http://localhost:8000/v1}"

if ! curl -fsS "${PROXY_URL%/v1}/" >/dev/null 2>&1; then
    echo "ERROR: orchestrator not responding at ${PROXY_URL%/v1}/"
    echo "Start it first:  python3 ~/projects/claf/orchestrator.py"
    exit 1
fi

export ANTHROPIC_BASE_URL="$PROXY_URL"
export ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN:-sk-claf-local-dummy}"

echo "ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL"
echo "Launching: claude --chrome --strict-mcp-config"
echo "(Ctrl+C to exit. Real Anthropic is one fresh terminal away.)"
echo

exec claude --chrome --strict-mcp-config

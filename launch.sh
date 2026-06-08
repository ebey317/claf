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

PROXY_URL="${CLAF_PROXY_URL:-http://localhost:8000}"
CLAF_DIR="$HOME/projects/claf"

# Auto-start the orchestrator if it isn't already up. madam = one command that
# brings the WHOLE hybrid stack online; you never manage the proxy by hand.
if ! curl -fsS "${PROXY_URL}/" >/dev/null 2>&1; then
    echo "Orchestrator not up — starting it..."
    # Start detached so it survives this shell; log to the usual file.
    nohup python3 "$CLAF_DIR/orchestrator.py" >> "$CLAF_DIR/orchestrator.startup.log" 2>&1 &
    # Wait up to ~15s for it to answer.
    for _i in $(seq 1 30); do
        sleep 0.5
        if curl -fsS "${PROXY_URL}/" >/dev/null 2>&1; then
            echo "Orchestrator is up at ${PROXY_URL}"
            break
        fi
    done
    if ! curl -fsS "${PROXY_URL}/" >/dev/null 2>&1; then
        echo "ERROR: orchestrator failed to start. Check $CLAF_DIR/orchestrator.startup.log"
        exit 1
    fi
else
    echo "Orchestrator already up at ${PROXY_URL}"
fi

export ANTHROPIC_BASE_URL="$PROXY_URL"
export ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN:-sk-claf-local-dummy}"
# Critical: never let an API key leak into the client when routing through CLAF.
# CLAF loads cloud-peer keys from ~/.master_ai_keys internally when it needs them.
unset ANTHROPIC_API_KEY

echo "ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL"
echo "Launching: claude --chrome"
echo "(Ctrl+C to exit. Real Anthropic is one fresh terminal away.)"
echo

# NOTE: do NOT use --strict-mcp-config here. Strict mode ignores ~/.claude.json
# and only loads servers passed via --mcp-config. With none passed that means
# ZERO MCP servers (sensei, email-bridge vanish). Plain --chrome loads the
# normal MCP config so sensei is available in the hybrid session.
exec claude --chrome

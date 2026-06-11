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

# FRESH RESTART EVERY TIME. madam = one command that brings the WHOLE hybrid
# stack online from a clean slate. A reused orchestrator serves STALE CODE — any
# fix to orchestrator.py / claf_config.py silently doesn't take until the proxy
# is actually restarted. So madam always kills the old one and starts fresh.
#
# The orchestrator runs as the `claf` systemd USER unit — managed via systemctl
# ONLY (never `nohup python3`, which spawns rogue processes that fight systemd
# for port 8000 and pile up as zombies). `systemctl restart` IS a kill + fresh
# start: it SIGTERMs the old main PID, waits, then launches a new one from disk.
echo "madam: fresh-restarting CLAF orchestrator (systemctl --user restart claf)..."
if systemctl --user list-unit-files claf.service >/dev/null 2>&1; then
    systemctl --user restart claf
else
    # No systemd unit (portable fallback): kill any orchestrator, then start one.
    echo "  (no claf.service unit found — falling back to pkill + nohup)"
    pkill -9 -f "$CLAF_DIR/orchestrator.py" 2>/dev/null || true
    sleep 1
    nohup python3 "$CLAF_DIR/orchestrator.py" >> "$CLAF_DIR/orchestrator.startup.log" 2>&1 &
fi

# Wait up to ~15s for the fresh proxy to answer.
for _i in $(seq 1 30); do
    sleep 0.5
    if curl -fsS "${PROXY_URL}/" >/dev/null 2>&1; then
        echo "Orchestrator is up at ${PROXY_URL} (fresh)"
        break
    fi
done
if ! curl -fsS "${PROXY_URL}/" >/dev/null 2>&1; then
    echo "ERROR: orchestrator failed to start. Check: journalctl --user -u claf -n 30"
    exit 1
fi

# Ensure sensei bridge is installed and running (auto-repair missing service).
_BRIDGE_URL="${SENSEI_BRIDGE_URL:-http://localhost:8080}"
_BRIDGE_SERVICE="sensei-bridge.service"
_BRIDGE_SERVICE_FILE="$HOME/.config/systemd/user/$_BRIDGE_SERVICE"
_BRIDGE_SRC="$CLAF_DIR/systemd/$_BRIDGE_SERVICE"
if [ ! -f "$_BRIDGE_SERVICE_FILE" ] && [ -f "$_BRIDGE_SRC" ]; then
    echo "madam: sensei-bridge service missing — installing from repo..."
    cp "$_BRIDGE_SRC" "$_BRIDGE_SERVICE_FILE"
    systemctl --user daemon-reload
    systemctl --user enable "$_BRIDGE_SERVICE" 2>/dev/null || true
fi
if ! curl -fsS "$_BRIDGE_URL/health" >/dev/null 2>&1; then
    echo "madam: sensei bridge down — starting $_BRIDGE_SERVICE..."
    systemctl --user start "$_BRIDGE_SERVICE" 2>/dev/null || true
    sleep 2
    if curl -fsS "$_BRIDGE_URL/health" >/dev/null 2>&1; then
        echo "  sensei bridge up at $_BRIDGE_URL"
    else
        echo "  WARNING: sensei bridge did not start — MCP tools may be unavailable"
    fi
fi

export ANTHROPIC_BASE_URL="$PROXY_URL"
export ANTHROPIC_AUTH_TOKEN="${ANTHROPIC_AUTH_TOKEN:-sk-claf-local-dummy}"
# Critical: never let an API key leak into the client when routing through CLAF.
# CLAF loads cloud-peer keys from ~/.master_ai_keys internally when it needs them.
unset ANTHROPIC_API_KEY

echo "ANTHROPIC_BASE_URL=$ANTHROPIC_BASE_URL"

# Ensure Chrome is running with remote debugging so sensei bridge can create tabs.
if ! curl -fsS "http://127.0.0.1:9222/json/version" >/dev/null 2>&1; then
    echo "madam: Chrome CDP not found on 9222 — launching Chrome..."
    /usr/bin/google-chrome --remote-debugging-port=9222 --no-first-run \
        --no-default-browser-check --disable-default-apps \
        2>/tmp/chrome-madam.log &
    for _i in $(seq 1 20); do
        sleep 0.5
        if curl -fsS "http://127.0.0.1:9222/json/version" >/dev/null 2>&1; then
            echo "  Chrome CDP up"
            break
        fi
    done
    if ! curl -fsS "http://127.0.0.1:9222/json/version" >/dev/null 2>&1; then
        echo "  WARNING: Chrome CDP did not start — MCP tab creation may fail"
    fi
fi

echo "Launching: claude --chrome"
echo "(Ctrl+C to exit. Real Anthropic is one fresh terminal away.)"
echo

# NOTE: do NOT use --strict-mcp-config here. Strict mode ignores ~/.claude.json
# and only loads servers passed via --mcp-config. With none passed that means
# ZERO MCP servers (sensei, email-bridge vanish). Plain --chrome loads the
# normal MCP config so sensei is available in the hybrid session.
exec claude --chrome

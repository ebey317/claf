#!/usr/bin/env bash
# Account-separation regression guard (Session 7 C35).
# Ensures the Console API key and Pro OAuth credentials stay isolated.

set -uo pipefail

CLAF_DIR="${CLAF_DIR:-$HOME/projects/claf}"
LAUNCH_SH="$CLAF_DIR/launch.sh"
ENV_FILE="$CLAF_DIR/.env"
KEY_FILE="$HOME/.master_ai_keys"

failures=0
results=()

banner() {
    echo ""
    echo "╔════════════════════════════════════════════════════════════════╗"
    echo "║       ACCOUNT SEPARATION VIOLATION — SEE FAILURES ABOVE        ║"
    echo "╚════════════════════════════════════════════════════════════════╝"
}

pass() { results+=("PASS $1"); echo "PASS $1"; }
fail() { results+=("FAIL $1"); echo "FAIL $1"; failures=$((failures + 1)); }

# 1. launch.sh unsets ANTHROPIC_API_KEY before launching Claude Code.
if [ -f "$LAUNCH_SH" ] && grep -q 'unset ANTHROPIC_API_KEY' "$LAUNCH_SH"; then
    pass "launch.sh unsets ANTHROPIC_API_KEY"
else
    fail "launch.sh unsets ANTHROPIC_API_KEY (missing unset in $LAUNCH_SH)"
fi

# 2. No Anthropic API secret (sk-ant-*) is exported in this shell.
leaked=$(env | grep '^ANTHROPIC' | grep -E 'sk-ant-[a-zA-Z0-9_-]+' || true)
if [ -z "$leaked" ]; then
    pass "shell env contains no sk-ant-* secret"
else
    fail "shell env contains sk-ant-* secret"
fi

# 3. The console key file exists and is reachable via ~/.master_ai_keys.
resolved=$(readlink -e "$KEY_FILE" 2>/dev/null || true)
if [ -n "$resolved" ]; then
    pass "console key file resolves: $resolved"
else
    fail "console key file does not resolve: $KEY_FILE"
fi

# 4. .env explicitly sets ANTHROPIC_API_KEY to empty (local alias only).
if [ -f "$ENV_FILE" ] && grep -qE '^ANTHROPIC_API_KEY=$' "$ENV_FILE"; then
    pass ".env has empty ANTHROPIC_API_KEY"
else
    fail ".env missing empty ANTHROPIC_API_KEY line"
fi

# Summary
echo ""
for r in "${results[@]}"; do
    echo "  $r"
done

if [ "$failures" -eq 0 ]; then
    echo ""
    echo "ACCOUNT SEPARATION OK — 4/4 checks passed"
    exit 0
else
    banner
    exit 1
fi

#!/usr/bin/env bash
# kimi_code.sh — launch Claude Code driven by Kimi K2 (Moonshot), agentic.
#
# This is "Kimi Code": the SAME Claude Code CLI (your hooks, memory, MCP/sensei,
# permissions all intact) but the model is Kimi K2 — a genuinely agentic
# tool-calling model, not a chat/reasoning model like gpt-oss. Moonshot ships an
# Anthropic-compatible endpoint, so Claude Code talks to it natively.
#
# Usage:
#   1. Get a key at https://platform.moonshot.ai  → API Keys → create.
#   2. Add it to your keychain (one line, no spaces):
#        echo 'MOONSHOT_API_KEY=sk-...' >> ~/.master_ai_keys
#   3. Run:  bash ~/projects/claf/kimi_code.sh
#
# To bail back to normal Claude, just open a fresh terminal.
set -euo pipefail

KEYS="$HOME/.master_ai_keys"
MODEL="${KIMI_MODEL:-kimi-k2-0711-preview}"   # agentic K2; kimi-k2-turbo-preview = faster

# Load the Moonshot key from the keychain (presence check, value never echoed).
if [[ -f "$KEYS" ]]; then
    # shellcheck disable=SC1090
    KEY_LINE="$(grep -E '^MOONSHOT_API_KEY=' "$KEYS" | head -1 || true)"
fi
if [[ -z "${KEY_LINE:-}" ]]; then
    echo "ERROR: MOONSHOT_API_KEY not found in $KEYS"
    echo "  1) Get a key: https://platform.moonshot.ai  (API Keys → create)"
    echo "  2) echo 'MOONSHOT_API_KEY=sk-...' >> $KEYS"
    echo "  3) re-run: bash ~/projects/claf/kimi_code.sh"
    exit 1
fi
MOONSHOT_API_KEY="${KEY_LINE#MOONSHOT_API_KEY=}"

# Point Claude Code at Moonshot's Anthropic-compatible endpoint.
export ANTHROPIC_BASE_URL="https://api.moonshot.ai/anthropic"
export ANTHROPIC_AUTH_TOKEN="$MOONSHOT_API_KEY"
export ANTHROPIC_MODEL="$MODEL"
unset ANTHROPIC_API_KEY   # avoid leaking a different key

echo "Kimi Code: model=$MODEL  endpoint=$ANTHROPIC_BASE_URL"
echo "Launching agentic Claude Code on Kimi K2 (hooks + memory + sensei intact)."
echo
exec claude --chrome --model "$MODEL"

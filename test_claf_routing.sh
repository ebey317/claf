#!/bin/bash
# CLAF Orchestrator Routing Test
# Tests: Local, Ollama Cloud, Groq/Cerebras fallback, Anthropic last-resort

set -e
PROXY_URL="${CLAF_PROXY_URL:-http://localhost:8000}"
LOG="$HOME/projects/claf/orchestrator.log"

echo "=== CLAF ROUTING TEST ==="
echo "Proxy: $PROXY_URL"
echo "Log: $LOG"
echo ""

# Check CLAF is up
if ! curl -fsS "$PROXY_URL/healthz" >/dev/null 2>&1; then
    echo "ERROR: CLAF not running at $PROXY_URL"
    echo "Start it first: cd ~/projects/claf && python3 orchestrator.py"
    exit 1
fi

# Get baseline log line count
BASELINE=$(wc -l < "$LOG")
echo "Baseline log lines: $BASELINE"
echo ""

# Test 1: Easy chat → should route LOCAL
echo "TEST 1: Easy chat ('say hello')"
echo "  Expected: local-ollama (tier 0)"
curl -s -X POST "$PROXY_URL/v1/messages" \
  -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"say hello"}],"stream":false}' \
  >/dev/null 2>&1
sleep 2
T1=$(tail -n +$((BASELINE+1)) "$LOG" | grep -c "picked_tier.*0" || true)
if [ "$T1" -gt 0 ]; then echo "  PASS: Local handled it"; else echo "  CHECK: May have gone cloud"; fi
echo ""

# Test 2: Hard code task → should route CLOUD (tier 1)
echo "TEST 2: Hard code ('debug a race condition in python asyncio')"
echo "  Expected: any cloud peer (tier 1)"
curl -s -X POST "$PROXY_URL/v1/messages" \
  -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"debug a race condition in python asyncio"}],"stream":false}' \
  >/dev/null 2>&1
sleep 3
T2=$(tail -n +$((BASELINE+1)) "$LOG" | grep -c '"picked_tier": 1' || true)
if [ "$T2" -gt 0 ]; then echo "  PASS: Cloud handled it"; else echo "  CHECK: May have gone elsewhere"; fi
echo ""

# Test 3: Regex/bash task → should route TAP (Groq tier 2) or stay local
echo "TEST 3: Medium task ('write a bash one-liner to find files modified in the last 24 hours')"
echo "  Expected: local or groq (tier 0 or 2)"
curl -s -X POST "$PROXY_URL/v1/messages" \
  -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"write a bash one-liner to find files modified in the last 24 hours"}],"stream":false}' \
  >/dev/null 2>&1
sleep 2
echo "  (Check log manually for this one)"
echo ""

# Test 4: Force emergency → should NOT go to Anthropic if others available
echo "TEST 4: Emergency flag with hard task"
echo "  Expected: Anything BUT anthropic (tier 9)"
curl -s -X POST "$PROXY_URL/v1/messages" \
  -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"refactor this complex nested async function"}],"metadata":{"emergency":true},"stream":false}' \
  >/dev/null 2>&1
sleep 3
T4=$(tail -n +$((BASELINE+1)) "$LOG" | grep -c "anthropic" || true)
if [ "$T4" -eq 0 ]; then echo "  PASS: Anthropic avoided"; else echo "  FAIL: Anthropic was used"; fi
echo ""

# Summary
echo "=== SUMMARY ==="
echo "Log entries since baseline:"
tail -n +$((BASELINE+1)) "$LOG" | grep "route_decision" | while read -r line; do
    parsed=$(echo "$line" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'{d.get(\"ts\",\"?\")} | tier={d.get(\"picked_tier\",\"?\")} | {d.get(\"picked_name\",\"?\")} | {d.get(\"picked_model\",\"?\")}')" 2>/dev/null)
    if [ -n "$parsed" ]; then echo "  $parsed"; else echo "  (raw: $line)"; fi
done
echo ""
echo "Anthropic calls in this test: $T4"
echo ""
echo "=== DONE ==="

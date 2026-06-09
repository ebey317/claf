#!/bin/bash
PROXY="http://localhost:8000"
LOG="$HOME/projects/claf/orchestrator.log"
BASE=$(wc -l < "$LOG")

echo "=== CLAF TEST ==="
curl -fsS "$PROXY/healthz" >/dev/null || { echo "CLAF down"; exit 1; }

echo "TEST 1: Easy chat"
curl -s -X POST "$PROXY/v1/messages" -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"say hello"}],"stream":false}' >/dev/null
sleep 2

echo "TEST 2: Hard code"
curl -s -X POST "$PROXY/v1/messages" -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"debug a race condition in python asyncio"}],"stream":false}' >/dev/null
sleep 3

echo "TEST 3: Bash one-liner"
curl -s -X POST "$PROXY/v1/messages" -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"write a bash one-liner to find files modified in the last 24 hours"}],"stream":false}' >/dev/null
sleep 2

echo "TEST 4: Emergency hard task"
curl -s -X POST "$PROXY/v1/messages" -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"refactor this complex nested async function"}],"metadata":{"emergency":true},"stream":false}' >/dev/null
sleep 3

echo ""
echo "=== RESULTS ==="
tail -n +$((BASE+1)) "$LOG" | grep "route_decision" | python3 -c "
import sys, json
for line in sys.stdin:
    d = json.loads(line)
    print(f\"  tier={d.get('picked_tier','?')} | {d.get('picked_name','?')} | {d.get('picked_model','?')}\")
"

ANTH=$(tail -n +$((BASE+1)) "$LOG" | grep -c "anthropic" || true)
echo "Anthropic calls: $ANTH (should be 0)"

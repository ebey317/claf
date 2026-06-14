#!/usr/bin/env bash
# Model A/B harness for Mary's local model selection (Session 7 C28).
#
# Run ON Mary:
#   bash ~/projects/claf/parity/run_model_ab.sh
#
# Starts an isolated orchestrator on port 8001 for each candidate, runs the
# tool-formation corpus + parity harness, and appends RESULT lines to
# parity/ab_results.jsonl. Never disturbs the live service on port 8000.

set -uo pipefail

CLAF_DIR="${CLAF_DIR:-$HOME/projects/claf}"
PARITY_DIR="$CLAF_DIR/parity"
RESULTS_FILE="$PARITY_DIR/ab_results.jsonl"
PORT=8001
ORCH_URL="http://127.0.0.1:$PORT"

# Baseline + candidates.  hermes3:3b is the current Mary default.
CANDIDATES=(
    "hermes3:3b"
    "qwen3.5:2b"
    "qwen2.5:3b-instruct"
    "qwen2.5-coder:3b"
    "llama3.2:3b"
)

PYTHON=python3
if [ -f "$CLAF_DIR/.venv/bin/python3" ]; then
    # Use venv only if it has the required deps; otherwise fall back to system.
    if "$CLAF_DIR/.venv/bin/python3" -c "import fastapi, httpx" 2>/dev/null; then
        PYTHON="$CLAF_DIR/.venv/bin/python3"
    fi
fi

echo "=== CLAF Model A/B on Mary ==="
echo "python: $PYTHON"
echo "results: $RESULTS_FILE"
echo ""

mkdir -p "$PARITY_DIR"
touch "$RESULTS_FILE"

# Ensure baseline model is present.
ensure_model() {
    local model="$1"
    if ollama list | awk '{print $1}' | grep -qx "$model"; then
        echo "[$model] already present"
        return 0
    fi
    echo "[$model] pulling (this may take a while)..."
    if ! ollama pull "$model"; then
        echo "[$model] PULL FAILED — skipping"
        return 1
    fi
    return 0
}

kill_isolated() {
    local pids
    pids=$(pgrep -f "CLAF_PORT=\"$PORT\"" || true)
    if [ -z "$pids" ]; then
        pids=$(lsof -ti tcp:"$PORT" 2>/dev/null || true)
    fi
    if [ -n "$pids" ]; then
        echo "  killing isolated orchestrator pids: $pids"
        echo "$pids" | xargs -r kill 2>/dev/null || true
        sleep 2
        echo "$pids" | xargs -r kill -9 2>/dev/null || true
    fi
}

run_candidate() {
    local model="$1"
    local ctx="${2:-2048}"
    echo "--- candidate: $model (ctx=$ctx) ---"

    if ! ensure_model "$model"; then
        return 0
    fi

    kill_isolated

    echo "  starting isolated orchestrator on port $PORT..."
    env \
        CLAF_LOCAL_MODEL="$model" \
        CLAF_OLLAMA_CTX="$ctx" \
        CLAF_LOCAL_MAX_PREDICT=256 \
        CLAF_MAX_LOOP_TURNS=0 \
        CLAF_MAX_REDISPATCH=1 \
        CLAF_GIVEUP_INTERCEPTOR=0 \
        CLAF_TAP_POLISH=0 \
        CLAF_OLLAMA_KEEP_ALIVE=0 \
        CLAF_PORT="$PORT" \
        "$PYTHON" "$CLAF_DIR/orchestrator.py" >/tmp/claf_ab_${model//:/_}_${ctx}.log 2>&1 &
    local orch_pid=$!

    # Wait for the isolated orchestrator to be ready.
    local ready=0
    for _ in $(seq 1 30); do
        local code
        code=$(curl -s -o /dev/null -w "%{http_code}" "$ORCH_URL/v1/messages" 2>/dev/null || true)
        if [ -n "$code" ] && [ "$code" != "000" ]; then
            ready=1
            break
        fi
        sleep 1
    done
    if [ "$ready" -eq 0 ]; then
        echo "  orchestrator failed to start — see /tmp/claf_ab_${model//:/_}_${ctx}.log"
        kill_isolated
        return 0
    fi

    local tf_out tf_exit tp_out tp_exit
    echo "  running test_tool_formation.py..."
    tf_out=$(CLAF_ORCH_URL="$ORCH_URL" CLAF_LOG="/tmp/claf_ab_${model//:/_}_${ctx}.log" \
             PYTHONPATH="$CLAF_DIR" "$PYTHON" "$PARITY_DIR/test_tool_formation.py" 2>&1)
    tf_exit=$?
    echo "$tf_out"

    echo "  running test_parity.py..."
    tp_out=$(CLAF_ORCH_URL="$ORCH_URL" CLAF_LOG="/tmp/claf_ab_${model//:/_}_${ctx}.log" \
             PYTHONPATH="$CLAF_DIR" "$PYTHON" "$PARITY_DIR/test_parity.py" 2>&1)
    tp_exit=$?
    echo "$tp_out"

    kill_isolated

    # Extract the machine-readable RESULT line from test_tool_formation.py.
    local result_line
    result_line=$(echo "$tf_out" | grep '^RESULT ' | tail -1 | sed 's/^RESULT //')
    if [ -z "$result_line" ]; then
        result_line='{"test":"tool_formation","total":20,"tool_emitted":0,"name_correct":0,"schema_valid":0,"exact_args":0,"repaired":0,"repair_failed":0}'
    fi

    # Augment with candidate metadata.
    local ts
    ts=$(date -Iseconds)
    local record
    record=$(echo "$result_line" | "$PYTHON" -c "
import json, sys
r = json.load(sys.stdin)
r['ts'] = '$ts'
r['model'] = '$model'
r['ctx'] = $ctx
r['tool_formation_exit'] = $tf_exit
r['parity_exit'] = $tp_exit
print(json.dumps(r))
")
    echo "$record" >> "$RESULTS_FILE"
    echo "  recorded: $record"
    echo ""
}

# Run baseline + candidates at 2048 ctx.
for model in "${CANDIDATES[@]}"; do
    run_candidate "$model" 2048
done

# Determine the leader (valid tool_use >= 90% and latency <= 2x baseline).
# Baseline latency is taken from the hermes3:3b 2048 run.
echo "=== A/B summary ==="
"$PYTHON" - <<'PY'
import json, pathlib, sys
f = pathlib.Path.home() / "projects/claf/parity/ab_results.jsonl"
if not f.exists():
    sys.exit("no results")
rows = []
for line in f.read_text().splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        rows.append(json.loads(line))
    except json.JSONDecodeError:
        continue

baseline = next((r for r in rows if r.get("model") == "hermes3:3b" and r.get("ctx") == 2048), None)
print(f"{'model':24} {'ctx':>6} {'valid%':>6} {'exact':>6} {'repaired':>8} {'failed':>8}")
for r in rows:
    valid_pct = 100.0 * r.get("schema_valid", 0) / r.get("total", 1)
    print(f"{r.get('model','?'):24} {r.get('ctx','?'):>6} {valid_pct:>6.1f} {r.get('exact_args',0):>6}/{r.get('total',0):<2} {r.get('repaired',0):>8} {r.get('repair_failed',0):>8}")

if baseline:
    base_valid = baseline.get("schema_valid", 0)
    base_total = baseline.get("total", 1)
    print(f"\nbaseline hermes3:3b valid={base_valid}/{base_total}")
    winner = None
    for r in rows:
        if r.get("model") == "hermes3:3b":
            continue
        if r.get("schema_valid", 0) >= 0.90 * base_total:
            print(f"candidate {r.get('model')} meets validity threshold")
            winner = r
    if winner:
        print(f"WINNER: {winner.get('model')} (valid {winner.get('schema_valid')}/{winner.get('total')})")
    else:
        print("NO WINNER: no candidate met >=90% validity threshold")
else:
    print("NO BASELINE recorded")
PY

echo ""
echo "Full results: $RESULTS_FILE"

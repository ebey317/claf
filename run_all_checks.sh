#!/usr/bin/env bash
# CLAF unified pre-flight / regression runner (Session 7 C31).
#
# Runs the fast smoke tests plus the Session 7 parity harnesses against the
# live orchestrator on localhost:8000.  Fails if any required check fails.
#
# Usage:
#   bash ~/projects/claf/run_all_checks.sh
#
# Optional env:
#   CLAF_PROXY_URL      orchestrator base URL (default http://localhost:8000)
#   SKIP_SLOW=1         skip test_tool_formation.py and stress_harness.py

set -uo pipefail

PROXY_URL="${CLAF_PROXY_URL:-http://localhost:8000}"
CLAF_DIR="${CLAF_DIR:-$HOME/projects/claf}"
PARITY_DIR="$CLAF_DIR/parity"
PYTHON="${PYTHON:-python3}"

# Prefer project venv when deps are present.
if [ -f "$CLAF_DIR/.venv/bin/python3" ]; then
    if "$CLAF_DIR/.venv/bin/python3" -c "import fastapi, httpx" 2>/dev/null; then
        PYTHON="$CLAF_DIR/.venv/bin/python3"
    fi
fi

export PYTHONPATH="$CLAF_DIR${PYTHONPATH:+:$PYTHONPATH}"

echo "=== CLAF run_all_checks ==="
echo "orchestrator: $PROXY_URL"
echo "python: $PYTHON"
echo ""

# ── CLAF must be up ─────────────────────────────────────────────────────────
if ! curl -fsS "$PROXY_URL/healthz" >/dev/null 2>&1; then
    echo "FAIL: CLAF not running at $PROXY_URL"
    echo "Start it first: systemctl --user restart claf"
    exit 1
fi

PASS=0
FAIL=0
SKIPPED=0

run_check() {
    local name="$1"
    shift
    echo "--- $name ---"
    if "$@"; then
        echo "PASS: $name"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $name"
        FAIL=$((FAIL + 1))
    fi
    echo ""
}

# ── Smoke tests ─────────────────────────────────────────────────────────────
run_check "routing_smoke" bash "$CLAF_DIR/test_claf_routing.sh"
run_check "v2_smoke" bash "$CLAF_DIR/test_v2.sh"

# ── Continuation-guard regression ───────────────────────────────────────────
run_check "continuation_guard" "$PYTHON" "$PARITY_DIR/test_continuation_guard.py"

# ── JSON repair parity ──────────────────────────────────────────────────────
run_check "json_repair" "$PYTHON" "$PARITY_DIR/test_json_repair.py"

# ── Account-separation regression guard ─────────────────────────────────────
run_check "account_separation" bash "$CLAF_DIR/scripts/verify_account_separation.sh"

# ── Tool-formation corpus (slow; ~3-5 min local) ────────────────────────────
if [ "${SKIP_SLOW:-0}" = "1" ]; then
    echo "--- tool_formation (skipped: SKIP_SLOW=1) ---"
    SKIPPED=$((SKIPPED + 1))
    echo ""
else
    run_check "tool_formation" "$PYTHON" "$PARITY_DIR/test_tool_formation.py"
fi

# ── Agentic stress harness (slowest; ~5 min) ────────────────────────────────
if [ "${SKIP_SLOW:-0}" = "1" ]; then
    echo "--- stress_harness (skipped: SKIP_SLOW=1) ---"
    SKIPPED=$((SKIPPED + 1))
    echo ""
else
    # Only S1 is required to PASS; S2-S5 are documented known-fail.
    run_check "stress_harness_s1" "$PYTHON" "$PARITY_DIR/stress_harness.py" --scenario s1
fi

# ── Summary ─────────────────────────────────────────────────────────────────
echo "=== SUMMARY ==="
echo "Passed:  $PASS"
echo "Failed:  $FAIL"
echo "Skipped: $SKIPPED"

if [ "$FAIL" -gt 0 ]; then
    echo ""
    echo "OVERALL: FAIL"
    exit 1
fi

echo ""
echo "OVERALL: PASS"
exit 0

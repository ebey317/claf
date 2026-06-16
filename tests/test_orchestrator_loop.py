"""Tests for CLAF orchestrator long-loop hardening helpers."""
import os
import sys
from pathlib import Path

# Use the project venv packages and make orchestrator importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import orchestrator as orch


def _make_tool_history(cycles: int) -> list[dict]:
    """Build a fake Anthropic history with `cycles` tool_use/tool_result pairs."""
    msgs = []
    for i in range(cycles):
        msgs.append({
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": f"tu_{i}",
                    "name": "Read",
                    "input": {"file_path": f"/tmp/file_{i}.txt"},
                }
            ],
        })
        msgs.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": f"tu_{i}",
                    "content": f"This is the long result for cycle {i} " * 50,
                }
            ],
        })
    return msgs


def test_state_done_when_no_tools():
    state, info = orch._derive_loop_state([], tools=None)
    assert state == orch._LoopState.DONE
    assert info["total_cycles"] == 0


def test_state_idle_with_tools_and_no_history():
    state, info = orch._derive_loop_state([], tools=[{"name": "Read"}])
    assert state == orch._LoopState.IDLE


def test_state_observing_after_tool_result():
    msgs = _make_tool_history(1)
    state, info = orch._derive_loop_state(msgs, tools=[{"name": "Read"}])
    assert state == orch._LoopState.OBSERVING
    assert info["total_cycles"] == 1


def test_state_acting_after_tool_use_without_result():
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_0", "name": "Read", "input": {}}
            ],
        }
    ]
    state, info = orch._derive_loop_state(msgs, tools=[{"name": "Read"}])
    assert state == orch._LoopState.ACTING


def test_state_summarizing_after_loop_turn_cap():
    os.environ["CLAF_MAX_LOOP_TURNS"] = "3"
    os.environ["CLAF_MAX_REPLAN_EPOCHS"] = "2"
    os.environ["CLAF_MAX_TOTAL_TOOL_TURNS"] = "12"
    msgs = _make_tool_history(3)
    state, info = orch._derive_loop_state(msgs, tools=[{"name": "Read"}])
    assert state == orch._LoopState.SUMMARIZING, f"got {state} with info {info}"
    assert info["cycles_since_reset"] == 3


def test_state_paused_after_max_epochs():
    os.environ["CLAF_MAX_LOOP_TURNS"] = "2"
    os.environ["CLAF_MAX_REPLAN_EPOCHS"] = "1"
    os.environ["CLAF_MAX_TOTAL_TOOL_TURNS"] = "12"
    # Two cycles plus an injected reset marker simulating one used epoch.
    msgs = _make_tool_history(2)
    msgs.append({"role": "user", "content": "[CLAF-LOOP-RESET epoch=1/1] keep going"})
    msgs.extend(_make_tool_history(2))
    state, info = orch._derive_loop_state(msgs, tools=[{"name": "Read"}])
    assert state == orch._LoopState.PAUSED, f"got {state} with info {info}"


def test_state_paused_on_absolute_total_cap():
    os.environ["CLAF_MAX_LOOP_TURNS"] = "10"
    os.environ["CLAF_MAX_REPLAN_EPOCHS"] = "3"
    os.environ["CLAF_MAX_TOTAL_TOOL_TURNS"] = "4"
    msgs = _make_tool_history(5)
    state, info = orch._derive_loop_state(msgs, tools=[{"name": "Read"}])
    assert state == orch._LoopState.PAUSED
    assert info["total_cycles"] == 5


def test_compress_history_keeps_recent_pairs():
    msgs = _make_tool_history(3)
    compressed = orch._compress_loop_history(msgs, keep_recent_pairs=2, max_tool_result_chars=80)
    # Recent pair (cycle 2) should be unchanged.
    assert compressed[-1]["content"][0]["content"].startswith("This is the long result for cycle 2")
    # Oldest pair (cycle 0) should be compressed/summarized.
    old_result = compressed[1]["content"][0]
    assert "[summarized" in old_result["content"] or len(old_result["content"]) <= 120


def test_compress_history_shortens_long_results():
    msgs = _make_tool_history(2)
    compressed = orch._compress_loop_history(msgs, keep_recent_pairs=1, max_tool_result_chars=60)
    # The non-recent result should be shortened.
    assert len(compressed[1]["content"][0]["content"]) <= 120


if __name__ == "__main__":
    test_state_done_when_no_tools()
    test_state_idle_with_tools_and_no_history()
    test_state_observing_after_tool_result()
    test_state_acting_after_tool_use_without_result()
    test_state_summarizing_after_loop_turn_cap()
    test_state_paused_after_max_epochs()
    test_state_paused_on_absolute_total_cap()
    test_compress_history_keeps_recent_pairs()
    test_compress_history_shortens_long_results()
    print("ok")

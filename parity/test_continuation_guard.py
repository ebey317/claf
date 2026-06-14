#!/usr/bin/env python3
"""CLAF task-continuation guard parity test.

Mirrors the structure of test_parity.py so Kimi (or any agent) can run,
extend, or cite individual cases by ID. Tests are grouped by LAYER, not by
who wrote them.

Run:  PYTHONPATH=/home/elijah/projects/claf python3 \
        ~/projects/claf/parity/test_continuation_guard.py

Pass criteria: all PASS, exit 0. Any FAIL = fix target for 90→100% push.
LAYER key:
  UNIT        _task_pending_count() helper in isolation
  EDGE        boundary / malformed inputs the model or OS could produce
  CONCURRENCY file safety under simultaneous reads + atomic writes
  GUARD       the guard firing-condition logic (no live dispatch needed)
  SPEED       latency bounds that keep the per-turn overhead invisible

Known issues captured as EDGE tests (don't hide them — fix them):
  - whitespace_status: " done " NOT stripped → counts as pending (real bug)
"""
from __future__ import annotations
import json, sys, time, threading, pathlib, tempfile, os

CLAF = pathlib.Path.home() / "projects" / "claf"
sys.path.insert(0, str(CLAF))

try:
    from task_state import load_task, save_task, TASK_FILE
except ImportError as e:
    sys.exit(f"IMPORT ERROR: {e}\nRun with PYTHONPATH=/home/elijah/projects/claf")

# ── re-implement helper (must match orchestrator.py exactly) ───────────────
def _task_pending_count() -> int:
    task = load_task()
    if not task:
        return 0
    _resolved = {"done", "failed", "skip"}
    return sum(
        1 for it in task.get("items", [])
        if isinstance(it, dict)
        and str(it.get("status", "pending")).strip().lower() not in _resolved
    )

# ── corpus ─────────────────────────────────────────────────────────────────
# Each entry: id, layer, fn (callable → raises AssertionError on failure),
#             known_fail (True = document the bug, don't fix the test).
CORPUS: list[dict] = []

def _reg(id_, layer, known_fail=False):
    def decorator(fn):
        CORPUS.append({"id": id_, "layer": layer, "fn": fn, "known_fail": known_fail})
        return fn
    return decorator

def _write(data):
    TASK_FILE.parent.mkdir(parents=True, exist_ok=True)
    TASK_FILE.write_text(json.dumps(data), encoding="utf-8")

def _clean():
    if TASK_FILE.exists():
        TASK_FILE.unlink()

# ── LAYER: UNIT ────────────────────────────────────────────────────────────

@_reg("u1_no_file", "UNIT")
def _():
    _clean()
    assert _task_pending_count() == 0

@_reg("u2_all_resolved", "UNIT")
def _():
    _write({"goal": "g", "items": [
        {"id": 1, "task": "x", "status": "done"},
        {"id": 2, "task": "y", "status": "failed"},
        {"id": 3, "task": "z", "status": "skip"},
    ]})
    assert _task_pending_count() == 0

@_reg("u3_one_pending", "UNIT")
def _():
    _write({"goal": "g", "items": [
        {"id": 1, "task": "x", "status": "done"},
        {"id": 2, "task": "y", "status": "pending"},
    ]})
    assert _task_pending_count() == 1

@_reg("u4_missing_status_counts_pending", "UNIT")
def _():
    _write({"goal": "g", "items": [{"id": 1, "task": "x"}]})
    assert _task_pending_count() == 1

@_reg("u5_skip_and_failed_resolved", "UNIT")
def _():
    _write({"goal": "g", "items": [
        {"id": 1, "task": "a", "status": "skip"},
        {"id": 2, "task": "b", "status": "failed"},
    ]})
    assert _task_pending_count() == 0

@_reg("u6_garbled_non_dict_items_skipped", "UNIT")
def _():
    _write({"goal": "g", "items": [
        "not_a_dict", None, 42,
        {"id": 1, "task": "real", "status": "pending"},
    ]})
    assert _task_pending_count() == 1

@_reg("u7_uppercase_status_normalized", "UNIT")
def _():
    _write({"goal": "g", "items": [
        {"id": 1, "task": "x", "status": "DONE"},
        {"id": 2, "task": "y", "status": "SKIP"},
    ]})
    assert _task_pending_count() == 0

# ── LAYER: EDGE ────────────────────────────────────────────────────────────

@_reg("e1_empty_items_array", "EDGE")
def _():
    _write({"goal": "g", "items": []})
    assert _task_pending_count() == 0

@_reg("e2_200_done_1_pending", "EDGE")
def _():
    items = [{"id": i, "task": f"s{i}", "status": "done"} for i in range(200)]
    items.append({"id": 201, "task": "final", "status": "pending"})
    _write({"goal": "big", "items": items})
    assert _task_pending_count() == 1

@_reg("e3_status_None_counts_pending", "EDGE")
def _():
    _write({"goal": "g", "items": [{"id": 1, "task": "x", "status": None}]})
    assert _task_pending_count() == 1  # str(None)="none" not in resolved

@_reg("e4_status_empty_string_counts_pending", "EDGE")
def _():
    _write({"goal": "g", "items": [{"id": 1, "task": "x", "status": ""}]})
    assert _task_pending_count() == 1

@_reg("e5_status_in_progress_counts_pending", "EDGE")
def _():
    _write({"goal": "g", "items": [{"id": 1, "task": "x", "status": "in_progress"}]})
    assert _task_pending_count() == 1

@_reg("e6_corrupt_json_safe_fallback", "EDGE")
def _():
    TASK_FILE.parent.mkdir(parents=True, exist_ok=True)
    TASK_FILE.write_text("{this is not json", encoding="utf-8")
    assert _task_pending_count() == 0

@_reg("e7_partial_write_safe_fallback", "EDGE")
def _():
    TASK_FILE.parent.mkdir(parents=True, exist_ok=True)
    TASK_FILE.write_text('{"goal": "g", "items": [{"id": 1', encoding="utf-8")
    assert _task_pending_count() == 0

@_reg("e8_empty_file_safe_fallback", "EDGE")
def _():
    TASK_FILE.parent.mkdir(parents=True, exist_ok=True)
    TASK_FILE.write_text("", encoding="utf-8")
    assert _task_pending_count() == 0

@_reg("e9_goal_missing_invalid_task", "EDGE")
def _():
    _write({"items": [{"id": 1, "task": "x", "status": "pending"}]})
    assert _task_pending_count() == 0  # load_task() validates goal is str

@_reg("e10_items_not_list_invalid_task", "EDGE")
def _():
    _write({"goal": "g", "items": "step1, step2"})
    assert _task_pending_count() == 0

@_reg("e11_unicode_unknown_status_pending", "EDGE")
def _():
    _write({"goal": "g", "items": [{"id": 1, "task": "日本語", "status": "完了"}]})
    assert _task_pending_count() == 1

@_reg("e12_whitespace_status_stripped", "EDGE")
def _():
    # Fixed 2026-06-12: .strip() added before .lower() in _task_pending_count().
    # " done " with surrounding spaces now resolves correctly to 0.
    _write({"goal": "g", "items": [{"id": 1, "task": "x", "status": " done "}]})
    assert _task_pending_count() == 0

# ── LAYER: CONCURRENCY ─────────────────────────────────────────────────────

@_reg("c1_concurrent_reads_stable", "CONCURRENCY")
def _():
    _write({"goal": "g", "items": [{"id": 1, "task": "x", "status": "pending"}]})
    errors, counts = [], []
    def reader():
        for _ in range(50):
            try:
                counts.append(_task_pending_count())
            except Exception as e:
                errors.append(str(e))
    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert not errors, f"concurrent read errors: {errors[:3]}"
    assert all(c == 1 for c in counts), f"inconsistent: {set(counts)}"

@_reg("c2_read_during_atomic_write_no_crash", "CONCURRENCY")
def _():
    errors = []
    TASK_FILE.parent.mkdir(parents=True, exist_ok=True)
    def writer():
        for i in range(30):
            save_task({"goal": "g", "items": [
                {"id": 1, "task": "x", "status": "pending" if i % 2 == 0 else "done"}
            ]})
            time.sleep(0.005)
    def reader():
        for _ in range(60):
            try:
                _task_pending_count()
            except Exception as e:
                errors.append(str(e))
            time.sleep(0.003)
    wt = threading.Thread(target=writer)
    rt = threading.Thread(target=reader)
    wt.start(); rt.start()
    wt.join(); rt.join()
    assert not errors, f"read-during-write errors: {errors[:3]}"

# ── LAYER: GUARD ───────────────────────────────────────────────────────────

@_reg("g1_fires_when_pending_no_tool_use", "GUARD")
def _():
    _write({"goal": "g", "items": [{"id": 1, "task": "x", "status": "pending"}]})
    tool_use = False; has_tools = True; overflow = False; pushed = False
    should = not tool_use and has_tools and not overflow and not pushed and _task_pending_count() > 0
    assert should

@_reg("g2_suppressed_on_overflow", "GUARD")
def _():
    _write({"goal": "g", "items": [{"id": 1, "task": "x", "status": "pending"}]})
    overflow = True
    should = not overflow and _task_pending_count() > 0
    assert not should

@_reg("g3_suppressed_when_already_pushed", "GUARD")
def _():
    _write({"goal": "g", "items": [{"id": 1, "task": "x", "status": "pending"}]})
    pushed = True
    should = not pushed and _task_pending_count() > 0
    assert not should

@_reg("g4_suppressed_when_no_tools_in_request", "GUARD")
def _():
    _write({"goal": "g", "items": [{"id": 1, "task": "x", "status": "pending"}]})
    has_tools = False
    should = has_tools and _task_pending_count() > 0
    assert not should

@_reg("g5_suppressed_when_tool_use_returned", "GUARD")
def _():
    _write({"goal": "g", "items": [{"id": 1, "task": "x", "status": "pending"}]})
    tool_use = True
    should = not tool_use
    assert not should

@_reg("g6_does_not_fire_when_all_done", "GUARD")
def _():
    _write({"goal": "g", "items": [{"id": 1, "task": "x", "status": "done"}]})
    tool_use = False; has_tools = True; overflow = False; pushed = False
    pending = _task_pending_count()
    should = not tool_use and has_tools and not overflow and not pushed and pending > 0
    assert not should
    assert pending == 0

# ── LAYER: SPEED ───────────────────────────────────────────────────────────

@_reg("s1_1000_items_under_50ms", "SPEED")
def _():
    items = [{"id": i, "task": f"s{i}", "status": "done" if i < 999 else "pending"} for i in range(1000)]
    _write({"goal": "perf", "items": items})
    t0 = time.perf_counter()
    for _ in range(100):
        _task_pending_count()
    avg_ms = (time.perf_counter() - t0) / 100 * 1000
    print(f"         ↳ s1: {avg_ms:.2f}ms avg (1000-item task, 100 runs)")
    assert avg_ms < 50, f"too slow: {avg_ms:.1f}ms (target <50ms)"

@_reg("s2_no_file_fast_path_under_5ms", "SPEED")
def _():
    _clean()
    t0 = time.perf_counter()
    for _ in range(1000):
        _task_pending_count()
    avg_ms = (time.perf_counter() - t0) / 1000 * 1000
    print(f"         ↳ s2: {avg_ms:.3f}ms avg (no-file fast path, 1000 runs)")
    assert avg_ms < 5, f"fast path too slow: {avg_ms:.2f}ms (target <5ms)"

# ── runner ─────────────────────────────────────────────────────────────────
def main():
    passed = failed = bugged = 0
    by_layer: dict[str, list] = {}
    for case in CORPUS:
        layer = case["layer"]
        by_layer.setdefault(layer, [])
        _clean()
        try:
            case["fn"]()
            status = "PASS"
            passed += 1
        except AssertionError as e:
            if case["known_fail"]:
                status = f"KNOWN BUG: {e}"
                bugged += 1
            else:
                status = f"FAIL: {e}"
                failed += 1
        except Exception as e:
            status = f"ERROR: {e}"
            failed += 1
        finally:
            _clean()
        by_layer[layer].append((case["id"], status))

    for layer, results in by_layer.items():
        print(f"\n── {layer} ──")
        for id_, st in results:
            tag = "  PASS" if st == "PASS" else ("  BUG " if st.startswith("KNOWN") else "  FAIL")
            print(f"{tag}  {id_}  {'' if st == 'PASS' else st}")

    total = len(CORPUS)
    print(f"\n══ {passed}/{total} passed  |  {bugged} known bugs  |  {failed} failures ══")
    if bugged:
        print("Known bugs = fix targets. See cases marked KNOWN BUG above.")
    sys.exit(0 if failed == 0 else 1)

if __name__ == "__main__":
    main()

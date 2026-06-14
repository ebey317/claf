"""Persistent task state for CLAF agent loops.

Survives loop caps and context resets. The orchestrator injects the active
task at every turn start so the model knows exactly where it is.

Usage flow:
  - Model writes ~/.claf/current_task.json (via Write tool) to start a task
  - Orchestrator reads + injects it at the top of system_text each turn
  - Model updates item status by rewriting the file (status: done/failed/skip)
  - When all items are done, model deletes the file (task complete)
"""

import json
import os
import pathlib
import tempfile
import threading

TASK_FILE = pathlib.Path.home() / ".claf" / "current_task.json"

_task_cache_lock = threading.Lock()
_task_cache_key = None
_task_cache_value = None


def _invalidate_task_cache() -> None:
    global _task_cache_key, _task_cache_value
    _task_cache_key = None
    _task_cache_value = None


def load_task() -> dict | None:
    """Return the current task dict, or None if none is active / file is corrupt.

    Cached by (mtime_ns, size) so repeated per-turn reads cost one stat()
    instead of a disk read + JSON parse.
    """
    global _task_cache_key, _task_cache_value
    with _task_cache_lock:
        try:
            if not TASK_FILE.exists():
                _invalidate_task_cache()
                return None
            st = TASK_FILE.stat()
            key = (st.st_mtime_ns, st.st_size)
            if key == _task_cache_key:
                return _task_cache_value
            raw = TASK_FILE.read_text(encoding="utf-8").strip()
            if not raw:
                _invalidate_task_cache()
                return None
            task = json.loads(raw)
            # Minimal validation
            if not isinstance(task.get("goal"), str) or not isinstance(task.get("items"), list):
                _invalidate_task_cache()
                return None
            _task_cache_key = key
            _task_cache_value = task
            return task
        except Exception:
            _invalidate_task_cache()
            return None


def save_task(task: dict) -> None:
    """Write task atomically."""
    TASK_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TASK_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(task, indent=2), encoding="utf-8")
    tmp.replace(TASK_FILE)
    _invalidate_task_cache()


def task_belongs_to(task: dict | None, conv_fp: str) -> bool:
    """Return True if the task was created for the given conversation.

    Tasks written before conversation binding was added (no conv_fp) are
    treated as belonging to the current conversation so the guard does not
    aggressively delete model-created tasks that legitimately lack a stamp.
    Auto-seeded tasks always carry a conv_fp.
    """
    if task is None:
        return False
    _task_fp = task.get("conv_fp")
    if not _task_fp:
        return True  # legacy / model-written task: assume current conv
    return str(_task_fp) == str(conv_fp)


def format_task_for_injection(task: dict) -> str:
    """Compact representation injected at the top of system_text.

    Tolerates malformed items (small local models mangle JSON) — a bad item
    renders as a placeholder instead of 500ing every local turn.
    """
    lines = ["[ACTIVE TASK]", f"Goal: {task['goal']}"]

    if task.get("strategy"):
        lines.append(f"Strategy: {task['strategy']}")

    success = task.get("success_criteria")
    if isinstance(success, list) and success:
        lines.append("Success criteria:")
        for criterion in success:
            lines.append(f"  - {criterion}")

    fallbacks = task.get("fallback_chain")
    if isinstance(fallbacks, list) and fallbacks:
        lines.append("Fallback chain:")
        for step in fallbacks:
            lines.append(f"  {step}")

    items = [i for i in task.get("items", []) if isinstance(i, dict)]
    for item in items:
        status = item.get("status", "pending")
        icon = "✅" if status == "done" else ("❌" if status == "failed" else "⬜")
        note = f" — {item['note']}" if item.get("note") else ""
        lines.append(f"{item.get('id', '?')}. {icon} {item.get('task', '<unnamed item>')}{note}")
    pending = sum(1 for i in items if i.get("status", "pending") == "pending")
    lines.append(f"({pending} item(s) remaining — update this file as you complete each one)")
    return "\n".join(lines)

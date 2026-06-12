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

TASK_FILE = pathlib.Path.home() / ".claf" / "current_task.json"


def load_task() -> dict | None:
    """Return the current task dict, or None if none is active / file is corrupt."""
    try:
        if not TASK_FILE.exists():
            return None
        raw = TASK_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        task = json.loads(raw)
        # Minimal validation
        if not isinstance(task.get("goal"), str) or not isinstance(task.get("items"), list):
            return None
        return task
    except Exception:
        return None


def save_task(task: dict) -> None:
    """Write task atomically."""
    TASK_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TASK_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(task, indent=2), encoding="utf-8")
    tmp.replace(TASK_FILE)


def format_task_for_injection(task: dict) -> str:
    """Compact representation injected at the top of system_text.

    Tolerates malformed items (small local models mangle JSON) — a bad item
    renders as a placeholder instead of 500ing every local turn.
    """
    lines = ["[ACTIVE TASK]", f"Goal: {task['goal']}"]
    items = [i for i in task.get("items", []) if isinstance(i, dict)]
    for item in items:
        status = item.get("status", "pending")
        icon = "✅" if status == "done" else ("❌" if status == "failed" else "⬜")
        note = f" — {item['note']}" if item.get("note") else ""
        lines.append(f"{item.get('id', '?')}. {icon} {item.get('task', '<unnamed item>')}{note}")
    pending = sum(1 for i in items if i.get("status", "pending") == "pending")
    lines.append(f"({pending} item(s) remaining — update this file as you complete each one)")
    return "\n".join(lines)

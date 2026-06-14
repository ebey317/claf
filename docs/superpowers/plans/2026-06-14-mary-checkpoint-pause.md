# Mary Checkpoint-Pause Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a human-in-the-loop checkpoint to `engagement_loop.py` so Mary pauses every `N` steps, writes a resume summary to `~/MD/notepad.md`, and resumes on the next loop run.

**Architecture:** Keep the change minimal and testable by adding pure helper functions (`_checkpoint_needed`, `_build_checkpoint_text`, `_write_checkpoint`, `_load_checkpoint`, `_clear_checkpoint`) and extending `_mark_task`/`run_once` to claim, pause, and resume `⏸️` tasks. A JSON checkpoint file stores progress; `notepad.md` stores the human-readable summary.

**Tech Stack:** Python 3, stdlib (`json`, `pathlib`, `datetime`), existing `claf_permissions` import, `pytest` for tests.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `toolbox/engagement_loop.py` | Core loop; add checkpoint helpers and pause/resume logic |
| `tests/test_engagement_loop.py` | Unit tests for checkpoint helpers and `_mark_task` |
| `charter/charter_tasks.md` | Document the checkpoint protocol for future agents |
| `docs/superpowers/plans/2026-06-14-mary-checkpoint-pause.md` | This plan |

---

### Task 1: Add checkpoint constants and pure helpers

**Files:**
- Modify: `toolbox/engagement_loop.py:20-26`

- [ ] **Step 1: Add `CHECKPOINT_FILE`, `NOTEPAD`, and `CHECKPOINT_EVERY` constants**

Insert after the existing path/env constants block:

```python
HANDOFF = Path.home() / "MD" / "HANDOFF.md"
ENGAGEMENT_LOG = Path.home() / ".claf" / "engagement.log"
CHECKPOINT_FILE = Path.home() / ".claf" / "engagement_checkpoint.json"
NOTEPAD = Path.home() / "MD" / "notepad.md"
CLAF_URL = os.environ.get("CLAF_URL", "http://localhost:8000/v1/messages")
MAX_TURNS = int(os.environ.get("CLAF_ENGAGEMENT_MAX_TURNS", "20"))
CHECKPOINT_EVERY = int(os.environ.get("CLAF_CHECKPOINT_EVERY", "5"))
```

- [ ] **Step 2: Add pure checkpoint helpers after `_add_engagement_qa`**

Insert before `run_once`:

```python
def _checkpoint_needed(turn: int, every: int = CHECKPOINT_EVERY) -> bool:
    """Return True when `turn` is a positive multiple of `every`."""
    return every > 0 and turn % every == 0


def _build_checkpoint_text(
    task_name: str,
    completed: int,
    total: int,
    qa: list[tuple[str, str]],
    remaining_steps: list[str],
) -> str:
    """Return a human-readable checkpoint block for notepad.md."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"## Checkpoint — {task_name} — {now}",
        "",
        f"Progress: {completed}/{total} steps completed.",
        "",
        "### Done so far",
    ]
    for q, a in qa:
        lines.append(f"- {q}: {str(a)[:120]}")
    if not qa:
        lines.append("- (no steps completed yet)")
    lines.extend(["", "### Next"])
    for step in remaining_steps:
        lines.append(f"- {step}")
    if not remaining_steps:
        lines.append("- (no remaining steps)")
    lines.extend([
        "",
        "Status: PAUSED — run `python3 ~/projects/claf/toolbox/engagement_loop.py` to continue.",
        "",
    ])
    return "\n".join(lines)


def _write_checkpoint(
    name: str,
    completed: int,
    total: int,
    qa: list[tuple[str, str]],
    remaining: list[str],
) -> None:
    """Persist machine-readable checkpoint and append human summary to notepad."""
    data = {
        "task_name": name,
        "completed_steps": completed,
        "total_steps": total,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    note = _build_checkpoint_text(name, completed, total, qa, remaining)
    NOTEPAD.parent.mkdir(parents=True, exist_ok=True)
    with NOTEPAD.open("a", encoding="utf-8") as f:
        f.write("\n" + note + "\n")


def _load_checkpoint() -> dict | None:
    """Load the machine-readable checkpoint, or None if missing/invalid."""
    if not CHECKPOINT_FILE.exists():
        return None
    try:
        return json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _clear_checkpoint() -> None:
    """Remove the checkpoint file when a task finishes or is blocked."""
    if CHECKPOINT_FILE.exists():
        CHECKPOINT_FILE.unlink()
```

- [ ] **Step 3: Commit**

```bash
cd ~/projects/claf
git add toolbox/engagement_loop.py
git commit -m "feat(engagement_loop): add checkpoint constants and helpers"
```

---

### Task 2: Write failing tests for checkpoint helpers

**Files:**
- Create: `tests/test_engagement_loop.py`

- [ ] **Step 1: Create the test file with the first failing tests**

```python
"""Tests for engagement_loop checkpoint helpers."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "toolbox"))

import engagement_loop as el


def test_checkpoint_needed_every_five():
    assert el._checkpoint_needed(5, 5) is True
    assert el._checkpoint_needed(10, 5) is True
    assert el._checkpoint_needed(4, 5) is False


def test_checkpoint_needed_zero_disabled():
    assert el._checkpoint_needed(5, 0) is False


def test_build_checkpoint_text():
    qa = [("Step 1: run ls", "file.txt")]
    remaining = ["Step 2: run pwd"]
    text = el._build_checkpoint_text("Test Task", 1, 2, qa, remaining)
    assert "Checkpoint — Test Task" in text
    assert "1/2 steps completed" in text
    assert "Step 1: run ls" in text
    assert "Step 2: run pwd" in text
    assert "PAUSED" in text


def test_mark_task_paused():
    block = "### ⏳ KIMI — Demo\n**Do this exactly:**\n1. run ls\n"
    text = el._mark_task(block, 0, len(block), "paused", "1/2 paused")
    assert "### ⏸️ KIMI — Demo" in text
    assert "PAUSED" in text
    assert "1/2 paused" in text


def test_mark_task_done():
    block = "### 🔄 KIMI — Demo\n**Do this exactly:**\n1. run ls\n"
    text = el._mark_task(block, 0, len(block), "done", "completed")
    assert "### ✅ KIMI — Demo" in text
    assert "DONE" in text


if __name__ == "__main__":
    test_checkpoint_needed_every_five()
    test_checkpoint_needed_zero_disabled()
    test_build_checkpoint_text()
    test_mark_task_paused()
    test_mark_task_done()
    print("ok")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/projects/claf
pytest tests/test_engagement_loop.py -v
```

Expected: FAIL with `AttributeError: module 'engagement_loop' has no attribute '_checkpoint_needed'` (helpers not yet added).

- [ ] **Step 3: Commit**

```bash
cd ~/projects/claf
git add tests/test_engagement_loop.py
git commit -m "test(engagement_loop): add checkpoint helper tests"
```

---

### Task 3: Extend `_mark_task` to support paused status

**Files:**
- Modify: `toolbox/engagement_loop.py:262-272`

- [ ] **Step 1: Add `paused` branch to `_mark_task`**

Replace the existing `_mark_task` body:

```python
def _mark_task(text: str, start: int, end: int, status: str, result: str) -> str:
    """Replace ⏳/🔄 with ✅, ⛔, or ⏸️ and append engagement summary."""
    block = text[start:end]
    today = datetime.now().strftime("%Y-%m-%d")
    if status == "done":
        new_heading = block.replace("### ⏳", "### ✅", 1).replace("### 🔄", "### ✅", 1)
        summary = f"\n**Engagement result (DONE {today}):** {result}\n"
    elif status == "paused":
        new_heading = block.replace("### ⏳", "### ⏸️", 1).replace("### 🔄", "### ⏸️", 1)
        summary = f"\n**Engagement result (PAUSED {today}):** {result}\n"
    else:
        new_heading = block.replace("### ⏳", "### ⛔ BLOCKED", 1).replace("### 🔄", "### ⛔ BLOCKED", 1)
        summary = f"\n**Engagement result (BLOCKED {today}):** {result}\n"
    new_block = new_heading.rstrip() + summary
    return text[:start] + new_block + text[end:]
```

- [ ] **Step 2: Run tests**

```bash
cd ~/projects/claf
pytest tests/test_engagement_loop.py -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
cd ~/projects/claf
git add toolbox/engagement_loop.py tests/test_engagement_loop.py
git commit -m "feat(engagement_loop): support paused status in _mark_task"
```

---

### Task 4: Implement pause/resume in `run_once`

**Files:**
- Modify: `toolbox/engagement_loop.py:280-347`

- [ ] **Step 1: Rewrite `run_once` to resume `⏸️` tasks first and pause every `CHECKPOINT_EVERY` steps**

Replace the entire `run_once` function with:

```python
def run_once(dry_run: bool = False) -> str:
    text = _read_handoff()
    # Prefer resuming a paused checkpoint task.
    found = _find_first_task(text, status="⏸️")
    resuming = bool(found)
    checkpoint = _load_checkpoint() if resuming else None
    if not found:
        found = _find_first_task(text)
    if not found:
        _log("no_tasks")
        return "no_tasks"

    start, end, task = found
    owner = task["owner"]
    name = task["name"]

    # Only auto-run KIMI tasks for now; CLAUDE tasks are cloud-agent owned
    if owner != "KIMI":
        _log("skip_non_kimi", owner=owner, name=name)
        return f"skip_non_kimi:{owner}"

    steps = task.get("steps") or [task.get("goal", name)]
    start_index = 0
    if resuming and checkpoint and checkpoint.get("task_name") == name:
        start_index = min(checkpoint.get("completed_steps", 0), len(steps))
        _log("task_resumed", owner=owner, name=name, from_step=start_index + 1)

    _log("task_claimed" if not resuming else "task_resumed", owner=owner, name=name)
    if not dry_run:
        if resuming:
            claimed_block = task["block"].replace("### ⏸️", "### 🔄", 1)
        else:
            claimed_block = task["block"].replace("### ⏳", "### 🔄", 1)
        text = text[:start] + claimed_block + text[end:]
        _write_handoff(text)

    qa: list[tuple[str, str]] = []
    results: list[str] = []

    for turn, step in enumerate(steps[start_index:MAX_TURNS], start=start_index + 1):
        q = f"Step {turn}: {step}"
        _log("step_start", task=name, step=step, turn=turn)
        res = _execute_step(step, name)

        if res.get("blocked"):
            reason = res["reason"]
            qa.append((q, f"BLOCKED — {reason}"))
            _add_engagement_qa(name, qa)
            if not dry_run:
                text = _read_handoff()
                found2 = _find_first_task(text, status="🔄")
                if not found2:
                    found2 = _find_first_task(text, status="⏸️")
                if not found2:
                    found2 = _find_first_task(text, status="⏳")
                if found2 and found2[2]["name"] == name:
                    text = _mark_task(text, found2[0], found2[1], "blocked", reason)
                    _write_handoff(text)
                _clear_checkpoint()
            return f"blocked:{reason}"

        if res.get("success"):
            answer = res.get("stdout") or res.get("claf_response") or "done"
        else:
            error = res.get("error") or res.get("stderr") or "unknown error"
            answer = f"ERROR: {error}"

        qa.append((q, answer))
        results.append(f"Step {turn}: {answer[:200]}")
        _log("step_done", task=name, turn=turn, success=res.get("success", False))

        completed_count = turn
        if _checkpoint_needed(completed_count, CHECKPOINT_EVERY) and completed_count < len(steps):
            summary = f"{completed_count}/{len(steps)} steps completed; paused for human checkpoint"
            if not dry_run:
                text = _read_handoff()
                found2 = _find_first_task(text, status="🔄")
                if found2 and found2[2]["name"] == name:
                    text = _mark_task(text, found2[0], found2[1], "paused", summary)
                    _write_handoff(text)
                remaining = steps[turn:]
                _write_checkpoint(name, completed_count, len(steps), qa, remaining)
            return f"paused:{name}:{completed_count}/{len(steps)}"

    summary = " | ".join(results) or "completed"
    _add_engagement_qa(name, qa)

    if not dry_run:
        text = _read_handoff()
        found2 = _find_first_task(text, status="🔄")
        if not found2:
            found2 = _find_first_task(text, status="⏸️")
        if found2 and found2[2]["name"] == name:
            text = _mark_task(text, found2[0], found2[1], "done", summary)
            _write_handoff(text)
        _clear_checkpoint()

    _log("task_done", owner=owner, name=name, summary=summary[:500])
    return f"done:{name}"
```

- [ ] **Step 2: Run tests**

```bash
cd ~/projects/claf
pytest tests/test_engagement_loop.py -v
```

Expected: PASS.

- [ ] **Step 3: Dry-run the engagement loop against the current HANDOFF**

```bash
cd ~/projects/claf
python3 toolbox/engagement_loop.py --dry-run
```

Expected output: `no_tasks` (current HANDOFF has no `⏳ KIMI` tasks).

- [ ] **Step 4: Commit**

```bash
cd ~/projects/claf
git add toolbox/engagement_loop.py
git commit -m "feat(engagement_loop): pause and resume long tasks at checkpoints"
```

---

### Task 5: Document the checkpoint protocol in the charter

**Files:**
- Modify: `charter/charter_tasks.md`

- [ ] **Step 1: Append a checkpoint section at the end**

Append:

```markdown
LONG-RUNNING TASK CHECKPOINTS
When a queued task has more than `CLAF_CHECKPOINT_EVERY` steps (default 5), the engagement loop pauses after every Nth completed step, writes a machine-readable checkpoint to `~/.claf/engagement_checkpoint.json`, appends a human-readable summary to `~/MD/notepad.md`, and marks the HANDOFF task `⏸️`. The operator must re-run the engagement loop to continue. This prevents model drift on long chains and keeps Mary coherent.
```

- [ ] **Step 2: Commit**

```bash
cd ~/projects/claf
git add charter/charter_tasks.md
git commit -m "docs(charter): document long-task checkpoint protocol"
```

---

### Task 6: Final verification and push

- [ ] **Step 1: Run the full test suite**

```bash
cd ~/projects/claf
pytest tests/ -v
```

Expected: all tests PASS.

- [ ] **Step 2: Push commits**

```bash
cd ~/projects/claf
git push origin main
```

Expected: remote accepts all commits.

---

## Self-Review

- **Spec coverage:**
  - Pause every N steps → Task 4 implements `_checkpoint_needed` and pause logic.
  - Write checkpoint summary → Task 1 `_write_checkpoint` + `_build_checkpoint_text`.
  - Resume on next run → Task 4 resume logic in `run_once`.
  - Charter update → Task 5.
- **Placeholder scan:** All code blocks are complete; no TBD/TODO/filler steps.
- **Type consistency:** `qa: list[tuple[str, str]]` is used consistently. `_mark_task` accepts `paused` string status. `CHECKPOINT_EVERY` is an int.

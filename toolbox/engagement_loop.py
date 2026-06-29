#!/usr/bin/env python3
"""Engagement loop — autonomously execute queued tasks from HANDOFF.md.

The loop reads ~/MD/HANDOFF.md, claims the first ⏳ task, executes its listed
steps, and marks it ✅ or ⛔ BLOCKED. It logs an internal Q&A transcript and
posts engagement updates back to HANDOFF.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HANDOFF = Path.home() / "MD" / "HANDOFF.md"
ENGAGEMENT_LOG = Path.home() / ".claf" / "engagement.log"
CHECKPOINT_FILE = Path.home() / ".claf" / "engagement_checkpoint.json"
NOTEPAD = Path.home() / "MD" / "notepad.md"
CLAF_URL = os.environ.get("CLAF_URL", "http://localhost:8000/v1/messages")
MAX_TURNS = int(os.environ.get("CLAF_ENGAGEMENT_MAX_TURNS", "20"))
# Small models (e.g. qwen2.5-coder:3b) lose coherence after ~5 tool calls in one
# pass.  The checkpoint is a context reset: stop, record done/remaining, then
# resume from a fresh window on the next loop run.
CHECKPOINT_EVERY = int(os.environ.get("CLAF_CHECKPOINT_EVERY", "5"))

# Make claf_permissions importable from the toolbox/ subdirectory.
_CLAF_DIR = Path(__file__).resolve().parent.parent
if str(_CLAF_DIR) not in sys.path:
    sys.path.insert(0, str(_CLAF_DIR))
import claf_permissions

# Steps that should pause for operator confirmation before running.
_RISKY_PREFIXES = (
    "rm ",
    "sudo ",
    "mkfs",
    "fdisk",
    "dd ",
    "git push",
    "git reset",
    "git rebase",
    "deploy",
    "systemctl restart",
    "systemctl stop",
)


def _log(event: str, **kwargs) -> None:
    ENGAGEMENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **kwargs,
    }
    with ENGAGEMENT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _read_handoff() -> str:
    if not HANDOFF.exists():
        return ""
    return HANDOFF.read_text(encoding="utf-8")


def _write_handoff(text: str) -> None:
    HANDOFF.parent.mkdir(parents=True, exist_ok=True)
    HANDOFF.write_text(text, encoding="utf-8")


def _find_first_task(text: str, status: str | None = None) -> tuple[int, int, dict] | None:
    """Return (start, end, task_dict) for the first matching task block.

    status can be '⏳' (default), '🔄', '✅', or '⛔ BLOCKED' to locate a specific
    task state. Use '🔄' to re-locate a claimed task for final marking.
    """
    if status is None:
        status = "⏳"
    # Escape special regex chars in status (BLOCKED has a space)
    status_re = re.escape(status)
    pattern = re.compile(rf"^###\s+{status_re}\s+(KIMI|CLAUDE)\s*[-—]\s*(.+)$", re.MULTILINE)
    for match in pattern.finditer(text):
        start = match.start()
        # Find end: next ### at start of line or end of file
        next_heading = re.search(r"\n###\s+", text[start + 1 :])
        end = start + 1 + next_heading.start() if next_heading else len(text)
        block = text[start:end]
        task = {
            "owner": match.group(1).strip().upper(),
            "name": match.group(2).strip(),
            "block": block,
            "start": start,
            "end": end,
        }
        # Extract fields from the block
        for line in block.splitlines():
            if line.startswith("**File to create/edit:**"):
                task["file"] = line.split("**File to create/edit:**", 1)[1].strip()
            elif line.startswith("**Done when:**"):
                task["done_when"] = line.split("**Done when:**", 1)[1].strip()
            elif line.startswith("**Do NOT:**"):
                task["do_not"] = line.split("**Do NOT:**", 1)[1].strip()
        # Extract numbered/bulleted steps under "Do this exactly:"
        steps: list[str] = []
        in_steps = False
        for line in block.splitlines():
            if line.strip().startswith("**Do this exactly:**"):
                in_steps = True
                continue
            if in_steps:
                if not line.strip():
                    continue
                if line.startswith("**"):
                    break
                # Strip leading bullet/number markers
                step = re.sub(r"^\s*[-*\d]+\.\s*", "", line).strip()
                if step:
                    steps.append(step)
        task["steps"] = steps
        return start, end, task
    return None


def _is_risky(step: str) -> bool:
    low = step.lower()
    return any(low.startswith(p) for p in _RISKY_PREFIXES) or (
        low.startswith("write") or low.startswith("edit") or low.startswith("modify")
    )


def _looks_like_shell(step: str) -> bool:
    return bool(
        re.search(
            r"^(python3|bash|sh|ls|cat|grep|curl|cd\s|mkdir|cp|mv|systemctl|git\s(?!push|reset|rebase)|tail|head|find|which|ps|top|df|du|journalctl)",
            step.strip(),
        )
    )


def _extract_command(step: str) -> str:
    """Return the runnable part of a step like 'Run: python3 foo.py'."""
    # If step is wrapped in backticks, use that
    backtick = re.search(r"`([^`]+)`", step)
    if backtick:
        return backtick.group(1).strip()
    # Strip common prefixes
    for prefix in ("Run:", "run", "Execute:", "execute", "Step:", "Use"):
        if step.lower().startswith(prefix.lower()):
            step = step[len(prefix) :].strip()
            if step.startswith(":"):
                step = step[1:].strip()
    return step.strip()


def _run_shell(cmd: str, timeout: int = 120) -> dict:
    _log("shell_start", command=cmd)
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        _log(
            "shell_done",
            command=cmd,
            returncode=result.returncode,
            stdout=output[:500],
            stderr=err[:500],
        )
        return {
            "success": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": output,
            "stderr": err,
        }
    except subprocess.TimeoutExpired:
        _log("shell_timeout", command=cmd)
        return {"success": False, "error": f"timed out after {timeout}s"}
    except Exception as e:
        _log("shell_error", command=cmd, error=str(e))
        return {"success": False, "error": str(e)}


def _call_claf(prompt: str, model: str = "qwen2.5-coder:3b") -> str:
    """Call local CLAF and return the assistant text."""
    _log("claf_start", prompt=prompt[:200])
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        CLAF_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
        _log("claf_done", response=text[:500])
        return text
    except Exception as e:
        _log("claf_error", error=str(e))
        return f"[CLAF error: {e}]"


def _execute_step(step: str, task_name: str) -> dict:
    """Execute one step, respecting CLAF permission mode. Returns result dict."""
    cmd = _extract_command(step)

    # Permission mode gate: bash steps
    if _looks_like_shell(cmd):
        verdict = claf_permissions.is_action_allowed("bash", cmd)
        if verdict == "deny":
            return {
                "success": False,
                "blocked": True,
                "reason": f"Permission mode ({claf_permissions.MODE}) denies: {cmd}",
            }
        if verdict == "plan":
            return {
                "success": False,
                "blocked": True,
                "reason": f"Permission mode is plan; would run: {cmd}",
            }
        if verdict == "ask":
            return {
                "success": False,
                "blocked": True,
                "reason": f"Risky step requires operator approval: {cmd}",
            }
        if _is_risky(cmd):
            return {
                "success": False,
                "blocked": True,
                "reason": f"Risky step requires operator approval: {cmd}",
            }
        return _run_shell(cmd)

    # Permission mode gate: non-shell steps (app launches, browser, etc.)
    verdict = claf_permissions.is_action_allowed("task", step)
    if verdict == "deny":
        return {
            "success": False,
            "blocked": True,
            "reason": f"Permission mode ({claf_permissions.MODE}) denies step: {step}",
        }
    if verdict == "plan":
        return {
            "success": False,
            "blocked": True,
            "reason": f"Permission mode is plan; would do: {step}",
        }
    if verdict == "ask":
        return {
            "success": False,
            "blocked": True,
            "reason": f"Step requires operator approval: {step}",
        }

    # Otherwise ask CLAF to interpret the step
    prompt = (
        f"You are executing a queued task: '{task_name}'.\n"
        f"Current step: {step}\n"
        "Execute this step using the most appropriate tool. "
        "Return only the tool call or a concise result."
    )
    response = _call_claf(prompt)
    return {"success": True, "claf_response": response}


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
        new_heading = block.replace("### ⏳", "### ⛔ BLOCKED", 1).replace(
            "### 🔄", "### ⛔ BLOCKED", 1
        )
        summary = f"\n**Engagement result (BLOCKED {today}):** {result}\n"
    new_block = new_heading.rstrip() + summary
    return text[:start] + new_block + text[end:]


def _add_engagement_qa(task_name: str, qa: list[tuple[str, str]]) -> None:
    for q, a in qa:
        _log("engagement_qa", task=task_name, question=q, answer=str(a)[:500])


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
    """Return a model-facing context-reset block for notepad.md."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"## Mary Context Reset — {task_name} — {now}",
        "",
        f"Progress: {completed}/{total} steps completed. Stopping here to reset context before continuing.",
        "",
        "### Done so far",
    ]
    for q, a in qa:
        lines.append(f"- {q}: {str(a)[:120]}")
    if not qa:
        lines.append("- (no steps completed yet)")
    lines.extend(["", "### Remaining"])
    for step in remaining_steps:
        lines.append(f"- {step}")
    if not remaining_steps:
        lines.append("- (no remaining steps)")
    lines.extend(
        [
            "",
            "Continue? Re-run `python3 ~/projects/claf/toolbox/engagement_loop.py` to resume from the next step with a fresh context window.",
            "",
        ]
    )
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Engagement loop for HANDOFF.md")
    parser.add_argument(
        "--dry-run", action="store_true", help="Plan only; do not modify HANDOFF.md"
    )
    args = parser.parse_args()
    result = run_once(dry_run=args.dry_run)
    print(result)
    return 0 if result.startswith("done:") or result == "no_tasks" else 1


if __name__ == "__main__":
    sys.exit(main())

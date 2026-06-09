#!/usr/bin/env python3
"""CLAF Agent Runner — Standalone autonomous agent loop.

Runs continuously, seeds tasks from AI_CONTEXT snapshots, executes them
via CLAF + tool use. Uses the same SQLite DB as secretary_agent so tasks
are visible to both.

Usage:
    python3 agent_runner.py           # foreground
    python3 agent_runner.py --daemon  # background (no stdio)

Environment:
    CLAF_URL            http://localhost:8000
    AGENT_SCAN_INTERVAL 30 (seconds between queue checks)
    AGENT_MAX_ACTIVE    2  (max concurrent tasks)
    AGENT_CONTEXT_DIR   ~/Desktop/AI_CONTEXT
    AGENT_AUTO_SEED     1  (auto-create tasks from context)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ─── Config ───────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
DB_PATH = _HERE / "secretary.db"
CLAF_URL = os.environ.get("CLAF_URL", "http://localhost:8000")
SCAN_INTERVAL = int(os.environ.get("AGENT_SCAN_INTERVAL", "30"))
MAX_ACTIVE = int(os.environ.get("AGENT_MAX_ACTIVE", "2"))
CONTEXT_DIR = Path(os.environ.get("AGENT_CONTEXT_DIR", str(Path.home() / "Desktop" / "AI_CONTEXT")))
AUTO_SEED = os.environ.get("AGENT_AUTO_SEED", "1") == "1"
LOG_PATH = _HERE / "agent_runner.log"

# ─── Logging ──────────────────────────────────────────────────────────────────
_log_lock = threading.Lock()


def _log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{ts}] [agent] {msg}\n"
    with _log_lock:
        sys.stderr.write(line)
        sys.stderr.flush()
        try:
            with open(LOG_PATH, "a") as f:
                f.write(line)
        except Exception:
            pass


# ─── DB (same schema as secretary) ────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_tables():
    conn = _db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            goal TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            session TEXT,
            created_ts TEXT NOT NULL,
            updated_ts TEXT NOT NULL,
            profile TEXT NOT NULL DEFAULT 'full'
        );
        CREATE TABLE IF NOT EXISTS task_steps (
            step_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            turn_num INTEGER NOT NULL,
            directive TEXT,
            result TEXT,
            ts TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS task_events (
            event_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT,
            ts TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS incidents (
            incident_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            step_id TEXT,
            error TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            ts TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS leases (
            task_id TEXT PRIMARY KEY,
            worker_id TEXT NOT NULL,
            expires_ts TEXT NOT NULL
        );
    """
    )
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN profile TEXT NOT NULL DEFAULT 'full'")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return str(uuid.uuid4())


def _task_create(goal: str, session: str = "agent", profile: str = "full") -> dict:
    conn = _db()
    task_id = _uid()
    ts = _now()
    conn.execute(
        "INSERT INTO tasks (task_id, goal, status, session, created_ts, updated_ts, profile) "
        "VALUES (?,?,?,?,?,?,?)",
        (task_id, goal, "queued", session, ts, ts, profile),
    )
    conn.execute(
        "INSERT INTO task_events VALUES (?,?,?,?,?)",
        (_uid(), task_id, "created", json.dumps({"goal": goal, "profile": profile}), ts),
    )
    conn.commit()
    conn.close()
    return {"task_id": task_id, "status": "queued"}


def _task_claim_next(worker_id: str, ttl: int = 300) -> dict | None:
    """Atomically claim the oldest queued task. Returns task dict or None."""
    conn = _db()
    now_ts = datetime.utcnow().timestamp()
    expires = now_ts + ttl
    try:
        # Find oldest queued task not currently leased
        row = conn.execute(
            "SELECT t.* FROM tasks t "
            "LEFT JOIN leases l ON t.task_id = l.task_id "
            "WHERE t.status = 'queued' "
            "AND (l.task_id IS NULL OR l.expires_ts < ?) "
            "ORDER BY t.created_ts ASC LIMIT 1",
            (str(now_ts),),
        ).fetchone()
        if not row:
            conn.close()
            return None

        task_id = row["task_id"]
        # Acquire lease
        conn.execute(
            "INSERT INTO leases VALUES (?,?,?) ON CONFLICT(task_id) DO UPDATE "
            "SET worker_id=excluded.worker_id, expires_ts=excluded.expires_ts",
            (task_id, worker_id, str(expires)),
        )
        conn.execute(
            "UPDATE tasks SET status=?, updated_ts=? WHERE task_id=?",
            ("executing", _now(), task_id),
        )
        conn.commit()
        conn.close()
        return dict(row)
    except Exception as e:
        _log(f"claim error: {e}")
        conn.close()
        return None


def _task_set_status(task_id: str, status: str):
    conn = _db()
    conn.execute(
        "UPDATE tasks SET status=?, updated_ts=? WHERE task_id=?",
        (status, _now(), task_id),
    )
    conn.commit()
    conn.close()


def _task_list(status_filter: str | None = None) -> list[dict]:
    conn = _db()
    if status_filter:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status=? ORDER BY created_ts DESC LIMIT 50",
            (status_filter,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM tasks ORDER BY created_ts DESC LIMIT 50").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _step_record(task_id: str, turn_num: int, directive: str, result: str):
    conn = _db()
    conn.execute(
        "INSERT INTO task_steps VALUES (?,?,?,?,?,?)",
        (_uid(), task_id, turn_num, directive[:2000], str(result)[:2000], _now()),
    )
    conn.commit()
    conn.close()


def _incident_record(task_id: str, error: str):
    conn = _db()
    conn.execute(
        "INSERT INTO incidents VALUES (?,?,?,?,?,?)",
        (_uid(), task_id, None, str(error)[:1000], 0, _now()),
    )
    conn.commit()
    conn.close()


def _lease_release(task_id: str):
    conn = _db()
    conn.execute("DELETE FROM leases WHERE task_id=?", (task_id,))
    conn.commit()
    conn.close()


# ─── CLAF helpers ─────────────────────────────────────────────────────────────

def _claf_call(messages: list, tools: list | None = None, system: str = "", max_tokens: int = 1024) -> dict:
    """Call CLAF /v1/messages. Returns Anthropic-format response dict."""
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = tools

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{CLAF_URL}/v1/messages",
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"error": str(e)}


# ─── Tool execution ───────────────────────────────────────────────────────────

_AGENT_TOOLS = [
    {"name": "bash", "description": "Run a shell command on this machine.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read a local file (first 4KB).",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write or create a local file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "done", "description": "Signal task completion with a result summary.",
     "input_schema": {"type": "object", "properties": {"result": {"type": "string"}}, "required": ["result"]}},
]

DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+/",
    r"mkfs\.",
    r"dd\s+if=.*of=/dev",
    r">\s*/etc/",
    r"curl.*\|\s*sh",
    r"wget.*\|\s*sh",
    r"\bsudo\b",
    r"chmod\s+777",
]


def _is_dangerous(cmd: str) -> bool:
    low = cmd.lower()
    for pat in DANGEROUS_PATTERNS:
        if re.search(pat, low):
            return True
    return False


def _exec_tool(name: str, args: dict) -> str:
    try:
        if name == "bash":
            cmd = str(args.get("command", "")).strip()
            if not cmd:
                return "bash: command required"
            if _is_dangerous(cmd):
                return "bash: blocked dangerous command"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            out = (result.stdout or "").strip()
            err = (result.stderr or "").strip()
            rep = out if out else err if err else f"(exit {result.returncode})"
            return rep[:1000] + (" ...[truncated]" if len(rep) > 1000 else "")

        if name == "read_file":
            path = os.path.expanduser(str(args.get("path", "")).strip())
            with open(path, "r") as f:
                content = f.read(4000)
            return content

        if name == "write_file":
            path = os.path.expanduser(str(args.get("path", "")).strip())
            content = str(args.get("content", ""))
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            return f"wrote {len(content)} chars to {path}"

        if name == "done":
            return "__DONE__:" + str(args.get("result", "Task completed."))

        return f"unknown tool: {name}"
    except subprocess.TimeoutExpired:
        return f"{name}: timed out after 30s"
    except FileNotFoundError as e:
        return f"{name}: file not found — {e}"
    except Exception as e:
        return f"{name} error: {e}"


# ─── Context seeding ──────────────────────────────────────────────────────────

def _read_latest_context() -> str:
    files = sorted(CONTEXT_DIR.glob("context_*.txt"))
    if not files:
        return ""
    try:
        return files[-1].read_text()
    except Exception:
        return ""


def _seed_tasks_from_context():
    """If context contains [ACTIVE TASK], create a task for it.
    Also look for obvious todo markers."""
    context = _read_latest_context()
    if not context:
        return

    # Check for [ACTIVE TASK] section
    active_match = re.search(r"\[ACTIVE TASK\]\s*(.*?)(?=\n\[|\Z)", context, re.DOTALL)
    if active_match:
        task_text = active_match.group(1).strip()
        # Skip if it says "none recorded"
        if task_text and "none recorded" not in task_text.lower():
            # Check if this exact goal already exists as queued/executing
            existing = _task_list()
            for t in existing:
                if t["goal"] == task_text and t["status"] in ("queued", "executing"):
                    return  # already tracked
            _log(f"seeding task from context: {task_text[:80]}")
            _task_create(task_text)

    # Also look for TODO / FIXME lines
    todos = re.findall(r"(?im)^\s*(?:TODO|FIXME|HACK|BUG)\s*[:\-]?\s*(.+)$", context)
    for todo in todos:
        goal = todo.strip()
        if len(goal) < 10:
            continue
        existing = _task_list()
        for t in existing:
            if t["goal"] == goal and t["status"] in ("queued", "executing"):
                break
        else:
            _log(f"seeding todo: {goal[:80]}")
            _task_create(goal)


# ─── Agent execution loop ─────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are an autonomous agent. Execute the goal step by step using the tools available.
Rules:
- Analyze the goal, plan steps, then execute.
- Use bash to explore the system, read files to gather info, write files to create output.
- When a step fails, try an alternative approach.
- When the goal is fully complete, call done() with a concise summary.
- Never call done() before attempting the goal.
- Do not repeat the same action more than 3 times.
- Be concise. Do not narrate your reasoning unless asked.
"""

MAX_TURNS = 20


def _execute_task(task: dict):
    task_id = task["task_id"]
    goal = task["goal"]
    worker_id = _uid()
    _log(f"task start {task_id[:8]}: {goal[:80]}")

    messages = [{"role": "user", "content": goal}]
    turn = 0
    seen_directives: dict[str, int] = {}

    try:
        while turn < MAX_TURNS:
            resp = _claf_call(messages, tools=_AGENT_TOOLS, system=_SYSTEM_PROMPT, max_tokens=1024)

            if "error" in resp:
                _incident_record(task_id, f"claf_error: {resp['error']}")
                _task_set_status(task_id, "failed")
                break

            content = resp.get("content", [])
            stop_reason = resp.get("stop_reason", "end_turn")

            # Append assistant message
            messages.append({"role": "assistant", "content": content})

            # Extract text and check for natural completion
            text_parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            full_text = " ".join(text_parts)

            if stop_reason == "end_turn":
                _step_record(task_id, turn, "end_turn", full_text[:500])
                _task_set_status(task_id, "completed")
                _log(f"task complete {task_id[:8]}: {full_text[:100]}")
                break

            if stop_reason != "tool_use":
                _step_record(task_id, turn, f"stop_{stop_reason}", full_text[:500])
                _task_set_status(task_id, "completed")
                break

            # Execute tool calls
            tool_results = []
            completed = False

            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name", "")
                args = block.get("input") or {}
                use_id = block.get("id", _uid())

                sig = f"{name}:{json.dumps(args, sort_keys=True)}"
                seen_directives[sig] = seen_directives.get(sig, 0) + 1
                if seen_directives[sig] > 3:
                    _incident_record(task_id, f"loop_detected:{sig[:100]}")
                    _task_set_status(task_id, "failed")
                    completed = True
                    break

                result_text = _exec_tool(name, args)
                _step_record(task_id, turn, sig, result_text)
                _log(f"  turn {turn} {name}: {result_text[:120]}")

                if result_text.startswith("__DONE__:"):
                    _task_set_status(task_id, "completed")
                    completed = True
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": use_id,
                        "content": result_text[9:],
                    })
                    break

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": use_id,
                    "content": result_text,
                })

            if completed:
                break

            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            turn += 1
        else:
            _incident_record(task_id, f"max_turns_exceeded:{MAX_TURNS}")
            _task_set_status(task_id, "failed")
            _log(f"task failed {task_id[:8]}: max turns exceeded")

    except Exception as e:
        _incident_record(task_id, traceback.format_exc())
        _task_set_status(task_id, "failed")
        _log(f"task crashed {task_id[:8]}: {e}")
    finally:
        _lease_release(task_id)


# ─── Main loop ────────────────────────────────────────────────────────────────

_shutdown = threading.Event()
_active_workers = 0
_active_lock = threading.Lock()


def _worker_loop():
    global _active_workers
    worker_id = _uid()
    while not _shutdown.is_set():
        with _active_lock:
            if _active_workers >= MAX_ACTIVE:
                time.sleep(1)
                continue
            _active_workers += 1

        task = _task_claim_next(worker_id)
        if task:
            try:
                _execute_task(task)
            except Exception as e:
                _log(f"execute_task crash: {e}")
        else:
            # No task — maybe seed from context, then sleep
            if AUTO_SEED:
                try:
                    _seed_tasks_from_context()
                except Exception as e:
                    _log(f"seed error: {e}")
            time.sleep(SCAN_INTERVAL)

        with _active_lock:
            _active_workers -= 1


# ─── HTTP status server ───────────────────────────────────────────────────────

def _http_server():
    try:
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def _send_json(self, obj, code=200):
                body = json.dumps(obj, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/agent/health":
                    tasks = _task_list()
                    queued = len([t for t in tasks if t["status"] == "queued"])
                    executing = len([t for t in tasks if t["status"] == "executing"])
                    self._send_json({
                        "ok": True,
                        "claf": CLAF_URL,
                        "queued": queued,
                        "executing": executing,
                        "active_workers": _active_workers,
                    })
                elif self.path == "/agent/tasks":
                    self._send_json({"tasks": _task_list()})
                else:
                    self._send_json({"error": "not found"}, code=404)

            def do_POST(self):
                if self.path == "/agent/tasks":
                    content_len = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(content_len).decode("utf-8")
                    try:
                        data = json.loads(body)
                    except Exception:
                        self._send_json({"error": "bad json"}, code=400)
                        return
                    goal = str(data.get("goal", "")).strip()
                    if not goal:
                        self._send_json({"error": "goal required"}, code=400)
                        return
                    t = _task_create(goal, data.get("session", "agent"), data.get("profile", "full"))
                    self._send_json({"ok": True, "task": t}, code=201)
                else:
                    self._send_json({"error": "not found"}, code=404)

        server = HTTPServer(("127.0.0.1", 8002), Handler)
        _log("HTTP server on :8002")
        server.serve_forever()
    except Exception as e:
        _log(f"http server error: {e}")


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="CLAF Agent Runner")
    parser.add_argument("--daemon", action="store_true", help="Run in background mode")
    args = parser.parse_args()

    if args.daemon:
        # Redirect stdout/stderr to log file
        sys.stdout = open(LOG_PATH, "a")
        sys.stderr = sys.stdout

    _ensure_tables()
    _log(f"agent runner started. claf={CLAF_URL} interval={SCAN_INTERVAL}s max_active={MAX_ACTIVE}")

    # Start HTTP server
    t_http = threading.Thread(target=_http_server, daemon=True)
    t_http.start()

    # Start worker threads
    threads = []
    for i in range(MAX_ACTIVE):
        t = threading.Thread(target=_worker_loop, daemon=True)
        t.start()
        threads.append(t)

    _log(f"{len(threads)} worker threads started")

    # Keep main thread alive
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        _log("shutdown signal received")
        _shutdown.set()
        for t in threads:
            t.join(timeout=5)
        _log("shutdown complete")


if __name__ == "__main__":
    import traceback
    main()

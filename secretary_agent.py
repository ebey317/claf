#!/usr/bin/env python3
"""
secretary_agent.py — Fully Autonomous Secretary MCP Server (secretary.autonomous.v1)

MCP stdio server (JSON-RPC 2.0) with 8 task-management tools.
HTTP server on :8001 for /agent/health and /agent/stats.
Autonomous task loop uses CLAF as LLM gateway + Sensei bridge for actions.
SQLite state at ~/projects/claf/secretary.db.
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ─── Config ───────────────────────────────────────────────────────────────────

_HERE = Path(__file__).parent
DB_PATH = os.environ.get("SECRETARY_DB", str(_HERE / "secretary.db"))
CLAF_URL = os.environ.get("CLAF_URL", "http://localhost:8000")
BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://localhost:8080")
HTTP_PORT = int(os.environ.get("SECRETARY_HTTP_PORT", "8001"))
MAX_TURNS = int(os.environ.get("SECRETARY_MAX_TURNS", "20"))
MAX_RETRY_STEPS = int(os.environ.get("SECRETARY_MAX_RETRY", "3"))
RETRY_DELAYS = [1, 2, 4, 8, 8]
BRIDGE_WAIT = 60
BRIDGE_SESSION = os.environ.get("BRIDGE_SESSION", "mcp-default")
LOG_PATH = os.environ.get("SECRETARY_LOG", str(_HERE / "secretary.log"))

# Mirror stderr to log file so debug is visible outside the MCP subprocess pipe.
try:
    _log_file = open(LOG_PATH, "a", buffering=1)
    _orig_stderr = sys.stderr

    class _Tee:
        def write(self, s):
            _orig_stderr.write(s)
            _log_file.write(s)

        def flush(self):
            _orig_stderr.flush()
            _log_file.flush()

        def __getattr__(self, name):
            return getattr(_orig_stderr, name)

    sys.stderr = _Tee()
except Exception:
    pass

# ─── DB init ──────────────────────────────────────────────────────────────────


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _db()
    conn.executescript("""
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
        CREATE TABLE IF NOT EXISTS approvals (
            approval_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            step_id TEXT,
            action TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
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
        CREATE TABLE IF NOT EXISTS memories (
            memory_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT,
            ts TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS leases (
            task_id TEXT PRIMARY KEY,
            worker_id TEXT NOT NULL,
            expires_ts TEXT NOT NULL
        );
    """)
    # Additive migration for existing DBs that predate the profile column
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN profile TEXT NOT NULL DEFAULT 'full'")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return str(uuid.uuid4())


# ─── DB helpers ───────────────────────────────────────────────────────────────


def _task_create(goal: str, session: str, profile: str = "full") -> dict:
    conn = _db()
    task_id = _uid()
    ts = _now()
    conn.execute(
        "INSERT INTO tasks (task_id, goal, status, session, created_ts, updated_ts, profile) "
        "VALUES (?,?,?,?,?,?,?)",
        (task_id, goal, "queued", session or BRIDGE_SESSION, ts, ts, profile),
    )
    _event(conn, task_id, "created", {"goal": goal, "profile": profile})
    conn.commit()
    conn.close()
    return {"task_id": task_id, "status": "queued", "profile": profile}


def _task_get(task_id: str) -> dict:
    conn = _db()
    row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        conn.close()
        return {}
    steps = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM task_steps WHERE task_id=? ORDER BY turn_num", (task_id,)
        )
    ]
    conn.close()
    d = dict(row)
    d["steps"] = steps
    return d


def _task_set_status(task_id: str, status: str):
    conn = _db()
    conn.execute(
        "UPDATE tasks SET status=?, updated_ts=? WHERE task_id=?",
        (status, _now(), task_id),
    )
    _event(conn, task_id, "status_change", {"status": status})
    conn.commit()
    conn.close()


def _task_list(status_filter: str = None) -> list:
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


def _step_record(task_id: str, turn_num: int, directive: str, result: str) -> str:
    conn = _db()
    step_id = _uid()
    conn.execute(
        "INSERT INTO task_steps VALUES (?,?,?,?,?,?)",
        (step_id, task_id, turn_num, directive[:2000], str(result)[:2000], _now()),
    )
    conn.commit()
    conn.close()
    return step_id


def _incident_record(task_id: str, step_id: str, error: str, retry_count: int):
    conn = _db()
    conn.execute(
        "INSERT INTO incidents VALUES (?,?,?,?,?,?)",
        (_uid(), task_id, step_id, str(error)[:1000], retry_count, _now()),
    )
    conn.commit()
    conn.close()


def _incidents_get(task_id: str) -> list:
    conn = _db()
    rows = conn.execute(
        "SELECT * FROM incidents WHERE task_id=? ORDER BY ts DESC", (task_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _event(conn: sqlite3.Connection, task_id: str, event_type: str, payload: dict):
    conn.execute(
        "INSERT INTO task_events VALUES (?,?,?,?,?)",
        (_uid(), task_id, event_type, json.dumps(payload), _now()),
    )


def _lease_acquire(task_id: str, worker_id: str, ttl: int = 120) -> bool:
    """Returns True if lease acquired."""
    conn = _db()
    expires = datetime.utcnow().timestamp() + ttl
    try:
        conn.execute(
            "INSERT INTO leases VALUES (?,?,?) ON CONFLICT(task_id) DO UPDATE "
            "SET worker_id=excluded.worker_id, expires_ts=excluded.expires_ts "
            "WHERE expires_ts < ?",
            (task_id, worker_id, str(expires), str(datetime.utcnow().timestamp())),
        )
        conn.commit()
        # Verify we own it
        row = conn.execute("SELECT worker_id FROM leases WHERE task_id=?", (task_id,)).fetchone()
        ok = row and row["worker_id"] == worker_id
        conn.close()
        return ok
    except Exception:
        conn.close()
        return False


def _lease_release(task_id: str):
    conn = _db()
    conn.execute("DELETE FROM leases WHERE task_id=?", (task_id,))
    conn.commit()
    conn.close()


# ─── Bridge helpers (copied from sensei_mcp_server.py) ───────────────────────


def _http(method: str, url: str, body=None, timeout=5.0) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return {"ok": True, "status": resp.status, "json": json.loads(raw)}
            except Exception:
                return {"ok": True, "status": resp.status, "text": raw}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "error": str(e)}
    except Exception as e:
        return {"ok": False, "status": 0, "error": str(e)}


def _bridge_alive() -> bool:
    r = _http("GET", f"{BRIDGE_URL}/extension/queue_state", timeout=1.5)
    return bool(r.get("ok"))


def _claf_alive() -> bool:
    r = _http("GET", f"{CLAF_URL}/healthz", timeout=2.0)
    return bool(r.get("ok"))


def _log(msg: str):
    sys.stderr.write(f"[secretary] {msg}\n")
    sys.stderr.flush()


def _push_action(kind: str, payload: dict, session: str) -> dict:
    _log(f"bridge push {kind} session={session} payload={json.dumps(payload)[:120]}")
    body = {"session_id": session, "actions": [{"kind": kind, **payload}]}
    return _http("POST", f"{BRIDGE_URL}/extension/queue", body=body, timeout=3.0)


def _await_result(action_id: str, session: str, wait: int = BRIDGE_WAIT) -> dict:
    if not action_id:
        return {"ok": False, "reason": "no_action_id"}
    _log(f"await result action_id={action_id} timeout={wait}s")
    deadline = time.time() + wait
    while time.time() < deadline:
        r = _http(
            "GET",
            f"{BRIDGE_URL}/extension/result?session_id={session}&action_id={action_id}",
            timeout=2.0,
        )
        if r.get("ok") and r.get("json"):
            j = r["json"]
            # Only return when bridge confirms the action is done (ok:true + result present).
            # Pending responses also include action_id so we can't use that as the signal.
            if j.get("ok") and j.get("result") is not None:
                _log(
                    f"result received action_id={action_id} verdict={j.get('result',{}).get('verdict','?')}"
                )
                return j
        time.sleep(0.4)
    _log(f"await timeout action_id={action_id}")
    return {"ok": False, "reason": "timeout"}


def _action_id_from(push_resp: dict):
    if not push_resp or not push_resp.get("ok"):
        return None
    j = push_resp.get("json") or {}
    aid = j.get("action_id")
    if isinstance(aid, str) and aid:
        return aid
    aids = j.get("action_ids")
    if isinstance(aids, list) and aids:
        return aids[0]
    return None


def _dispatch_browser(kind: str, payload: dict, session: str) -> dict:
    if not _bridge_alive():
        return {"ok": False, "reason": "bridge_unreachable", "hint": "Open Chrome side panel."}
    push = _push_action(kind, payload, session)
    if not push.get("ok"):
        return {"ok": False, "reason": "push_failed"}
    aid = _action_id_from(push)
    result = _await_result(aid, session)
    return {"ok": bool(result.get("ok", True)), "result": result, "action_id": aid}


# ─── Tool execution (called inside the agent loop) ───────────────────────────


def _exec_tool(name: str, args: dict, session: str) -> str:
    """Execute a tool call from the agent loop. Returns text result."""
    try:
        if name == "browser_navigate":
            url = str(args.get("url", "")).strip()
            if not url.startswith("http"):
                url = "https://" + url
            r = _dispatch_browser("BROWSER_NAV", {"target": url}, session)
            return f"navigate {url} → {json.dumps(r)[:400]}"

        if name == "browser_click":
            what = str(args.get("selector", "")).strip()
            r = _dispatch_browser("BROWSER_CLICK", {"target": what}, session)
            return f"click '{what}' → {json.dumps(r)[:300]}"

        if name == "browser_fill":
            where = str(args.get("selector", "")).strip()
            text = str(args.get("value", ""))
            r = _dispatch_browser("BROWSER_FILL", {"target": where, "value": text}, session)
            return f"fill '{where}' = {text[:60]} → {json.dumps(r)[:300]}"

        if name == "browser_read":
            r = _dispatch_browser("BROWSER_READ_PAGE", {}, session)
            rep = json.dumps(r)
            return rep[:2500] + (" ...[truncated]" if len(rep) > 2500 else "")

        if name == "web_search":
            query = str(args.get("query", "")).strip()
            url = "https://www.google.com/search?q=" + query.replace(" ", "+")
            r = _dispatch_browser("BROWSER_NAV", {"target": url}, session)
            return f"search '{query}' → {json.dumps(r)[:300]}"

        if name == "bash":
            cmd = str(args.get("command", "")).strip()
            if not cmd:
                return "bash: command required"
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


# ─── Agent loop tools (sent to LLM as tool schemas) ──────────────────────────

_AGENT_TOOLS = [
    {
        "name": "browser_navigate",
        "description": "Navigate browser to a URL.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "browser_click",
        "description": "Click an element by CSS selector or visible text.",
        "input_schema": {
            "type": "object",
            "properties": {"selector": {"type": "string"}},
            "required": ["selector"],
        },
    },
    {
        "name": "browser_fill",
        "description": "Type text into a form field.",
        "input_schema": {
            "type": "object",
            "properties": {"selector": {"type": "string"}, "value": {"type": "string"}},
            "required": ["selector", "value"],
        },
    },
    {
        "name": "browser_read",
        "description": "Read visible content of the current page.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "web_search",
        "description": "Google search query, opens results in browser.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "bash",
        "description": "Run a shell command on this machine.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a local file (first 4KB).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write or create a local file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "done",
        "description": "Signal task completion with a result summary.",
        "input_schema": {
            "type": "object",
            "properties": {"result": {"type": "string"}},
            "required": ["result"],
        },
    },
]

# ─── Profile-based tool loading ──────────────────────────────────────────────

_PROFILES_PATH = _HERE / "config" / "tools.json"


def _load_profiles() -> dict:
    try:
        with open(_PROFILES_PATH, "r") as f:
            return json.load(f)
    except Exception:
        all_names = [t["name"] for t in _AGENT_TOOLS]
        return {
            "profiles": {"full": {"tools": all_names}},
            "default": "full",
            "auto_classify": [],
        }


_PROFILES = _load_profiles()


def _classify_profile(goal: str) -> str:
    g = (goal or "").lower()
    for rule in _PROFILES.get("auto_classify", []):
        for kw in rule.get("keywords", []):
            if kw in g:
                return rule["profile"]
    return _PROFILES.get("default", "full")


def _tools_for_profile(profile: str) -> list:
    profiles = _PROFILES.get("profiles", {})
    names = profiles.get(profile, profiles.get("full", {})).get("tools")
    if not names:
        return _AGENT_TOOLS
    return [t for t in _AGENT_TOOLS if t["name"] in names]


_SYSTEM_PROMPT = """\
You are an autonomous secretary agent. Execute the goal step by step using the tools available.
Rules:
- Never invent values or make assumptions about credentials, passwords, or private data.
- If you need information you do not have, use bash or browser_read to discover it.
- When a step fails, try an alternative approach before giving up.
- When the goal is fully complete, call done() with a concise summary.
- Never call done() before attempting the goal.
- Do not repeat the same action more than 3 times.
"""


def _loop_hash(step_history: list, last_n: int = 3) -> str:
    recent = step_history[-last_n:]
    return hashlib.md5(json.dumps([s.get("directive") for s in recent]).encode()).hexdigest()


# ─── Secretary autonomous loop ────────────────────────────────────────────────

_active_tasks: dict = {}  # task_id → threading.Event (for abort)
_active_lock = threading.Lock()


def _run_loop(task_id: str, goal: str, session: str, worker_id: str, profile: str = "full"):
    """Runs in a daemon thread. Drives the CLAF tool-use loop until done/failed."""
    if not _lease_acquire(task_id, worker_id):
        return  # another worker has it

    tools_for_turn = _tools_for_profile(profile)
    _task_set_status(task_id, "planning")
    stop_event = threading.Event()
    with _active_lock:
        _active_tasks[task_id] = stop_event

    messages = []
    turn = 0
    seen_hashes = {}
    step_history = []

    try:
        _log(f"task start task_id={task_id} profile={profile} goal={goal[:80]}")
        _task_set_status(task_id, "executing")

        while turn < MAX_TURNS:
            if stop_event.is_set():
                _task_set_status(task_id, "cancelled")
                break

            # Build message list: initial goal + accumulated turns
            if not messages:
                messages = [{"role": "user", "content": goal}]

            # Call CLAF
            _log(f"claf call turn={turn} messages={len(messages)}")
            payload = {
                "model": "claude-sonnet-4-6",
                "max_tokens": 1024,
                "system": _SYSTEM_PROMPT,
                "tools": tools_for_turn,
                "messages": messages,
            }
            resp = _http("POST", f"{CLAF_URL}/v1/messages", body=payload, timeout=60.0)

            if not resp.get("ok"):
                err = resp.get("error", "claf_unreachable")
                _incident_record(task_id, None, err, turn)
                # Retry up to MAX_RETRY_STEPS
                if turn >= MAX_RETRY_STEPS:
                    _task_set_status(task_id, "failed")
                    break
                delay = RETRY_DELAYS[min(turn, len(RETRY_DELAYS) - 1)]
                time.sleep(delay)
                turn += 1
                continue

            body = resp.get("json") or {}
            stop_reason = body.get("stop_reason", "end_turn")
            content = body.get("content", [])

            # Append assistant message
            messages.append({"role": "assistant", "content": content})

            # end_turn — check for done() or natural completion
            if stop_reason == "end_turn":
                # Extract text to see if goal was stated done
                text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]
                full_text = " ".join(text_parts)
                _step_record(task_id, turn, "end_turn", full_text[:500])
                _task_set_status(task_id, "completed")
                break

            if stop_reason != "tool_use":
                _task_set_status(task_id, "completed")
                break

            # Execute tool calls
            tool_results = []
            completed = False

            for block in content:
                if block.get("type") != "tool_use":
                    continue
                name = block.get("name", "")
                args = block.get("input") or {}
                use_id = block.get("id", _uid())

                # Loop detection
                directive_sig = f"{name}:{json.dumps(args, sort_keys=True)}"
                seen_hashes[directive_sig] = seen_hashes.get(directive_sig, 0) + 1
                if seen_hashes[directive_sig] > 3:
                    _task_set_status(task_id, "failed")
                    _incident_record(task_id, None, f"loop_detected:{directive_sig[:100]}", turn)
                    completed = True
                    break

                result_text = _exec_tool(name, args, session)
                _step_record(task_id, turn, directive_sig, result_text)

                if result_text.startswith("__DONE__:"):
                    _task_set_status(task_id, "completed")
                    completed = True
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": use_id,
                            "content": result_text[9:],
                        }
                    )
                    break

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": use_id,
                        "content": result_text,
                    }
                )

                step_history.append({"directive": directive_sig, "result": result_text[:100]})

            if completed:
                break

            # Append tool results as user turn
            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            turn += 1

        else:
            # Exhausted MAX_TURNS
            _task_set_status(task_id, "failed")
            _incident_record(task_id, None, f"max_turns_exceeded:{MAX_TURNS}", turn)

    except Exception:
        _incident_record(task_id, None, traceback.format_exc(), turn)
        _task_set_status(task_id, "failed")
    finally:
        with _active_lock:
            _active_tasks.pop(task_id, None)
        _lease_release(task_id)


def _start_task(task_id: str, goal: str, session: str, profile: str = "full"):
    worker_id = _uid()
    t = threading.Thread(
        target=_run_loop,
        args=(task_id, goal, session, worker_id, profile),
        daemon=True,
    )
    t.start()


# ─── Uniform result envelope ──────────────────────────────────────────────────


def _ok(result=None, task_id=None, **kwargs) -> dict:
    return {"ok": True, "result": result, "task_id": task_id, **kwargs}


def _fail(reason: str, task_id=None, **kwargs) -> dict:
    return {"ok": False, "reason": reason, "task_id": task_id, **kwargs}


# ─── MCP tool handlers ────────────────────────────────────────────────────────


def _mcp_intake_task(args: dict) -> dict:
    goal = str(args.get("goal") or "").strip()
    session = str(args.get("session") or BRIDGE_SESSION)
    if not goal:
        return _fail("goal is required")
    profile = str(args.get("profile") or "").strip()
    if not profile:
        profile = _classify_profile(goal)
    if profile not in _PROFILES.get("profiles", {}):
        profile = _PROFILES.get("default", "full")
    t = _task_create(goal, session, profile)
    return _ok(
        result={"status": "queued", "task_id": t["task_id"], "profile": profile},
        task_id=t["task_id"],
    )


def _mcp_get_task(args: dict) -> dict:
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return _fail("task_id is required")
    task = _task_get(task_id)
    if not task:
        return _fail("task not found", task_id=task_id)
    return _ok(result=task, task_id=task_id)


def _mcp_run_task(args: dict) -> dict:
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return _fail("task_id is required")
    task = _task_get(task_id)
    if not task:
        return _fail("task not found", task_id=task_id)
    status = task.get("status")
    if status in ("executing", "planning"):
        return _ok(result={"status": status, "note": "already running"}, task_id=task_id)
    if status not in ("queued", "paused"):
        return _fail(f"cannot run task in status: {status}", task_id=task_id)
    _task_set_status(task_id, "queued")
    _start_task(
        task_id, task["goal"], task.get("session", BRIDGE_SESSION), task.get("profile", "full")
    )
    return _ok(result={"status": "started"}, task_id=task_id)


def _mcp_pause_task(args: dict) -> dict:
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return _fail("task_id is required")
    with _active_lock:
        ev = _active_tasks.get(task_id)
    if ev:
        ev.set()
    _task_set_status(task_id, "paused")
    return _ok(result={"status": "paused"}, task_id=task_id)


def _mcp_resume_task(args: dict) -> dict:
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return _fail("task_id is required")
    task = _task_get(task_id)
    if not task:
        return _fail("task not found", task_id=task_id)
    if task.get("status") != "paused":
        return _fail("task is not paused", task_id=task_id)
    # Clear old stop event and start fresh
    with _active_lock:
        _active_tasks.pop(task_id, None)
    _task_set_status(task_id, "queued")
    _start_task(
        task_id, task["goal"], task.get("session", BRIDGE_SESSION), task.get("profile", "full")
    )
    return _ok(result={"status": "resumed"}, task_id=task_id)


def _mcp_cancel_task(args: dict) -> dict:
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return _fail("task_id is required")
    with _active_lock:
        ev = _active_tasks.get(task_id)
        if ev:
            ev.set()
    _task_set_status(task_id, "cancelled")
    return _ok(result={"status": "cancelled"}, task_id=task_id)


def _mcp_list_tasks(args: dict) -> dict:
    status_filter = args.get("status") or None
    tasks = _task_list(status_filter)
    return _ok(result=tasks)


def _mcp_get_incidents(args: dict) -> dict:
    task_id = str(args.get("task_id") or "").strip()
    if not task_id:
        return _fail("task_id is required")
    incidents = _incidents_get(task_id)
    return _ok(result=incidents, task_id=task_id)


_HANDLERS = {
    "secretary.intake_task": _mcp_intake_task,
    "secretary.get_task": _mcp_get_task,
    "secretary.run_task": _mcp_run_task,
    "secretary.pause_task": _mcp_pause_task,
    "secretary.resume_task": _mcp_resume_task,
    "secretary.cancel_task": _mcp_cancel_task,
    "secretary.list_tasks": _mcp_list_tasks,
    "secretary.get_incidents": _mcp_get_incidents,
}


# ─── MCP tool schemas ─────────────────────────────────────────────────────────

_MCP_TOOLS = [
    {
        "name": "secretary.intake_task",
        "description": "Create a new autonomous secretary task. Returns task_id immediately. "
        "Optional 'profile' restricts the tool set sent to the LLM "
        "(filesystem/browser/full); auto-classified from goal if omitted.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "session": {"type": "string"},
                "profile": {"type": "string", "enum": ["filesystem", "browser", "full"]},
            },
            "required": ["goal"],
        },
    },
    {
        "name": "secretary.get_task",
        "description": "Get full status and step history for a task.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "secretary.run_task",
        "description": "Start or resume execution of a queued or paused task.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "secretary.pause_task",
        "description": "Pause a running task after the current step.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "secretary.resume_task",
        "description": "Resume a paused task.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "secretary.cancel_task",
        "description": "Abort a running or queued task.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "secretary.list_tasks",
        "description": "List tasks, optionally filtered by status (queued/executing/completed/failed/cancelled).",
        "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}}},
    },
    {
        "name": "secretary.get_incidents",
        "description": "Get incident log for a task.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
]


# ─── JSON-RPC stdio server ────────────────────────────────────────────────────


def _send(obj: dict):
    framed = bool(getattr(_send, "_framed", False))
    payload = json.dumps(obj).encode("utf-8")
    if framed:
        sys.stdout.buffer.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii"))
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
    else:
        sys.stdout.write(payload.decode("utf-8") + "\n")
        sys.stdout.flush()


def _read_exact(n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sys.stdin.buffer.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def _iter_rpc_messages():
    """Yield (msg, framed) from stdin.

    Supports:
    - framed stdio: Content-Length: N\\r\\n\\r\\n{json}
    - newline-delimited json: {json}\\n
    """
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return
        line_str = line.decode("utf-8", errors="replace").strip()
        if not line_str:
            continue

        if line_str.lower().startswith("content-length:"):
            try:
                n = int(line_str.split(":", 1)[1].strip())
            except Exception:
                sys.stderr.write(f"[secretary] bad content-length: {line_str}\n")
                sys.stderr.flush()
                continue

            # headers until blank line
            while True:
                h = sys.stdin.buffer.readline()
                if not h:
                    return
                if h in (b"\r\n", b"\n"):
                    break

            raw = _read_exact(n)
            if not raw:
                return
            try:
                yield json.loads(raw.decode("utf-8", errors="replace")), True
            except Exception as e:
                sys.stderr.write(f"[secretary] parse error: {e}\n")
                sys.stderr.flush()
            continue

        try:
            yield json.loads(line_str), False
        except Exception as e:
            sys.stderr.write(f"[secretary] parse error: {e}\n")
            sys.stderr.flush()
            continue


def _handle_rpc(msg: dict):
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "secretary", "version": "1.0.0"},
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": _MCP_TOOLS}}

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = _HANDLERS.get(name)
        if not handler:
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps(_fail(f"unknown tool: {name}"))}
                    ],
                    "isError": True,
                },
            }
        try:
            result = handler(args if isinstance(args, dict) else {})
            text = json.dumps(result, default=str)
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {"content": [{"type": "text", "text": text}]},
            }
        except Exception as e:
            err = {"ok": False, "reason": f"handler_error: {e}"}
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {"content": [{"type": "text", "text": json.dumps(err)}], "isError": True},
            }

    if mid is None:
        return None  # notification — drop silently

    return {
        "jsonrpc": "2.0",
        "id": mid,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


# ─── Standalone mode ──────────────────────────────────────────────────────────

_STANDALONE = False
_AUTO_SEED = os.environ.get("SECRETARY_AUTO_SEED", "1") == "1"
_CONTEXT_DIR = Path.home() / "Desktop" / "AI_CONTEXT"
_SCAN_INTERVAL = int(os.environ.get("SECRETARY_SCAN_INTERVAL", "30"))


def _read_latest_context() -> str:
    files = sorted(_CONTEXT_DIR.glob("context_*.txt"))
    if not files:
        return ""
    try:
        return files[-1].read_text()
    except Exception:
        return ""


def _seed_tasks_from_context():
    context = _read_latest_context()
    if not context:
        return
    active_match = re.search(r"\[ACTIVE TASK\]\s*(.*?)(?=\n\[|\Z)", context, re.DOTALL)
    if active_match:
        task_text = active_match.group(1).strip()
        if task_text and "none recorded" not in task_text.lower():
            existing = _task_list()
            for t in existing:
                if t["goal"] == task_text and t["status"] in ("queued", "executing"):
                    return
            _log(f"seeding task from context: {task_text[:80]}")
            _task_create(task_text)


def _autonomous_loop():
    """Background loop: claim queued tasks and auto-start them."""
    while True:
        try:
            queued = _task_list("queued")
            for task in queued:
                tid = task["task_id"]
                # Skip if already active
                with _active_lock:
                    if tid in _active_tasks:
                        continue
                _start_task(
                    tid,
                    task["goal"],
                    task.get("session", BRIDGE_SESSION),
                    task.get("profile", "full"),
                )
                time.sleep(2)  # stagger starts
            # If no tasks, seed from context
            if not queued and _AUTO_SEED:
                _seed_tasks_from_context()
            time.sleep(_SCAN_INTERVAL)
        except Exception as e:
            _log(f"autonomous loop error: {e}")
            time.sleep(_SCAN_INTERVAL)


# ─── HTTP health/stats server (port 8001, background thread) ─────────────────


def _http_server():
    try:
        from http.server import BaseHTTPRequestHandler, HTTPServer

        def _stats_data():
            conn = _db()
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
            ).fetchall()
            counts = {r["status"]: r["cnt"] for r in rows}
            total = sum(counts.values())
            events = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM task_events ORDER BY ts DESC LIMIT 10"
                ).fetchall()
            ]
            incidents = conn.execute("SELECT COUNT(*) as cnt FROM incidents").fetchone()["cnt"]
            conn.close()
            return {
                "tasks_total": total,
                "by_status": counts,
                "incident_count": incidents,
                "recent_events": events,
            }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass  # silence access log

            def _send_json(self, obj, code=200):
                body = json.dumps(obj, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/agent/health":
                    self._send_json(
                        {
                            "ok": True,
                            "claf": _claf_alive(),
                            "bridge": _bridge_alive(),
                            "active_tasks": len(_active_tasks),
                            "db": str(DB_PATH),
                        }
                    )
                elif self.path == "/agent/stats":
                    self._send_json(_stats_data())
                elif self.path == "/agent/tasks":
                    status = None
                    if "?" in self.path:
                        qs = self.path.split("?", 1)[1]
                        for pair in qs.split("&"):
                            if pair.startswith("status="):
                                status = pair.split("=", 1)[1]
                    self._send_json({"tasks": _task_list(status)})
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
                    profile = str(data.get("profile", "")).strip() or _classify_profile(goal)
                    if profile not in _PROFILES.get("profiles", {}):
                        profile = _PROFILES.get("default", "full")
                    t = _task_create(goal, data.get("session", BRIDGE_SESSION), profile)
                    # In standalone mode, auto-start the task
                    if _STANDALONE:
                        _start_task(
                            t["task_id"], goal, data.get("session", BRIDGE_SESSION), profile
                        )
                    self._send_json({"ok": True, "task": t}, code=201)
                else:
                    self._send_json({"error": "not found"}, code=404)

        server = HTTPServer(("127.0.0.1", HTTP_PORT), Handler)
        server.serve_forever()
    except Exception as e:
        sys.stderr.write(f"[secretary] http server error: {e}\n")


# ─── Entry point ──────────────────────────────────────────────────────────────


def _resume_interrupted_tasks():
    """On startup, re-queue any task left in executing/planning by a prior crash."""
    stuck = _task_list("executing") + _task_list("planning")
    for task in stuck:
        tid = task["task_id"]
        _log(f"resuming interrupted task {tid[:8]} goal={task.get('goal','')[:60]}")
        _task_set_status(tid, "queued")
        wid = f"resume-{_uid()[:8]}"
        threading.Thread(
            target=_run_loop,
            args=(
                tid,
                task["goal"],
                task.get("session", BRIDGE_SESSION),
                wid,
                task.get("profile", "full"),
            ),
            daemon=True,
        ).start()


def main():
    global _STANDALONE
    parser = argparse.ArgumentParser(description="Secretary Agent")
    parser.add_argument(
        "--standalone", action="store_true", help="Run in autonomous mode without MCP stdio"
    )
    args = parser.parse_args()
    _STANDALONE = args.standalone

    init_db()
    mode = "standalone" if _STANDALONE else "mcp"
    sys.stderr.write(f"[secretary] db={DB_PATH} claf={CLAF_URL} http=:{HTTP_PORT} mode={mode}\n")
    sys.stderr.flush()

    # Start HTTP server in background
    t = threading.Thread(target=_http_server, daemon=True)
    t.start()

    # Re-run any tasks that were executing when the process last died
    _resume_interrupted_tasks()

    if _STANDALONE:
        # Start autonomous task loop
        auto_t = threading.Thread(target=_autonomous_loop, daemon=True)
        auto_t.start()
        _log("standalone mode — autonomous loop started")
        # Keep main thread alive
        while True:
            time.sleep(60)
        return 0

    # MCP stdio loop
    for msg, framed in _iter_rpc_messages():
        _send._framed = framed
        try:
            resp = _handle_rpc(msg if isinstance(msg, dict) else {})
        except Exception as e:
            sys.stderr.write(f"[secretary] handler crash: {e}\n")
            sys.stderr.flush()
            resp = {
                "jsonrpc": "2.0",
                "id": (msg or {}).get("id"),
                "error": {"code": -32603, "message": f"internal: {e}"},
            }
        if resp is not None:
            _send(resp)

    return 0


if __name__ == "__main__":
    sys.exit(main())

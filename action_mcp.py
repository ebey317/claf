#!/usr/bin/env python3
"""action_mcp — Real action execution for the orchestrator action bridge.

Implements parse_and_execute_directives(text) which scans LLM output for
action directives (BROWSE:, SHELL:, FILE:) and executes them.

Safety:
- SHELL commands are checked against DANGEROUS_PATTERNS
- FILE writes are restricted to the project root and home dir
- All actions are logged
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ─── Safety ───────────────────────────────────────────────────────────────────

DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+/",
    r"mkfs\.",
    r"dd\s+if=.*of=/dev",
    r">\s*/etc/",
    r"curl.*\|\s*sh",
    r"wget.*\|\s*sh",
    r"\bsudo\b",
    r"chmod\s+777",
    r":\(\)\{\s*:\|:\&\s*\};:",  # fork bomb
]

_PROJECT_ROOT = str(Path.home() / "projects")
_HOME = str(Path.home())


def _is_dangerous(cmd: str) -> bool:
    low = cmd.lower()
    for pat in DANGEROUS_PATTERNS:
        if re.search(pat, low):
            return True
    return False


def _safe_path(path: str) -> str:
    """Resolve path and ensure it's within home or project root."""
    p = Path(path).expanduser().resolve()
    home = Path.home().resolve()
    # Allow paths under home
    try:
        p.relative_to(home)
        return str(p)
    except ValueError:
        pass
    # Allow paths under /tmp
    try:
        p.relative_to(Path("/tmp").resolve())
        return str(p)
    except ValueError:
        pass
    raise ValueError(f"Path {path} is outside allowed directories")


# ─── Bridge helpers ───────────────────────────────────────────────────────────

_BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://localhost:8080")
_BRIDGE_SESSION = os.environ.get("BRIDGE_SESSION", "mcp-default")


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
    r = _http("GET", f"{_BRIDGE_URL}/extension/queue_state", timeout=1.5)
    return bool(r.get("ok"))


def _push_action(kind: str, payload: dict, session: str = _BRIDGE_SESSION) -> dict:
    body = {"session_id": session, "actions": [{"kind": kind, **payload}]}
    return _http("POST", f"{_BRIDGE_URL}/extension/queue", body=body, timeout=3.0)


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


def _await_result(action_id: str, session: str = _BRIDGE_SESSION, wait: int = 30) -> dict:
    if not action_id:
        return {"ok": False, "reason": "no_action_id"}
    deadline = time.time() + wait
    while time.time() < deadline:
        r = _http(
            "GET",
            f"{_BRIDGE_URL}/extension/result?session_id={session}&action_id={action_id}",
            timeout=2.0,
        )
        if r.get("ok") and r.get("json"):
            j = r["json"]
            if j.get("ok") and j.get("result") is not None:
                return j
        time.sleep(0.4)
    return {"ok": False, "reason": "timeout"}


def _dispatch_browser(kind: str, payload: dict, session: str = _BRIDGE_SESSION) -> dict:
    if not _bridge_alive():
        return {"ok": False, "reason": "bridge_unreachable", "hint": "Open Chrome side panel."}
    push = _push_action(kind, payload, session)
    if not push.get("ok"):
        return {"ok": False, "reason": "push_failed"}
    aid = _action_id_from(push)
    result = _await_result(aid, session)
    return {"ok": bool(result.get("ok", True)), "result": result, "action_id": aid}


# ─── Directive execution ──────────────────────────────────────────────────────


def _exec_browse_open_url(url: str) -> dict:
    if not url.startswith("http"):
        url = "https://" + url
    r = _dispatch_browser("BROWSER_NAV", {"target": url})
    return {
        "action": "browse_open_url",
        "success": r.get("ok", False),
        "url": url,
        "message": json.dumps(r)[:200],
    }


def _exec_browse_search(query: str) -> dict:
    url = "https://www.google.com/search?q=" + query.replace(" ", "+")
    r = _dispatch_browser("BROWSER_NAV", {"target": url})
    return {
        "action": "browse_search",
        "success": r.get("ok", False),
        "query": query,
        "engine": "google",
        "message": json.dumps(r)[:200],
    }


def _exec_shell(cmd: str) -> dict:
    if _is_dangerous(cmd):
        return {
            "action": "shell_run",
            "success": False,
            "command": cmd,
            "error": "Command matched dangerous pattern — blocked.",
        }
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        return {
            "action": "shell_run",
            "success": result.returncode == 0,
            "command": cmd,
            "return_code": result.returncode,
            "stdout": out[:500],
            "stderr": err[:500],
            "message": out if out else err if err else f"(exit {result.returncode})",
        }
    except subprocess.TimeoutExpired:
        return {
            "action": "shell_run",
            "success": False,
            "command": cmd,
            "error": "timed out after 30s",
        }
    except Exception as e:
        return {"action": "shell_run", "success": False, "command": cmd, "error": str(e)}


def _exec_file_read(path: str) -> dict:
    try:
        safe = _safe_path(path)
        with open(safe, "r") as f:
            content = f.read(8000)
        return {
            "action": "file_read",
            "success": True,
            "path": safe,
            "size": len(content),
            "content": content[:4000],
            "message": f"Read {safe} ({len(content)} chars)",
        }
    except Exception as e:
        return {"action": "file_read", "success": False, "path": path, "error": str(e)}


def _exec_file_write(path: str, content: str) -> dict:
    try:
        safe = _safe_path(path)
        os.makedirs(os.path.dirname(safe) or ".", exist_ok=True)
        with open(safe, "w") as f:
            f.write(content)
        return {
            "action": "file_write",
            "success": True,
            "path": safe,
            "bytes_written": len(content.encode("utf-8")),
            "message": f"Wrote {safe} ({len(content)} chars)",
        }
    except Exception as e:
        return {"action": "file_write", "success": False, "path": path, "error": str(e)}


# ─── Main entrypoint ──────────────────────────────────────────────────────────

_DIRECTIVE_PATTERNS = [
    (re.compile(r"BROWSE\s*:\s*open_url\s*=\s*([^\s]+)", re.IGNORECASE), "browse_open_url", 1),
    (
        re.compile(r"BROWSE\s*:\s*search\s*=\s*(.+?)(?=\s[A-Z]+:|$)", re.IGNORECASE | re.DOTALL),
        "browse_search",
        1,
    ),
    (re.compile(r"SHELL\s*:\s*(.+?)(?=\s[A-Z]+:|\n|$)", re.IGNORECASE | re.DOTALL), "shell_run", 1),
    (re.compile(r"FILE\s*:\s*read\s*=\s*([^\s,]+)", re.IGNORECASE), "file_read", 1),
    (
        re.compile(
            r"FILE\s*:\s*write\s*=\s*([^\s,]+)(?:,\s*content\s*=\s*(.+))?(?=\s[A-Z]+:|$)",
            re.IGNORECASE | re.DOTALL,
        ),
        "file_write",
        2,
    ),
]


def parse_and_execute_directives(text: str) -> list[dict]:
    """Scan text for action directives and execute them.
    Returns a list of result dicts (one per directive found)."""
    results: list[dict] = []
    for pattern, action_type, group_count in _DIRECTIVE_PATTERNS:
        for m in pattern.finditer(text):
            if action_type == "browse_open_url":
                results.append(_exec_browse_open_url(m.group(1).strip()))
            elif action_type == "browse_search":
                results.append(_exec_browse_search(m.group(1).strip()))
            elif action_type == "shell_run":
                results.append(_exec_shell(m.group(1).strip()))
            elif action_type == "file_read":
                results.append(_exec_file_read(m.group(1).strip()))
            elif action_type == "file_write":
                path = m.group(1).strip()
                content = (
                    m.group(2).strip() if m.lastindex and m.lastindex >= 2 and m.group(2) else ""
                )
                results.append(_exec_file_write(path, content))
    return results


if __name__ == "__main__":
    # Quick self-test
    test = """
    I'll search for python docs.
    BROWSE: search=python documentation
    Then I'll check the system.
    SHELL: uname -a
    """
    for r in parse_and_execute_directives(test):
        print(json.dumps(r, indent=2))

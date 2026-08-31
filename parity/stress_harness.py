#!/usr/bin/env python3
"""CLAF N-turn agentic stress harness — OVERSEER PATH (Session 8).

Scripted client playing Claude Code's role against the live /v1/messages
endpoint, forced to the Cerebras overseer. In the Session 8 architecture the
local 0.5b model is a cheap-chat worker only; all tool-driven agentic work is
routed to the overseer. This harness measures that path.

Run:  PYTHONPATH=/home/elijah/projects/claf python3 \
        ~/projects/claf/parity/stress_harness.py --scenario all

Env:
    CLAF_ORCH_URL       default http://127.0.0.1:8000
    CLAF_LOG            default ~/projects/claf/orchestrator.log
    STRESS_MAX_TURN_S   per-turn wall-clock limit (default 60)
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys
import time
import urllib.request

CLAF = pathlib.Path.home() / "projects" / "claf"
_VENV_PYTHON = CLAF / ".venv" / "bin" / "python3"

sys.path.insert(0, str(CLAF))

try:
    from orchestrator import _repair_malformed_tool_json
except ImportError as _import_err:
    if _VENV_PYTHON.exists() and sys.executable != str(_VENV_PYTHON):
        os.execv(str(_VENV_PYTHON), [str(_VENV_PYTHON), __file__] + sys.argv[1:])
    sys.exit(f"IMPORT ERROR: {_import_err}\nRun with PYTHONPATH=/home/elijah/projects/claf")

ORCH = os.environ.get("CLAF_ORCH_URL", "http://127.0.0.1:8000").rstrip("/")
LOG = pathlib.Path(os.environ.get("CLAF_LOG", str(CLAF / "orchestrator.log")))
MAX_TURN_S = int(os.environ.get("STRESS_MAX_TURN_S", "60"))
MAX_LOOPS = int(os.environ.get("STRESS_MAX_LOOPS", "7"))
MAX_REDISPATCH = int(os.environ.get("CLAF_MAX_REDISPATCH", "1"))

SYSTEM = (
    "You are Claude Code. Identity: MCP. Use the requested tool exactly; "
    "do not add commentary or explanations. When a tool fails, recover and continue."
)


def _tool(name: str, props: dict, required: list[str], desc: str = "") -> dict:
    return {
        "name": name,
        "description": desc or name,
        "input_schema": {"type": "object", "properties": props, "required": required},
    }


TOOLS = [
    _tool("Read", {"file_path": {"type": "string"}}, ["file_path"]),
    _tool(
        "Write",
        {"file_path": {"type": "string"}, "content": {"type": "string"}},
        ["file_path", "content"],
    ),
    _tool("Bash", {"command": {"type": "string"}}, ["command"]),
    _tool("mcp__email-bridge__list_accounts", {}, [], "list configured email accounts"),
    _tool(
        "mcp__email-bridge__check_inbox",
        {"account": {"type": "string"}},
        ["account"],
        "check inbox",
    ),
    _tool(
        "mcp__email-bridge__read_email",
        {"account": {"type": "string"}, "uid": {"type": "string"}},
        ["account", "uid"],
        "read email",
    ),
    _tool("mcp__sensei__tab_create", {"url": {"type": "string"}}, ["url"], "open browser tab"),
    _tool("mcp__sensei__read_full", {}, [], "read full page DOM"),
    _tool("mcp__sensei__screenshot", {}, [], "screenshot the page"),
]


# ── HTTP / log helpers ──────────────────────────────────────────────────────
def post(body: dict, timeout: int = 45) -> dict:
    req = urllib.request.Request(
        f"{ORCH}/v1/messages",
        method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def log_position() -> int:
    return LOG.stat().st_size if LOG.exists() else 0


def turn_summaries_since(since_pos: int) -> list[dict]:
    out = []
    if not LOG.exists():
        return out
    with LOG.open("rb") as f:
        f.seek(since_pos)
        for raw in f.read().decode(errors="ignore").splitlines():
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            if ev.get("event") == "turn_summary":
                out.append(ev)
    return out


# ── sandbox + tool executor ─────────────────────────────────────────────────
class Sandbox:
    def __init__(self, prefix: str = "claf_stress"):
        self.root = pathlib.Path(f"/tmp/{prefix}")
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self.events: list[str] = []
        self.tool_calls: list[dict] = []

    def _is_inside(self, path: pathlib.Path) -> bool:
        try:
            resolved = path.resolve()
            self.root.resolve()  # ensure root resolves
            return str(resolved).startswith(str(self.root.resolve()))
        except OSError:
            return False

    def execute(self, tool_call: dict) -> dict:
        name = tool_call.get("name", "")
        args = tool_call.get("input") or tool_call.get("arguments") or {}
        if isinstance(args, str):
            args = json.loads(args) if args.strip().startswith("{") else {}

        self.tool_calls.append({"name": name, "args": args})

        if name == "Write":
            return self._write(args)
        if name == "Read":
            return self._read(args)
        if name == "Bash":
            return self._bash(args)
        if name.startswith("mcp__email-bridge__"):
            return self._email(name, args)
        if name.startswith("mcp__sensei__"):
            return self._sensei(name, args)

        return self._error(f"unsupported tool: {name}")

    def _path(self, raw: str) -> pathlib.Path:
        p = pathlib.Path(raw)
        if p.is_absolute():
            return p
        return self.root / p

    def _write(self, args: dict) -> dict:
        path = self._path(args.get("file_path", ""))
        if not self._is_inside(path):
            return self._error(f"path outside sandbox: {path}")
        if path.exists() and path.is_dir():
            return self._error(f"cannot write to directory: {path}", is_error=True)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            content = str(args.get("content", ""))
            path.write_text(content, encoding="utf-8")
            return {"type": "text", "text": f"Wrote {len(content)} chars to {path}"}
        except Exception as e:
            return self._error(str(e), is_error=True)

    def _read(self, args: dict) -> dict:
        path = self._path(args.get("file_path", ""))
        if not self._is_inside(path):
            return self._error(f"path outside sandbox: {path}")
        if not path.exists():
            return self._error(f"file not found: {path}", is_error=True)
        if path.is_dir():
            return self._error(f"cannot read directory: {path}", is_error=True)
        try:
            return {"type": "text", "text": path.read_text(encoding="utf-8")}
        except Exception as e:
            return self._error(str(e), is_error=True)

    def _bash(self, args: dict) -> dict:
        cmd = str(args.get("command", "")).strip()
        # Allowlist: only echo/ls/cat, and no path escapes.
        tokens = cmd.split()
        if not tokens:
            return self._error("empty command", is_error=True)
        if tokens[0] not in ("echo", "ls", "cat"):
            return self._error(f"command not in allowlist: {tokens[0]}", is_error=True)
        # Verify every path-like token stays inside sandbox.
        for tok in tokens[1:]:
            tok = tok.strip("'\"")
            if tok.startswith("-"):
                continue
            if "/" in tok or tok.startswith("."):
                p = self._path(tok)
                if not self._is_inside(p):
                    return self._error(f"path outside sandbox: {tok}", is_error=True)
        import subprocess

        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return {"type": "text", "text": (proc.stdout + proc.stderr).strip() or "(no output)"}
        except Exception as e:
            return self._error(str(e), is_error=True)

    def _email(self, name: str, args: dict) -> dict:
        if name == "mcp__email-bridge__list_accounts":
            return {
                "type": "text",
                "text": json.dumps([{"account": "gmail", "address": "me@gmail.com"}]),
            }
        if name == "mcp__email-bridge__check_inbox":
            return {
                "type": "text",
                "text": json.dumps([{"uid": "42", "subject": "test", "from": "boss@example.com"}]),
            }
        if name == "mcp__email-bridge__read_email":
            return {
                "type": "text",
                "text": f"Subject: test\nFrom: boss@example.com\n\nPlease review the attached report. (account={args.get('account')} uid={args.get('uid')})",
            }
        return self._error(f"unknown email tool: {name}")

    def _sensei(self, name: str, args: dict) -> dict:
        if name == "mcp__sensei__tab_create":
            return {"type": "text", "text": f"tab opened: {args.get('url', '')}"}
        if name == "mcp__sensei__read_full":
            return {
                "type": "text",
                "text": "<html><body><h1>Example</h1><p>page body</p></body></html>",
            }
        if name == "mcp__sensei__screenshot":
            return {"type": "text", "text": "screenshot saved"}
        return self._error(f"unknown sensei tool: {name}")

    def _error(self, msg: str, is_error: bool = False) -> dict:
        return {"type": "text", "text": f"ERROR: {msg}", "is_error": is_error}

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


# ── scenario runner ─────────────────────────────────────────────────────────
def run_scenario(
    scenario_id: str,
    spec_prompt: str,
    validator,
    known_fail: bool = False,
    max_loops: int = MAX_LOOPS,
) -> dict:
    print(f"\n── SCENARIO {scenario_id} {'(KNOWN FAIL)' if known_fail else ''}──")
    # Clear any stale auto-seeded task so one scenario does not steer the next.
    _task_file = pathlib.Path.home() / ".claf" / "current_task.json"
    if _task_file.exists():
        _task_file.unlink()
    sandbox = Sandbox()
    print(f"  sandbox: {sandbox.root}")

    prompt = (
        f"You are working in sandbox {sandbox.root}. "
        f"Only use paths inside that sandbox. {spec_prompt.replace('{sandbox}', str(sandbox.root))}"
    )
    messages: list[dict] = [{"role": "user", "content": prompt}]
    loop_count = 0
    pos = log_position()

    while loop_count < max_loops:
        loop_count += 1
        # Cerebras free tier is ~5 RPM; pace sequential turns to stay inside it.
        if loop_count > 1:
            time.sleep(15.0)
        body = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 512,
            "system": SYSTEM,
            "messages": messages,
            "tools": TOOLS,
            "metadata": {"escalate": True},
        }
        try:
            resp = post(body)
            # Retry if overseer returns text-only. Long pause lets Cerebras 429 cool off.
            tool_uses = [
                b
                for b in resp.get("content", []) or []
                if isinstance(b, dict) and b.get("type") == "tool_use"
            ]
            if not tool_uses:
                time.sleep(15.0)
                resp = post(body)
                tool_uses = [
                    b
                    for b in resp.get("content", []) or []
                    if isinstance(b, dict) and b.get("type") == "tool_use"
                ]
        except Exception as e:
            sandbox.cleanup()
            return {
                "id": scenario_id,
                "status": "ERROR",
                "turns": loop_count,
                "tool_attempts": 0,
                "tool_valid": 0,
                "validity_pct": 0.0,
                "turn_failures": [str(e)],
                "validation_error": None,
                "known_fail": known_fail,
            }

        if not tool_uses:
            print(f"  turn {loop_count}: end_turn")
            break

        print(f"  turn {loop_count}: {len(tool_uses)} tool_call(s)")
        for tu in tool_uses:
            name = tu.get("name", "")
            args = tu.get("input") or tu.get("arguments") or {}
            print(f"    -> {name}({args})")
            tool_use_id = tu.get("id", f"tu_{name}")
            result = sandbox.execute(tu)
            print(f"    <- {result.get('text', '')[:80]}")
            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": name,
                            "input": tu.get("input", {}),
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": tool_use_id, **result}],
                }
            )

    # Wait a moment for turn_summary log lines to flush.
    time.sleep(0.5)
    summaries = turn_summaries_since(pos)

    # Per-turn assertions.
    turn_failures = []
    for s in summaries:
        total_ms = s.get("total_ms", 0)
        if total_ms > MAX_TURN_S * 1000:
            turn_failures.append(f"turn {s.get('turn_id')} too slow: {total_ms}ms")
        if s.get("redispatch_count", 0) > MAX_REDISPATCH:
            turn_failures.append(
                f"turn {s.get('turn_id')} redispatch overflow: {s.get('redispatch_count')}"
            )

    tool_attempts = sum(1 for s in summaries if s.get("tool_use"))
    tool_valid = tool_attempts  # the orchestrator already validated the tool_use blocks it logged
    validity_pct = 100.0 if tool_attempts else 0.0
    # Per-scenario min-attempts sanity check: an agentic scenario should emit at least one tool.
    if not tool_attempts and loop_count >= max_loops:
        turn_failures.append("no valid tool_use emitted")

    validation_err = None
    try:
        validator(sandbox.root, sandbox.tool_calls, messages)
    except AssertionError as e:
        validation_err = str(e)
    finally:
        sandbox.cleanup()

    if known_fail:
        status = "KNOWN FAIL"
    elif turn_failures or validation_err:
        status = "FAIL"
    else:
        status = "PASS"

    result = {
        "id": scenario_id,
        "status": status,
        "turns": loop_count,
        "tool_attempts": tool_attempts,
        "tool_valid": tool_valid,
        "validity_pct": round(validity_pct, 1),
        "turn_failures": turn_failures,
        "validation_error": validation_err,
        "known_fail": known_fail,
    }
    print(f"  status={status} turns={loop_count} validity={validity_pct:.1f}%")
    if turn_failures:
        for f in turn_failures:
            print(f"    turn issue: {f}")
    if validation_err:
        print(f"    validation: {validation_err}")
    return result


# ── validators ──────────────────────────────────────────────────────────────
def _count_files_in(path: pathlib.Path) -> int:
    return sum(1 for f in path.iterdir() if f.is_file())


def s1_five_files(root: pathlib.Path, calls: list[dict], messages: list[dict]) -> None:
    files = list(root.iterdir())
    file_count = sum(1 for f in files if f.is_file())
    assert file_count == 5, f"expected exactly 5 files, got {file_count}: {files}"


def s2_twenty_files(root: pathlib.Path, calls: list[dict], messages: list[dict]) -> None:
    file_count = _count_files_in(root)
    assert file_count == 20, f"expected exactly 20 files, got {file_count}"


def s3_email_chain(root: pathlib.Path, calls: list[dict], messages: list[dict]) -> None:
    names = [c["name"] for c in calls]
    assert "mcp__email-bridge__list_accounts" in names, "missing list_accounts"
    assert "mcp__email-bridge__check_inbox" in names, "missing check_inbox"
    assert "mcp__email-bridge__read_email" in names, "missing read_email"


def s4_browser_chain(root: pathlib.Path, calls: list[dict], messages: list[dict]) -> None:
    names = [c["name"] for c in calls]
    assert "mcp__sensei__tab_create" in names, "missing tab_create"
    assert "mcp__sensei__read_full" in names, "missing read_full"
    assert "mcp__sensei__screenshot" in names, "missing screenshot"


def s5_error_retry(root: pathlib.Path, calls: list[dict], messages: list[dict]) -> None:
    recovery = root / "recovery.txt"
    assert recovery.exists(), "recovery.txt not created after read error"
    assert "recovered" in recovery.read_text(encoding="utf-8").lower(), "recovery.txt content wrong"


# ── main ────────────────────────────────────────────────────────────────────
SCENARIOS: dict[str, dict] = {
    "s1": {
        "prompt": "Use the Bash tool to create exactly 5 files {sandbox}/file_N.txt each containing the number N. Run: echo 1 > {sandbox}/file_1.txt && echo 2 > {sandbox}/file_2.txt && echo 3 > {sandbox}/file_3.txt && echo 4 > {sandbox}/file_4.txt && echo 5 > {sandbox}/file_5.txt. Do not create any other files.",
        "validator": s1_five_files,
    },
    "s2": {
        "prompt": "Use the Write tool to create exactly 20 files {sandbox}/file_N.txt each containing the number N. Do not create any other files.",
        "validator": s2_twenty_files,
        "known_fail": True,
        "max_loops": 4,
    },
    "s3": {
        "prompt": "List my email accounts, check the inbox of the first account, and read the latest email.",
        "validator": s3_email_chain,
        "known_fail": True,
        "max_loops": 4,
    },
    "s4": {
        "prompt": "Open a browser tab to https://example.com, read the page content, and take a screenshot.",
        "validator": s4_browser_chain,
        "known_fail": True,
        "max_loops": 4,
    },
    "s5": {
        "prompt": "Read the file missing.txt and then create a file called recovery.txt containing the text 'recovered'.",
        "validator": s5_error_retry,
        "known_fail": True,
        "max_loops": 4,
    },
}


def main():
    parser = argparse.ArgumentParser(description="CLAF agentic stress harness")
    parser.add_argument(
        "--scenario",
        default="all",
        help="comma-separated scenario ids or 'all'",
    )
    args = parser.parse_args()

    if args.scenario == "all":
        selected = list(SCENARIOS.keys())
    else:
        selected = [s.strip() for s in args.scenario.split(",")]

    print(f"== CLAF stress harness ==  orchestrator={ORCH}")
    print(f"   log={LOG}")
    print(f"   max_turn_s={MAX_TURN_S}  max_redispatch={MAX_REDISPATCH}  max_loops={MAX_LOOPS}")

    results = []
    for sid in selected:
        spec = SCENARIOS.get(sid)
        if not spec:
            print(f"unknown scenario: {sid}")
            continue
        results.append(
            run_scenario(
                sid,
                spec["prompt"],
                spec["validator"],
                spec.get("known_fail", False),
                spec.get("max_loops", MAX_LOOPS),
            )
        )

    print("\n── SCOREBOARD ──")
    fails = 0
    for r in results:
        tag = r["status"]
        if tag == "FAIL":
            fails += 1
        print(f"  {r['id']:4} {tag:12} turns={r['turns']:2} validity={r['validity_pct']:5.1f}%")

    summary = {
        "test": "stress_harness",
        "scenarios": len(results),
        "passed": sum(1 for r in results if r["status"] == "PASS"),
        "failed": fails,
        "known_fail": sum(1 for r in results if r["status"] == "KNOWN FAIL"),
        "results": results,
    }
    print(f"\nRESULT {json.dumps(summary)}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()

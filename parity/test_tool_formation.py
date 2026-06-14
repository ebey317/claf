#!/usr/bin/env python3
"""CLAF tool-formation parity corpus — OVERSEER PATH (Session 8).

20 deterministic single-shot prompts vs the live /v1/messages endpoint,
forced to the Cerebras overseer (`gpt-oss-120b`). In the Session 8
architecture the local 0.5b model is a cheap-chat worker only; all tool
use is routed to the overseer. This test measures that path.

Run:  PYTHONPATH=/home/elijah/projects/claf python3 \
        ~/projects/claf/parity/test_tool_formation.py

Env:
    CLAF_ORCH_URL   default http://127.0.0.1:8000
    CLAF_LOG        default ~/projects/claf/orchestrator.log
"""
from __future__ import annotations
import json, os, sys, time, urllib.request, pathlib

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
LOG = pathlib.Path(os.environ.get(
    "CLAF_LOG", str(CLAF / "orchestrator.log")))

SYSTEM = ("You are Claude Code. Identity: MCP. Use the requested tool exactly; "
          "do not add commentary or explanations.")

# ── tool factory ────────────────────────────────────────────────────────────
def _tool(name: str, props: dict, required: list[str], desc: str = "") -> dict:
    return {"name": name, "description": desc or name,
            "input_schema": {"type": "object", "properties": props, "required": required}}

TOOLS = [
    _tool("Read", {"file_path": {"type": "string"}}, ["file_path"]),
    _tool("Write", {"file_path": {"type": "string"}, "content": {"type": "string"}},
          ["file_path", "content"]),
    _tool("Bash", {"command": {"type": "string"}}, ["command"]),
    _tool("TaskCreate", {"subject": {"type": "string"}, "description": {"type": "string"}},
          ["subject", "description"]),
    _tool("mcp__sensei__tab_create", {"url": {"type": "string"}}, ["url"]),
]

# ── corpus ──────────────────────────────────────────────────────────────────
# Each entry: id, prompt, expected tool name, expected args dict.
CORPUS = [
    # Write — exact path + content
    ("w1", "Write the exact text 'alpha' to /tmp/claf_formation_w1.txt", "Write",
     {"file_path": "/tmp/claf_formation_w1.txt", "content": "alpha"}),
    ("w2", "Create file /tmp/claf_formation_w2.txt containing 'beta'", "Write",
     {"file_path": "/tmp/claf_formation_w2.txt", "content": "beta"}),
    ("w3", "Save 'gamma' to /tmp/claf_formation_w3.txt", "Write",
     {"file_path": "/tmp/claf_formation_w3.txt", "content": "gamma"}),
    ("w4", "Write /tmp/claf_formation_w4.txt with content 'delta'", "Write",
     {"file_path": "/tmp/claf_formation_w4.txt", "content": "delta"}),

    # Read — exact path (files are pre-created by the harness)
    ("r1", "Read the file /tmp/claf_formation_r1.txt", "Read",
     {"file_path": "/tmp/claf_formation_r1.txt"}),
    ("r2", "Read /tmp/claf_formation_r2.txt", "Read",
     {"file_path": "/tmp/claf_formation_r2.txt"}),
    ("r3", "Show me the contents of /tmp/claf_formation_r3.txt", "Read",
     {"file_path": "/tmp/claf_formation_r3.txt"}),
    ("r4", "Open and read /tmp/claf_formation_r4.txt", "Read",
     {"file_path": "/tmp/claf_formation_r4.txt"}),

    # Bash — exact echo command
    ("b1", "Run bash command: echo 'echo1'", "Bash",
     {"command": "echo 'echo1'"}),
    ("b2", "Execute echo 'echo2' in bash", "Bash",
     {"command": "echo 'echo2'"}),
    ("b3", "Bash: echo 'echo3'", "Bash",
     {"command": "echo 'echo3'"}),
    ("b4", "Use Bash to echo 'echo4'", "Bash",
     {"command": "echo 'echo4'"}),

    # Browser — exact URL
    ("t1", "Open a browser tab to https://example.com", "mcp__sensei__tab_create",
     {"url": "https://example.com"}),
    ("t2", "Create a browser tab for https://kimi.com", "mcp__sensei__tab_create",
     {"url": "https://kimi.com"}),
    ("t3", "Open https://example.org in a new tab", "mcp__sensei__tab_create",
     {"url": "https://example.org"}),
    ("t4", "Visit https://example.net via tab_create", "mcp__sensei__tab_create",
     {"url": "https://example.net"}),

    # TaskCreate — exact subject
    ("c1", "Create a task with subject 'review logs'", "TaskCreate",
     {"subject": "review logs"}),
    ("c2", "Make a new task titled 'update documentation'", "TaskCreate",
     {"subject": "update documentation"}),
    ("c3", "TaskCreate subject 'test tool formation'", "TaskCreate",
     {"subject": "test tool formation"}),
    ("c4", "Add a task: 'clean up temp files'", "TaskCreate",
     {"subject": "clean up temp files"}),
]


# ── helpers ─────────────────────────────────────────────────────────────────
def _prepare_read_files():
    for _id, prompt, name, expected in CORPUS:
        if name == "Read":
            path = expected["file_path"]
            pathlib.Path(path).write_text(f"contents of {path}\n", encoding="utf-8")


def post(body: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        f"{ORCH}/v1/messages", method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def extract_tools(resp: dict) -> list[dict]:
    tools = []
    for b in resp.get("content", []) or []:
        if isinstance(b, dict) and b.get("type") == "tool_use":
            tools.append(b)
    return tools


def log_events_for_turn(turn_id: str, since_pos: int) -> dict:
    out = {"repaired": 0, "repair_failed": 0}
    if not LOG.exists():
        return out
    with LOG.open("rb") as f:
        f.seek(since_pos)
        for raw in f.read().decode(errors="ignore").splitlines():
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            if ev.get("turn_id") != turn_id:
                continue
            if ev.get("event") == "tool_call_repaired":
                out["repaired"] += ev.get("count", 1)
            elif ev.get("event") == "tool_call_repair_failed":
                out["repair_failed"] += 1
    return out


def score_one(cid: str, prompt: str, expect_name: str, expect_args: dict) -> dict:
    pos = LOG.stat().st_size if LOG.exists() else 0
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 256,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
        "tools": TOOLS,
        "metadata": {"escalate": True},
    }
    t0 = time.time()
    err = None
    resp = {}
    try:
        resp = post(body)
        # Retry if the overseer returned text-only. A long pause lets a
        # Cerebras 429 flash-cap cool off; a short pause handles transient
        # cloud refusals without waiting when the request already succeeded.
        if not any(b.get("type") == "tool_use" for b in resp.get("content", []) or []):
            # Cerebras free tier is ~5 RPM; a long pause lets a 429/flash-cap cool off.
            time.sleep(20.0)
            resp = post(body)
    except Exception as e:
        err = str(e)
    dt = round(time.time() - t0, 1)

    tools = extract_tools(resp)
    turn_id = resp.get("id", "")
    log_ev = log_events_for_turn(turn_id, pos)

    tool_emitted = len(tools) >= 1
    name_ok = tool_emitted and tools[0].get("name") == expect_name
    args = tools[0].get("input", {}) if tools else {}
    schema_ok = False
    exact_ok = False
    if name_ok:
        schema = next((t["input_schema"] for t in TOOLS if t["name"] == expect_name), {})
        required = schema.get("required", [])
        schema_ok = all(k in args for k in required)
        exact_ok = all(args.get(k) == v for k, v in expect_args.items())

    return {
        "id": cid,
        "prompt": prompt,
        "expected": expect_name,
        "dt": dt,
        "err": err,
        "tool_emitted": tool_emitted,
        "name_ok": name_ok,
        "schema_ok": schema_ok,
        "exact_ok": exact_ok,
        "got_name": tools[0].get("name") if tools else None,
        "got_args": args,
        "repaired": log_ev["repaired"],
        "repair_failed": log_ev["repair_failed"],
    }


def main():
    print(f"== CLAF tool-formation corpus ==  orchestrator={ORCH}")
    print(f"   log={LOG}\n")
    _prepare_read_files()

    rows = []
    for cid, prompt, expect_name, expect_args in CORPUS:
        rows.append(score_one(cid, prompt, expect_name, expect_args))
        # Cerebras free tier is ~5 RPM; pace sequential prompts to stay inside it.
        time.sleep(15.0)

    print(f"{'id':4} {'expect':22} {'emit':5} {'name':5} {'schema':7} {'exact':6} {'dt':>5}  notes")
    for r in rows:
        notes = []
        if r["repaired"]:
            notes.append(f"repaired={r['repaired']}")
        if r["repair_failed"]:
            notes.append(f"repair_failed={r['repair_failed']}")
        if r["err"]:
            notes.append(f"err={r['err'][:40]}")
        if not r["name_ok"] and r["got_name"]:
            notes.append(f"got={r['got_name']}")
        print(f"{r['id']:4} {r['expected']:22} "
              f"{'Y' if r['tool_emitted'] else 'N':5} "
              f"{'Y' if r['name_ok'] else 'N':5} "
              f"{'Y' if r['schema_ok'] else 'N':7} "
              f"{'Y' if r['exact_ok'] else 'N':6} "
              f"{r['dt']:>5}  {', '.join(notes)}")

    total = len(rows)
    emit = sum(1 for r in rows if r["tool_emitted"])
    name = sum(1 for r in rows if r["name_ok"])
    schema = sum(1 for r in rows if r["schema_ok"])
    exact = sum(1 for r in rows if r["exact_ok"])
    repaired = sum(r["repaired"] for r in rows)
    failed = sum(r["repair_failed"] for r in rows)

    print(f"\nSCOREBOARD  emit={emit}/{total}  name={name}/{total}  "
          f"schema={schema}/{total}  exact={exact}/{total}")
    print(f"REPAIR LOG  repaired={repaired}  repair_failed={failed}")

    result = {
        "test": "tool_formation",
        "total": total,
        "tool_emitted": emit,
        "name_correct": name,
        "schema_valid": schema,
        "exact_args": exact,
        "repaired": repaired,
        "repair_failed": failed,
    }
    print(f"RESULT {json.dumps(result)}")
    sys.exit(0 if exact >= 16 else 1)



if __name__ == "__main__":
    main()

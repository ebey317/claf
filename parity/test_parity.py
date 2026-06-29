#!/usr/bin/env python3
"""CLAF hybrid parity harness.

Answers the operator's question empirically: "if the hybrid ended a thread and
emitted a result, would it be the same result as what I (cloud Claude) would say?"

A 9b local model never emits token-identical text to cloud. So 'parity' is scored
across 5 measurable LAYERS, not prose similarity:

  ROUTING      did the request that needed cloud actually escalate?
  CONTEXT      did identity (charter) + the operator's words survive the trim?
  CAPABILITY   did the right tool reach local AND parse back to a tool_use?
  BEHAVIOR     act-first, show evidence, no fake "done", no giving up?
  TERMINATION  stop when done, replan when stuck, no infinite loop?

Run ON the gaming PC against its live orchestrator:
    python3 ~/projects/claf/parity/test_parity.py
Env:
    CLAF_ORCH_URL   default http://127.0.0.1:8000
    CLAF_LOG        default ~/projects/claf/orchestrator.log
"""

from __future__ import annotations
import json, os, sys, time, urllib.request, urllib.error, pathlib

ORCH = os.environ.get("CLAF_ORCH_URL", "http://127.0.0.1:8000").rstrip("/")
LOG = pathlib.Path(
    os.environ.get("CLAF_LOG", str(pathlib.Path.home() / "projects/claf/orchestrator.log"))
)

# Make the orchestrator importable for the deterministic parser check (prompt 10).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# ROUTING ORACLE  — operator's rule (2026-06-11): escalate when it's too hard
# for local; otherwise stay local. Web search is ALWAYS cloud (must be accurate
# and current) EXCEPT in apocalyptic/off_grid mode where local is all there is.
# ---------------------------------------------------------------------------
APOCALYPTIC = os.environ.get("CLAF_MODE", "hybrid") in ("off_grid", "local")
ROUTING_ORACLE: dict[str, str | None] = {
    "p1_open_tab": "local",  # browser action, local holds tab_create
    "p2_loop_continue": "local",  # mid-loop continuation stays local
    "p3_web_search": (
        "local" if APOCALYPTIC else "cloud"
    ),  # web search = cloud for accuracy/currency, local only off-grid
    "p4_analyze_tradeoff": "cloud",  # complex reasoning → escalate (too hard for local)
    "p5_write_memory": "local",  # mechanical file write — not too hard for local
    "p6_file_and_web": "local",  # mixed action, local multi-group
    "p7_buried_command": "local",  # trim/context test, routing incidental
    "p8_error_retry": "local",  # behavior test, stays local
    "p9_loop_at_cap": "local",  # termination test, local
}


# ---------------------------------------------------------------------------
# Tool factory — send a realistic slice of Claude Code's tool array so
# select_local_tools() has something real to group.
# ---------------------------------------------------------------------------
def _tool(name: str, props: dict, required: list[str], desc: str = "") -> dict:
    return {
        "name": name,
        "description": desc or name,
        "input_schema": {"type": "object", "properties": props, "required": required},
    }


BROWSER_TOOLS = [
    _tool("mcp__sensei__tab_create", {"url": {"type": "string"}}, [], "open a browser tab"),
    _tool("mcp__sensei__read_full", {}, [], "read full page DOM"),
    _tool("mcp__sensei__click", {"what": {"type": "string"}}, ["what"], "click element"),
    _tool(
        "mcp__sensei__fill",
        {"where": {"type": "string"}, "text": {"type": "string"}},
        ["where", "text"],
        "fill field",
    ),
    _tool("mcp__sensei__screenshot", {}, [], "screenshot the page"),
    _tool("mcp__sensei__browse", {"url": {"type": "string"}}, ["url"], "navigate to url"),
    _tool("mcp__sensei__search", {"query": {"type": "string"}}, ["query"], "web search"),
    _tool("mcp__sensei__scroll", {"direction": {"type": "string"}}, ["direction"], "scroll page"),
]
FS_TOOLS = [
    _tool("Read", {"file_path": {"type": "string"}}, ["file_path"], "read a file"),
    _tool("Bash", {"command": {"type": "string"}}, ["command"], "run a shell command"),
    _tool(
        "Write",
        {"file_path": {"type": "string"}, "content": {"type": "string"}},
        ["file_path", "content"],
        "write a file",
    ),
    _tool(
        "Edit",
        {
            "file_path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
        },
        ["file_path", "old_string", "new_string"],
        "edit a file",
    ),
    _tool("Grep", {"pattern": {"type": "string"}}, ["pattern"], "search file contents"),
]
TASK_TOOLS = [
    _tool("TaskList", {}, [], "list tasks"),
    _tool(
        "TaskCreate",
        {"subject": {"type": "string"}, "description": {"type": "string"}},
        ["subject", "description"],
        "create a task",
    ),
]
ALL_TOOLS = BROWSER_TOOLS + FS_TOOLS + TASK_TOOLS

SYSTEM = (
    "You are Claude Code. Identity: MCP. Act through tools, show evidence, "
    "never narrate a plan, never say you can't."
) * 1  # small stand-in system

GIVEUP = (
    "i cannot",
    "unable to",
    "i'll stop",
    "please try again",
    "i can't",
    "cannot access",
    "unable to connect",
    "is unavailable",
)
PLAN_PREAMBLE = ("i will now", "here's my plan", "here is my plan", "let me ", "i'd be happy to")

# ~5K of hook-style preamble to bury prompt 7's real command at the END.
HOOK_PREAMBLE = "[STANDING ORDERS] " + (
    "browser console_logs network_requests read_full "
    "identify page type pick tool one action screenshot confirm. " * 60
)


def _user(text):
    return {"role": "user", "content": text}


def _assist_tooluse(name, inp):
    return {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": f"toolu_{name}", "name": name, "input": inp}],
    }


def _tool_result(tid, text, is_error=False):
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": is_error}
        ],
    }


# ---------------------------------------------------------------------------
# Corpus — 9 live prompts (1 body each) + prompt 10 is a parser unit check.
# ---------------------------------------------------------------------------
def _loop_history(n: int) -> list:
    """n assistant tool_use turns + tool_results — drives the loop-cap path."""
    msgs = [_user("keep taking screenshots of the page until I say stop")]
    for i in range(n):
        msgs.append(_assist_tooluse("mcp__sensei__screenshot", {}))
        msgs.append(_tool_result(f"toolu_mcp__sensei__screenshot", f"screenshot {i} saved"))
    return msgs


CORPUS = [
    {
        "id": "p1_open_tab",
        "layer": "ROUTING+BEHAVIOR",
        "tools": ALL_TOOLS,
        "messages": [_user("open an mcp tab to kimi.com")],
        "expect_tool": "mcp__sensei__tab_create",
        "forbid": GIVEUP + PLAN_PREAMBLE,
    },
    {
        "id": "p2_loop_continue",
        "layer": "CAPABILITY",
        "tools": ALL_TOOLS,
        "messages": [
            _user("open a tab to example.com then screenshot it"),
            _assist_tooluse("mcp__sensei__tab_create", {"url": "https://example.com"}),
            _tool_result("toolu_mcp__sensei__tab_create", "tab opened, id=2"),
        ],
        "expect_tool": "mcp__sensei__screenshot",
        "forbid": GIVEUP,
    },
    {
        "id": "p3_web_search",
        "layer": "CAPABILITY",
        "tools": ALL_TOOLS,
        "messages": [_user("search the web for the qwen3.5 context window size")],
        "expect_tool": "mcp__sensei__search",
        "forbid": GIVEUP,
    },
    {
        "id": "p4_analyze_tradeoff",
        "layer": "ROUTING",
        "tools": ALL_TOOLS,
        "messages": [
            _user("analyze the trade-offs between QLoRA and full fine-tuning for a 9b model")
        ],
        "expect_tool": None,
        "forbid": (),
    },
    {
        "id": "p5_write_memory",
        "layer": "CAPABILITY",
        "tools": ALL_TOOLS,
        "messages": [_user("write a file ~/notes.txt that says the parity test passed")],
        "expect_tool": "Write",
        "forbid": GIVEUP + PLAN_PREAMBLE,
    },
    {
        "id": "p6_file_and_web",
        "layer": "CAPABILITY",
        "tools": ALL_TOOLS,
        "messages": [_user("create a file ~/out.txt then open the website example.com")],
        # multi-group: one filesystem tool (Write OR Bash OR Edit) AND one browser-open
        "expect_tool": None,
        "expect_any_groups": [
            ["Write", "Bash", "Edit"],
            ["mcp__sensei__tab_create", "mcp__sensei__browse"],
        ],
        "forbid": GIVEUP,
    },
    {
        "id": "p7_buried_command",
        "layer": "CONTEXT",
        "tools": ALL_TOOLS,
        "messages": [
            _user(HOOK_PREAMBLE + "\n\nACTUAL COMMAND: take a screenshot of the current page")
        ],
        "expect_tool": "mcp__sensei__screenshot",
        "forbid": GIVEUP,
    },
    {
        "id": "p8_error_retry",
        "layer": "BEHAVIOR",
        "tools": ALL_TOOLS,
        "messages": [
            _user("click the Submit button"),
            _assist_tooluse("mcp__sensei__click", {"what": "Submit"}),
            _tool_result("toolu_mcp__sensei__click", "Error: element not found", is_error=True),
        ],
        "expect_tool": None,
        "forbid": GIVEUP + ("i'll stop here",),
    },
    {
        "id": "p9_loop_at_cap",
        "layer": "TERMINATION",
        "tools": ALL_TOOLS,
        "messages": _loop_history(21),
        "expect_log": "loop_replan_injected",
        "expect_tool": None,
        "forbid": (),
    },
]

# ---------------------------------------------------------------------------
# Prompt 10 — deterministic parser check (no model). Tests every tool-call
# text format the local/cloud-backup models actually emit.
# ---------------------------------------------------------------------------
PARSER_CASES = [
    ("funccall_parens", 'tab_create(url="https://kimi.com")', "mcp__sensei__tab_create", True),
    (
        "function_xml",
        '<function=tab_create>{"url": "https://kimi.com"}</function>',
        "mcp__sensei__tab_create",
        True,
    ),
    ("directive_bash", "BASH: ls -la", "Bash", True),
    (
        "bare_kv_NO_PARENS",
        "tab_create url=https://kimi.com",
        "mcp__sensei__tab_create",
        True,
    ),  # the known gap
]


def post(body: dict, timeout=120) -> dict:
    req = urllib.request.Request(
        f"{ORCH}/v1/messages",
        method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def last_route_decision(since_pos: int) -> dict:
    """Read log bytes written after since_pos, return the last route_decision event."""
    if not LOG.exists():
        return {}
    out = {}
    with LOG.open("rb") as f:
        f.seek(since_pos)
        for raw in f.read().decode(errors="ignore").splitlines():
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            if ev.get("event") == "route_decision":
                out = ev
    return out


def log_has(since_pos: int, event: str) -> bool:
    if not LOG.exists():
        return False
    with LOG.open("rb") as f:
        f.seek(since_pos)
        for raw in f.read().decode(errors="ignore").splitlines():
            try:
                if json.loads(raw).get("event") == event:
                    return True
            except Exception:
                continue
    return False


def emitted_text_and_tools(resp: dict) -> tuple[str, list[str]]:
    txt, tools = [], []
    for b in resp.get("content", []) or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            txt.append(b.get("text", ""))
        elif b.get("type") == "tool_use":
            tools.append(b.get("name", ""))
    return " ".join(txt).lower(), tools


def run_live():
    results = []
    for c in CORPUS:
        pos = LOG.stat().st_size if LOG.exists() else 0
        body = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 512,
            "system": SYSTEM,
            "messages": c["messages"],
            "tools": c.get("tools"),
        }
        t0 = time.time()
        err = None
        try:
            resp = post(body)
        except Exception as e:
            resp, err = {}, str(e)
        dt = time.time() - t0
        rd = last_route_decision(pos)
        text, tools = emitted_text_and_tools(resp)

        oracle = ROUTING_ORACLE.get(c["id"])
        actual_route = "cloud" if rd.get("cloud_escalated") else "local"
        scores = {}
        # ROUTING
        if oracle is not None:
            scores["ROUTING"] = actual_route == oracle
        # CAPABILITY — expected tool present
        if c.get("expect_tool"):
            scores["CAPABILITY"] = c["expect_tool"] in tools
        if c.get("expect_groups"):
            scores["CAPABILITY"] = all(g in tools for g in c["expect_groups"])
        if c.get("expect_any_groups"):
            scores["CAPABILITY"] = all(
                any(g in tools for g in grp) for grp in c["expect_any_groups"]
            )
        # BEHAVIOR — no forbidden phrases
        if c.get("forbid"):
            scores["BEHAVIOR"] = not any(p in text for p in c["forbid"])
        # TERMINATION — expected log event
        if c.get("expect_log"):
            scores["TERMINATION"] = log_has(pos, c["expect_log"])

        results.append(
            {
                "id": c["id"],
                "layer": c["layer"],
                "route": actual_route,
                "oracle": oracle,
                "tools": tools,
                "dt": round(dt, 1),
                "err": err,
                "scores": scores,
                "text_head": text[:80],
            }
        )
    return results


def run_parser():
    from orchestrator import parse_directives_to_content

    rows = []
    for label, text, expect_name, should_parse in PARSER_CASES:
        blocks, used = parse_directives_to_content(text, ALL_TOOLS)
        got = [b.get("name") for b in blocks if b.get("type") == "tool_use"]
        ok = (expect_name in got) == should_parse
        rows.append(
            {
                "case": label,
                "parsed_tool_use": used,
                "got": got,
                "expected": expect_name,
                "PASS": ok,
            }
        )
    return rows


def main():
    print(f"== CLAF parity harness ==  orchestrator={ORCH}")
    print(f"   log={LOG}\n")
    live = run_live()
    print("LIVE PROMPTS")
    print(f"{'id':22} {'layer':16} {'route':6} {'oracle':6} {'dt':>5}  scores")
    layer_tally: dict[str, list[int]] = {}
    for r in live:
        sc = " ".join(f"{k}={'Y' if v else 'N'}" for k, v in r["scores"].items())
        for k, v in r["scores"].items():
            layer_tally.setdefault(k, [0, 0])
            layer_tally[k][0] += 1 if v else 0
            layer_tally[k][1] += 1
        flag = f"  ERR={r['err']}" if r["err"] else ""
        print(
            f"{r['id']:22} {r['layer']:16} {r['route']:6} {str(r['oracle']):6} "
            f"{r['dt']:>5}  {sc}{flag}"
        )
        print(f"   tools={r['tools']}  emitted='{r['text_head']}'")
    print("\nPARSER UNIT (prompt 10)")
    for row in run_parser():
        print(
            f"  [{'PASS' if row['PASS'] else 'FAIL'}] {row['case']:20} "
            f"got={row['got']} expected={row['expected']}"
        )
    print("\nLAYER SCORECARD")
    for layer, (hit, tot) in sorted(layer_tally.items()):
        print(f"  {layer:12} {hit}/{tot}")
    print("\nNOTE: routing oracle entries marked None are TODO(human) — fill ROUTING_ORACLE.")


if __name__ == "__main__":
    main()

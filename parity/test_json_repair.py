#!/usr/bin/env python3
"""CLAF malformed tool-JSON repair parity test (Session 7 C26).

Mirrors test_continuation_guard.py format. Exercises the aggressive repair
layer that lets small local models (hermes3:3b on CPU) emit usable tool calls
even when their JSON is syntactically garbage.

Run:  PYTHONPATH=/home/elijah/projects/claf python3 \
        ~/projects/claf/parity/test_json_repair.py

Pass criteria: all PASS, exit 0. Any FAIL = fix target for Mary reliability.
LAYER key:
  VALID       already-valid JSON passes through unchanged
  QUOTING     quoting mistakes (single quotes, unquoted keys, spaced keys)
  TRAILING    trailing commas / markdown fences
  SHORTHAND   string-argument expansions (Bash, Write, Read)
  SCHEMA      repaired calls missing required params are rejected
  BUG         literal shapes seen in production logs
"""

from __future__ import annotations
import json, os, sys, pathlib

CLAF = pathlib.Path.home() / "projects" / "claf"
_VENV_PYTHON = CLAF / ".venv" / "bin" / "python3"

sys.path.insert(0, str(CLAF))

try:
    from orchestrator import _repair_malformed_tool_json
except ImportError as _import_err:
    # Bare system python may lack fastapi/httpx. Try the project venv once.
    if _VENV_PYTHON.exists() and sys.executable != str(_VENV_PYTHON):
        os.execv(str(_VENV_PYTHON), [str(_VENV_PYTHON), __file__] + sys.argv[1:])
    sys.exit(f"IMPORT ERROR: {_import_err}\nRun with PYTHONPATH=/home/elijah/projects/claf")


# ── tool factory ────────────────────────────────────────────────────────────
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
    _tool("TaskCreate", {"subject": {"type": "string"}}, ["subject"]),
]

# ── corpus ──────────────────────────────────────────────────────────────────
# Each entry: id, layer, fn (callable → raises AssertionError on failure).
CORPUS: list[dict] = []


def _reg(id_, layer):
    def decorator(fn):
        CORPUS.append({"id": id_, "layer": layer, "fn": fn})
        return fn

    return decorator


def _first(repaired: list[dict]):
    assert repaired, "expected a repaired tool call, got none"
    return repaired[0]["function"]


def _arg(repaired: list[dict], key: str):
    fn = _first(repaired)
    assert key in fn["arguments"], f"missing {key} in {fn['arguments']}"
    return fn["arguments"][key]


# ── LAYER: VALID ───────────────────────────────────────────────────────────


@_reg("v1_valid_read", "VALID")
def _():
    text = json.dumps({"name": "Read", "arguments": {"file_path": "/tmp/x.txt"}})
    r = _repair_malformed_tool_json(text, TOOLS)
    assert _first(r)["name"] == "Read"
    assert _arg(r, "file_path") == "/tmp/x.txt"


# ── LAYER: QUOTING ──────────────────────────────────────────────────────────


@_reg("q1_unquoted_keys", "QUOTING")
def _():
    text = '{name: "Read", arguments: {file_path: "/tmp/x.txt"}}'
    r = _repair_malformed_tool_json(text, TOOLS)
    assert _first(r)["name"] == "Read"
    assert _arg(r, "file_path") == "/tmp/x.txt"


@_reg("q2_single_quoted", "QUOTING")
def _():
    text = "{'name': 'Read', 'arguments': {'file_path': '/tmp/x.txt'}}"
    r = _repair_malformed_tool_json(text, TOOLS)
    assert _first(r)["name"] == "Read"
    assert _arg(r, "file_path") == "/tmp/x.txt"


@_reg("q3_spaced_quoted_keys", "QUOTING")
def _():
    text = '{ " name": "Read", " arguments": {" file_path": "/tmp/x.txt"}}'
    r = _repair_malformed_tool_json(text, TOOLS)
    assert _first(r)["name"] == "Read"
    assert _arg(r, "file_path") == "/tmp/x.txt"


@_reg("q4_unclosed_key_quote", "QUOTING")
def _():
    # Literal Session 5 shape: quote opened before key, never closed before colon.
    text = '{"arguments": "/tmp/Claf-Mary_test/file_1.txt \'step 1\'", "name: "Write"}'
    r = _repair_malformed_tool_json(text, TOOLS)
    assert _first(r)["name"] == "Write"
    assert "file_path" in _first(r)["arguments"]
    assert "content" in _first(r)["arguments"]


# ── LAYER: TRAILING ─────────────────────────────────────────────────────────


@_reg("t1_trailing_comma", "TRAILING")
def _():
    text = '{"name": "Write", "arguments": {"file_path": "/tmp/x.txt", "content": "hi",},}'
    r = _repair_malformed_tool_json(text, TOOLS)
    assert _first(r)["name"] == "Write"
    assert _arg(r, "file_path") == "/tmp/x.txt"
    assert _arg(r, "content") == "hi"


@_reg("t2_json_fence", "TRAILING")
def _():
    text = '```json\n{"name": "Bash", "arguments": {"command": "echo hi"}}\n```'
    r = _repair_malformed_tool_json(text, TOOLS)
    assert _first(r)["name"] == "Bash"
    assert _arg(r, "command") == "echo hi"


# ── LAYER: SHORTHAND ────────────────────────────────────────────────────────


@_reg("s1_bash_string_arg", "SHORTHAND")
def _():
    text = '{"name": "Bash", "arguments": "echo hello"}'
    r = _repair_malformed_tool_json(text, TOOLS)
    assert _first(r)["name"] == "Bash"
    assert _arg(r, "command") == "echo hello"


@_reg("s2_read_string_arg", "SHORTHAND")
def _():
    text = '{"name": "Read", "arguments": "/tmp/x.txt"}'
    r = _repair_malformed_tool_json(text, TOOLS)
    assert _first(r)["name"] == "Read"
    assert _arg(r, "file_path") == "/tmp/x.txt"


@_reg("s3_write_path_content_shorthand", "SHORTHAND")
def _():
    text = '{"name": "Write", "arguments": "/tmp/x.txt hello world"}'
    r = _repair_malformed_tool_json(text, TOOLS)
    assert _first(r)["name"] == "Write"
    assert _arg(r, "file_path") == "/tmp/x.txt"
    assert _arg(r, "content") == "hello world"


@_reg("s4_write_python_dict_literal", "SHORTHAND")
def _():
    text = "{\"name\": \"Write\", \"arguments\": \"{'path': '/tmp/x.txt', 'content': 'hi'}\"}"
    r = _repair_malformed_tool_json(text, TOOLS)
    assert _first(r)["name"] == "Write"
    assert _arg(r, "file_path") == "/tmp/x.txt"
    assert _arg(r, "content") == "hi"


# ── LAYER: SCHEMA ───────────────────────────────────────────────────────────


@_reg("x1_missing_required_rejected", "SCHEMA")
def _():
    # Write missing content → should be rejected (not shipped client-side).
    text = '{"name": "Write", "arguments": {"file_path": "/tmp/x.txt"}}'
    r = _repair_malformed_tool_json(text, TOOLS)
    assert r == [], f"expected rejection, got {r}"


@_reg("x2_missing_taskcreate_subject_rejected", "SCHEMA")
def _():
    text = '{"name": "TaskCreate", "arguments": {}}'
    r = _repair_malformed_tool_json(text, TOOLS)
    assert r == [], f"expected rejection, got {r}"


# ── LAYER: BUG ──────────────────────────────────────────────────────────────


@_reg("b1_session5_literal", "BUG")
def _():
    # Exact Session 5 failure text seen on Mary.
    text = '{"arguments": "/tmp/Claf-Mary_test/file_1.txt \'step 1\'", "name: "Write"}'
    r = _repair_malformed_tool_json(text, TOOLS)
    assert _first(r)["name"] == "Write"
    assert "file_path" in _first(r)["arguments"]
    assert "content" in _first(r)["arguments"]


@_reg("b2_name_with_trailing_words", "BUG")
def _():
    text = '{"name": "Read the file", "arguments": {"file_path": "/tmp/x.txt"}}'
    r = _repair_malformed_tool_json(text, TOOLS)
    assert _first(r)["name"] == "Read"
    assert _arg(r, "file_path") == "/tmp/x.txt"


# ── runner ──────────────────────────────────────────────────────────────────
def main():
    passed = failed = 0
    by_layer: dict[str, list] = {}
    for case in CORPUS:
        layer = case["layer"]
        by_layer.setdefault(layer, [])
        try:
            case["fn"]()
            status = "PASS"
            passed += 1
        except AssertionError as e:
            status = f"FAIL: {e}"
            failed += 1
        except Exception as e:
            status = f"ERROR: {e}"
            failed += 1
        by_layer[layer].append((case["id"], status))

    for layer, results in by_layer.items():
        print(f"\n── {layer} ──")
        for id_, st in results:
            tag = "  PASS" if st == "PASS" else "  FAIL"
            print(f"{tag}  {id_}  {'' if st == 'PASS' else st}")

    total = len(CORPUS)
    print(f"\n══ {passed}/{total} passed  |  {failed} failures ══")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()

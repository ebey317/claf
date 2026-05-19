#!/usr/bin/env python3
"""CLAF orchestrator — Anthropic-skin / local-brain proxy.

Listens on http://localhost:8000/v1/messages, accepts Claude Code's
Anthropic-format requests, translates them to Ollama's chat format,
calls a local model (default qwen3-vl:2b), then wraps the response
back into the Anthropic message envelope.

v0 scope:
- Non-streaming responses only (Claude Code can be coaxed into non-stream).
- Text in / text out. Tool-use, vision, and cache_control blocks are
  flattened into a text approximation so a small local model has a
  fighting chance.
- One routing tier: every request → local Ollama. Escalation tiers
  (free APIs, paid Anthropic) are wired in stubs and OFF by default.

Launch:
    pip install -r requirements.txt
    python3 orchestrator.py
    # then in another terminal:
    bash launch.sh

Env knobs (all optional):
    CLAF_LOCAL_MODEL   default qwen3-vl:2b
    CLAF_OLLAMA_URL    default http://localhost:11434/api/chat
    CLAF_PORT          default 8000
    CLAF_LOG_FILE      default ~/projects/claf/orchestrator.log
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from claf_config import MODE, PROVIDERS, describe, select_provider


PORT = int(os.environ.get("CLAF_PORT", "8000"))
LOG_FILE = Path(os.environ.get("CLAF_LOG_FILE", str(Path.home() / "projects/claf/orchestrator.log")))
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# Convenience: the tier-0 (local) provider is the default target in v0.
_LOCAL = next(p for p in PROVIDERS if p.tier == 0)
LOCAL_MODEL = _LOCAL.model
OLLAMA_URL = _LOCAL.url

# Dual-local routing: pick a different Ollama model when the request contains
# image content. Default workhorse handles text/tool/code; vision model gets
# routed image-bearing requests. Set CLAF_VISION_MODEL to enable; absent →
# vision routes to the same workhorse (current single-model behavior).
VISION_MODEL = os.environ.get("CLAF_VISION_MODEL", "").strip() or None


def _request_has_image(body: dict) -> bool:
    """Return True if any message content block is an image."""
    for msg in body.get("messages", []) or []:
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image":
                    return True
    return False


def select_local_model(body: dict) -> str:
    """Choose which Ollama model to route to for this request.
    - Image present + CLAF_VISION_MODEL set → vision model
    - Otherwise → the configured local workhorse
    """
    if VISION_MODEL and _request_has_image(body):
        return VISION_MODEL
    return LOCAL_MODEL

app = FastAPI(title="CLAF orchestrator", version="0.4.0")


def log(event: str, **fields) -> None:
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def flatten_anthropic_content(content) -> str:
    """Claude content is either a string or a list of blocks (text / image / tool_use / tool_result).
    Flatten to a single text string. Image blocks are NOT inlined here — they're
    extracted separately via extract_anthropic_images() and passed to Ollama via
    the per-message 'images' array."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "tool_use":
            name = block.get("name", "?")
            inp = json.dumps(block.get("input", {}), indent=None)
            parts.append(f"[Tool call: {name}({inp})]")
        elif btype == "tool_result":
            tool_id = block.get("tool_use_id", "?")
            inner = block.get("content", "")
            if isinstance(inner, list):
                inner = "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in inner)
            parts.append(f"[Tool result for {tool_id}]:\n{inner}")
        elif btype == "image":
            # Image data forwarded separately via Ollama 'images' field.
            # Leave a brief marker in text so model can refer to it.
            parts.append("[image attached]")
        else:
            parts.append(f"[unknown block type={btype}]")
    return "\n".join(p for p in parts if p)


def extract_anthropic_images(content) -> list[str]:
    """Pull base64 image strings out of Anthropic content blocks. Ollama's
    chat API accepts these via the per-message 'images' array (raw base64).
    Returns a list of base64-encoded image strings; empty if none present."""
    if not isinstance(content, list):
        return []
    out: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "image":
            continue
        src = block.get("source", {}) or {}
        # Anthropic supports source.type = "base64" or "url". We only forward base64.
        if src.get("type") != "base64":
            continue
        data = src.get("data") or ""
        if data:
            out.append(data)
    return out


def flatten_system(system) -> str:
    if not system:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in system
        )
    return str(system)


def anthropic_to_ollama_messages(claude_messages: list) -> list[dict]:
    out: list[dict] = []
    for m in claude_messages:
        role = m.get("role", "user")
        if role not in ("user", "assistant"):
            role = "user"
        content = m.get("content", "")
        msg: dict = {"role": role, "content": flatten_anthropic_content(content)}
        images = extract_anthropic_images(content)
        if images:
            msg["images"] = images
        out.append(msg)
    return out


def ollama_chat(provider, messages: list[dict]) -> tuple[str, dict]:
    payload = {
        "model": provider.model,
        "messages": messages,
        "stream": False,
        # Thinking models (qwen3-vl, deepseek-r1, etc.) can burn most of their
        # budget on chain-of-thought; 4096 leaves room for both think + answer.
        "options": {"temperature": 0.1, "num_predict": 4096},
    }
    with httpx.Client(timeout=300.0) as client:
        r = client.post(provider.url, json=payload)
        r.raise_for_status()
        data = r.json()
    msg = data.get("message", {})
    text = msg.get("content", "")
    thinking = msg.get("thinking", "")
    # Defensive: if a thinking model burned its whole budget on think and
    # emitted empty content, surface that to Claude Code instead of silently
    # returning blank — otherwise the UI looks like the model failed.
    if not text and thinking:
        log("thinking_only_response", thinking_chars=len(thinking), model=provider.model)
        text = (
            "[thinking-only response — model spent its token budget on chain-of-thought "
            f"and emitted no answer. Last 240 chars of thinking: ...{thinking[-240:]}]"
        )
    usage = {
        "input_tokens": data.get("prompt_eval_count", 0),
        "output_tokens": data.get("eval_count", 0),
    }
    return text, usage


def openai_compat_chat(provider, messages: list[dict]) -> tuple[str, dict]:
    """OpenAI-compatible chat completions (Groq / Gemini / OpenRouter)."""
    key = os.environ.get(provider.env_key or "", "")
    if not key:
        raise RuntimeError(f"{provider.name}: env var {provider.env_key} not set")
    payload = {
        "model": provider.model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 4096,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    with httpx.Client(timeout=120.0) as client:
        r = client.post(provider.url, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
    text = data["choices"][0]["message"]["content"]
    usage = {
        "input_tokens": data.get("usage", {}).get("prompt_tokens", 0),
        "output_tokens": data.get("usage", {}).get("completion_tokens", 0),
    }
    return text, usage


def anthropic_direct_chat(provider, body: dict) -> tuple[str, dict]:
    """Pass-through to the real Anthropic API. Reuses the operator's existing
    Anthropic message body shape since Claude Code is already producing it."""
    key = os.environ.get(provider.env_key or "", "")
    if not key:
        raise RuntimeError(f"{provider.name}: env var {provider.env_key} not set")
    payload = dict(body)
    payload["model"] = provider.model
    payload["stream"] = False
    payload.setdefault("max_tokens", 4096)
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=300.0) as client:
        r = client.post(provider.url, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
    # Anthropic content is already a list of blocks; flatten text parts only.
    text_parts = [b.get("text", "") for b in data.get("content", []) if isinstance(b, dict) and b.get("type") == "text"]
    text = "".join(text_parts)
    usage = {
        "input_tokens": data.get("usage", {}).get("input_tokens", 0),
        "output_tokens": data.get("usage", {}).get("output_tokens", 0),
    }
    return text, usage


# Directive parsing: small local models emit tool calls as TEXT in several
# formats — directive-prefix ("READ:/path"), function-call ("Read(file_path=
# \"/path\")"), or both. Thinking models also wrap reasoning in <think>...
# </think> tags that need to be stripped before parsing. This layer normalizes
# all of that into Anthropic tool_use content blocks so Claude Code's native
# dispatcher fires the actual tool.

_THINK_BLOCK = re.compile(r"<think\b[^>]*>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_ORPHAN = re.compile(r"</?think\b[^>]*>", re.IGNORECASE)

# Directive-prefix patterns: TOOLNAME: argument-on-rest-of-line
_DIRECTIVE_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"^\s*READ\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE), "Read", "file_path"),
    (re.compile(r"^\s*RUN\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE), "Bash", "command"),
    (re.compile(r"^\s*BASH\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE), "Bash", "command"),
]

# Function-call style: ToolName(key="value", key2="multi\nline\ncontent")
# Regex alone can't reliably balance parens inside quoted strings spanning
# many lines, so we use the regex below ONLY to locate the start of a call,
# then walk the arg blob with a state machine in _find_funccalls().
_FUNCCALL_START = re.compile(
    r"(?<![A-Za-z_])([A-Z][A-Za-z_]+)\s*\(",
)


def _walk_balanced_args(text: str, start: int) -> tuple[str, int] | None:
    """Starting at the position of an open paren in `text`, walk the argument
    blob until the matching close paren, respecting quote state (so parens
    inside quoted strings don't count). Returns (arg_blob, end_index) where
    end_index points to the position AFTER the closing paren, or None if no
    balanced close was found within the text.
    """
    n = len(text)
    if start >= n or text[start] != "(":
        return None
    depth = 1
    i = start + 1
    arg_start = i
    while i < n:
        c = text[i]
        if c == "(":
            depth += 1
            i += 1
            continue
        if c == ")":
            depth -= 1
            if depth == 0:
                return text[arg_start:i], i + 1
            i += 1
            continue
        if c in ("'", '"'):
            # skip quoted string; honor backslash escapes
            quote = c
            i += 1
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        i += 1
    return None


def _find_funccalls(text: str) -> list[tuple[int, int, str, str]]:
    """Find all function-call style invocations in text. Returns a list of
    (start_index, end_index, name, raw_args) tuples. Uses balanced-paren
    walking so multi-line quoted content args are captured correctly.
    """
    out: list[tuple[int, int, str, str]] = []
    for m in _FUNCCALL_START.finditer(text):
        # position of '(' is m.end() - 1
        result = _walk_balanced_args(text, m.end() - 1)
        if result is None:
            continue
        args_raw, end_idx = result
        out.append((m.start(), end_idx, m.group(1), args_raw))
    return out

# Parse "key='value'" / 'key="value"' / 'key=123' inside a funccall arg string.
_KV_PATTERN = re.compile(
    r"""
    ([A-Za-z_][A-Za-z0-9_]*)         # key
    \s*=\s*
    (?:
      "((?:[^"\\]|\\.)*)"            # double-quoted value (group 2)
      |
      '((?:[^'\\]|\\.)*)'            # single-quoted value (group 3)
      |
      ([^,)\s]+)                      # bare value (group 4)
    )
    """,
    re.VERBOSE,
)


def _find_tool(available: list, name: str) -> dict | None:
    target = name.lower()
    for t in available or []:
        if isinstance(t, dict) and str(t.get("name", "")).lower() == target:
            return t
    return None


def _kv_args(raw: str) -> dict:
    out: dict = {}
    for m in _KV_PATTERN.finditer(raw or ""):
        key = m.group(1)
        val = m.group(2) if m.group(2) is not None else (m.group(3) if m.group(3) is not None else m.group(4))
        if val is None:
            continue
        # unescape backslash sequences for quoted values
        out[key] = val.encode().decode("unicode_escape", errors="ignore") if "\\" in val else val
    return out


def parse_directives_to_content(text: str, available_tools: list) -> tuple[list[dict], bool]:
    """Scan model text for tool calls in several formats and produce Anthropic
    content blocks. Strips <think>...</think> reasoning. Recognizes:
    - directive-prefix: READ:/path, RUN:cmd, BASH:cmd
    - function-call: Read(file_path="/path"), Bash(command="..."), etc.

    Returns (content_blocks, used_tool_use).
    """
    if not text:
        return [{"type": "text", "text": ""}], False

    # Strip thinking blocks before any matching — they often contain example
    # directives that the model is rehearsing, not actually emitting.
    text = _THINK_BLOCK.sub("", text)
    # Also strip orphan/unbalanced <think> or </think> tags that leaked through
    text = _THINK_ORPHAN.sub("", text)

    found: list[tuple[int, int, dict]] = []

    # Pass 1: directive-prefix patterns
    for pattern, tool_name, input_key in _DIRECTIVE_PATTERNS:
        tool = _find_tool(available_tools, tool_name)
        if not tool:
            continue
        for m in pattern.finditer(text):
            value = m.group(1).strip()
            if not value:
                continue
            found.append(
                (
                    m.start(),
                    m.end(),
                    {
                        "type": "tool_use",
                        "id": f"toolu_claf_{uuid.uuid4().hex[:24]}",
                        "name": tool["name"],
                        "input": {input_key: value},
                    },
                )
            )

    # Pass 2: function-call patterns via balanced-paren walker
    for start_idx, end_idx, name_raw, args_raw in _find_funccalls(text):
        tool = _find_tool(available_tools, name_raw)
        if not tool:
            continue
        kv = _kv_args(args_raw)
        if not kv:
            continue
        found.append(
            (
                start_idx,
                end_idx,
                {
                    "type": "tool_use",
                    "id": f"toolu_claf_{uuid.uuid4().hex[:24]}",
                    "name": tool["name"],
                    "input": kv,
                },
            )
        )

    if not found:
        return [{"type": "text", "text": text}], False

    # De-duplicate overlapping spans: if two patterns matched the same region,
    # keep the earliest start with the longest span.
    found.sort(key=lambda t: (t[0], -(t[1] - t[0])))
    deduped: list[tuple[int, int, dict]] = []
    for span in found:
        if deduped and span[0] < deduped[-1][1]:
            continue
        deduped.append(span)

    keep_parts: list[str] = []
    cursor = 0
    for start, end, _ in deduped:
        if start > cursor:
            keep_parts.append(text[cursor:start])
        cursor = end
    if cursor < len(text):
        keep_parts.append(text[cursor:])
    remaining = "".join(keep_parts).strip()

    blocks: list[dict] = []
    if remaining:
        blocks.append({"type": "text", "text": remaining})
    blocks.extend(block for _, _, block in deduped)
    return blocks, True


def wrap_anthropic_response(model_id: str, content_blocks: list, usage: dict, tool_use: bool) -> dict:
    return {
        "id": f"msg_claf_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model_id,
        "content": content_blocks,
        "stop_reason": "tool_use" if tool_use else "end_turn",
        "stop_sequence": None,
        "usage": usage,
    }


@app.get("/")
def root():
    return {"name": "claf-orchestrator", "version": "0.4.0", "local_model": LOCAL_MODEL, "mode": MODE}


@app.get("/healthz")
def healthz():
    """Self-check without firing an inference. Validates config; pings Ollama."""
    cfg = describe()
    ollama_reachable = False
    try:
        with httpx.Client(timeout=3.0) as c:
            r = c.get(OLLAMA_URL.replace("/api/chat", "/api/tags"))
            ollama_reachable = r.status_code == 200
    except Exception:
        pass
    return {"config": cfg, "ollama_reachable": ollama_reachable}


@app.get("/stats")
def stats():
    """Tally token usage from the log. Cloud tokens should stay 0 in off_grid mode —
    that's the line the operator wants to keep watching."""
    by_tier: dict[str, dict] = {}
    if LOG_FILE.exists():
        with LOG_FILE.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("event") != "response_out":
                    continue
                tier = str(e.get("tier", "?"))
                name = e.get("name", "?")
                slot = by_tier.setdefault(tier, {"name": name, "calls": 0, "input_tokens": 0, "output_tokens": 0})
                slot["calls"] += 1
                slot["input_tokens"] += int(e.get("input_tokens") or 0)
                slot["output_tokens"] += int(e.get("output_tokens") or 0)

    cloud_in = sum(s["input_tokens"] for t, s in by_tier.items() if t != "0")
    cloud_out = sum(s["output_tokens"] for t, s in by_tier.items() if t != "0")
    local_in = by_tier.get("0", {}).get("input_tokens", 0)
    local_out = by_tier.get("0", {}).get("output_tokens", 0)
    total_calls = sum(s["calls"] for s in by_tier.values())

    return {
        "mode": MODE,
        "by_tier": by_tier,
        "totals": {
            "total_calls": total_calls,
            "local_input_tokens": local_in,
            "local_output_tokens": local_out,
            "cloud_input_tokens": cloud_in,
            "cloud_output_tokens": cloud_out,
        },
        "happy_signal": cloud_in == 0 and cloud_out == 0,
    }


@app.get("/v1/models")
def list_models():
    """Claude Code probes this on startup. Return a single canonical entry."""
    return {
        "data": [
            {
                "id": LOCAL_MODEL,
                "type": "model",
                "display_name": f"local:{LOCAL_MODEL}",
                "created_at": "2026-05-19T00:00:00Z",
            }
        ]
    }


@app.post("/v1/messages")
async def messages(request: Request):
    body = await request.json()
    log(
        "request_in",
        model=body.get("model"),
        message_count=len(body.get("messages", [])),
        has_system=bool(body.get("system")),
        stream=body.get("stream", False),
    )

    if body.get("stream"):
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "type": "invalid_request_error",
                    "message": "CLAF v0 does not support streaming. Disable stream:true on the client.",
                }
            },
        )

    system_text = flatten_system(body.get("system"))
    messages = anthropic_to_ollama_messages(body.get("messages", []))
    if system_text:
        messages.insert(0, {"role": "system", "content": system_text})

    requested_model = body.get("model", "claude-sonnet-4-6")
    provider = select_provider(body)
    log("route_decision", mode=MODE, picked_tier=provider.tier, picked_name=provider.name, picked_model=provider.model)

    # Off-grid guardrail: even though claf_config prunes cloud tiers from
    # PROVIDERS in off_grid mode, refuse to dispatch a non-local kind here
    # as a defense-in-depth check. If this trips, something is misconfigured
    # — the request was about to leak off-box. Refuse loudly.
    if MODE == "off_grid" and provider.kind != "ollama":
        log("off_grid_lock", attempted=provider.name, kind=provider.kind)
        return JSONResponse(
            status_code=423,  # Locked
            content={
                "error": {
                    "type": "off_grid_lock",
                    "message": f"off_grid mode refuses non-local provider {provider.name}",
                }
            },
        )

    # Dual-local routing: if a vision model is configured AND request has
    # image content, override the model used for this single call. The
    # provider object stays the same (still tier 0 ollama); only the model
    # string is swapped before dispatch.
    routed_model = select_local_model(body) if provider.kind == "ollama" else provider.model
    if routed_model != provider.model:
        from dataclasses import replace as _replace
        log("dual_local_route", from_model=provider.model, to_model=routed_model, reason="image_in_request")
        provider = _replace(provider, model=routed_model)

    try:
        if provider.kind == "ollama":
            assistant_text, usage = ollama_chat(provider, messages)
        elif provider.kind == "openai_compat":
            assistant_text, usage = openai_compat_chat(provider, messages)
        elif provider.kind == "anthropic":
            # tier-4 pass-through uses the original Anthropic-shape body
            assistant_text, usage = anthropic_direct_chat(provider, body)
        else:
            raise RuntimeError(f"unknown provider kind: {provider.kind}")
    except Exception as e:
        log("provider_error", tier=provider.tier, name=provider.name, error=str(e))
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "type": "api_error",
                    "message": f"{provider.name} call failed: {e}",
                }
            },
        )

    content_blocks, tool_use = parse_directives_to_content(assistant_text, body.get("tools", []) or [])
    response = wrap_anthropic_response(requested_model, content_blocks, usage, tool_use)
    log(
        "response_out",
        tier=provider.tier,
        name=provider.name,
        out_chars=len(assistant_text),
        tool_use=tool_use,
        tool_use_count=sum(1 for b in content_blocks if b.get("type") == "tool_use"),
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
    )
    return response


if __name__ == "__main__":
    import uvicorn

    print(f"CLAF orchestrator → local model {LOCAL_MODEL} at {OLLAMA_URL}")
    print(f"Listening on http://127.0.0.1:{PORT}  (set ANTHROPIC_BASE_URL=http://localhost:{PORT}/v1)")
    print(f"Log: {LOG_FILE}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")

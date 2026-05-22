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

def get_page_content(browser_data):
    if len(browser_data) > 500:
        return browser_data[:500] + "... [truncated]"
    return browser_data

import re
import time
import uuid
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# Bootstrap: pull cloud-peer API keys from the operator's keystore at
# ~/.master_ai_keys (JSON or KEY=VALUE) and project them into env vars that claf_config
# reads. This keeps keys out of .env (and out of git) while still making
# every cloud peer reachable in hybrid/cloud modes. Local-only mode never
# touches this — claf_config doesn't read the env keys when MODE=local.
_KEYS_FILE = Path.home() / ".master_ai_keys"
_KEY_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "groq": "GROQ_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
}


def _normalize_bootstrap_key(raw_key: str) -> str:
    key = (raw_key or "").strip()
    if not key:
        return ""
    if key in _KEY_MAP.values():
        return key
    lower = key.lower()
    if lower in _KEY_MAP:
        return _KEY_MAP[lower]
    return key.upper()


def _load_keys_json_or_kv(path: Path) -> dict[str, str]:
    raw = path.read_text()
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            out: dict[str, str] = {}
            for k, v in parsed.items():
                k_norm = _normalize_bootstrap_key(str(k))
                v_norm = str(v).strip() if v is not None else ""
                if k_norm and v_norm:
                    out[k_norm] = v_norm
            return out
    except Exception:
        pass

    out: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k_norm = _normalize_bootstrap_key(k)
        v_norm = v.strip().strip('"').strip("'")
        if k_norm and v_norm:
            out[k_norm] = v_norm
    return out


if _KEYS_FILE.exists():
    try:
        _ks = _load_keys_json_or_kv(_KEYS_FILE)
        for _env in set(_KEY_MAP.values()):
            _v = (_ks.get(_env) or "").strip()
            if _v:
                os.environ.setdefault(_env, _v)
    except Exception:
        pass  # if keystore is malformed, fall back to whatever env already has

import sensei_supervisor as supervisor  # ReAct XML tool-call translator (off-grid MCP)
from claf_config import MODE, PROVIDERS, describe, select_provider, _is_hard_task


PORT = int(os.environ.get("CLAF_PORT", "8000"))
LOG_FILE = Path(os.environ.get("CLAF_LOG_FILE", str(Path.home() / "projects/claf/orchestrator.log")))
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# Convenience: the local provider is the default target when present. In
# `cloud` mode there is no local provider — leave these unset/None and let
# vision-routing and /v1/models handle the absence gracefully.
_LOCAL = next((p for p in PROVIDERS if p.pool == "local"), None)
LOCAL_MODEL = _LOCAL.model if _LOCAL else None
OLLAMA_URL = _LOCAL.url if _LOCAL else None

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


def ollama_chat(provider, messages: list[dict], tools: list[dict] | None = None) -> tuple[str, dict, bool]:
    import sensei_supervisor as supervisor
    num_ctx = int(os.environ.get("CLAF_OLLAMA_CTX", "2048"))
    user_msg = " ".join(m.get("content", "") for m in messages if m.get("role") == "user")
    mode = supervisor.sniff_mode(user_msg, bool(tools), False)
    sys_prompt = supervisor.build_system_prompt(mode, tools)
    if sys_prompt:
        has_system = any(m.get("role") == "system" for m in messages)
        if has_system:
            messages = [{"role": "system", "content": sys_prompt}] + [m for m in messages if m.get("role") != "system"]
        else:
            messages = [{"role": "system", "content": sys_prompt}] + messages
    messages_out = messages
    used_react = (mode == "work" and bool(tools))
    payload = {
        "model": provider.model,
        "messages": messages_out,
        "stream": False,
        "options": {
            "temperature": 0.1,
            "num_predict": min(num_ctx, 4096),
            "num_ctx": num_ctx,
        },
    }
    if used_react:
        payload["options"]["stop"] = ["</tool_call>"]
    with httpx.Client(timeout=300.0) as client:
        r = client.post(provider.url, json=payload)
        r.raise_for_status()
        data = r.json()
    msg = data.get("message", {})
    text = msg.get("content", "")
    thinking = msg.get("thinking", "")
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
    return text, usage, used_react


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


def anthropic_direct_chat(provider, body: dict) -> tuple[list, dict]:
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
    # Anthropic returns native content blocks (text + tool_use); pass them
    # through unchanged so Claude Code's native tool dispatcher fires.
    content_blocks = data.get("content", []) or []
    usage = {
        "input_tokens": data.get("usage", {}).get("input_tokens", 0),
        "output_tokens": data.get("usage", {}).get("output_tokens", 0),
    }
    return content_blocks, usage


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
    r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_.:-]*)\s*\(",
)

# Local models often emit plain command lines rather than strict function
# syntax. Accept these line forms and map them to available tool names.
_TOOL_LINE_PREFIX = re.compile(
    r"^\s*(?:call|use|invoke|run|execute)\s+([A-Za-z_][A-Za-z0-9_.:-]*)(?:\s+(.+?))?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_TOOL_LINE_DIRECTIVE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_.:-]*)\s*:\s*(.+?)\s*$",
    re.MULTILINE,
)
_TOOL_LINE_BARE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_.:-]*)\s*$",
    re.MULTILINE,
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
    target = (name or "").strip().strip("`'\"").rstrip(":").lower()
    if not target:
        return None
    for t in available or []:
        if not isinstance(t, dict):
            continue
        tool_name = str(t.get("name", "")).strip().lower()
        if not tool_name:
            continue
        aliases = {tool_name}
        if "__" in tool_name:
            parts = [p for p in tool_name.split("__") if p]
            if parts:
                aliases.add(parts[-1])
            if len(parts) >= 2:
                aliases.add(f"{parts[-2]}.{parts[-1]}")
        if "." in tool_name:
            aliases.add(tool_name.split(".")[-1])
        if ":" in tool_name:
            aliases.add(tool_name.split(":")[-1])
        if "-" in tool_name:
            aliases.add(tool_name.split("-")[-1])
        if target in aliases:
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


def _tool_schema(tool: dict) -> dict:
    return (tool or {}).get("inputSchema") or (tool or {}).get("input_schema") or {}


def _tool_requires_input(tool: dict) -> bool:
    req = _tool_schema(tool).get("required") or []
    return bool(req)


def _tool_input_from_text(tool: dict, raw: str | None) -> dict | None:
    schema = _tool_schema(tool)
    props = schema.get("properties") or {}
    required = list(schema.get("required") or [])
    arg_text = (raw or "").strip()
    if not props:
        return {}
    if arg_text.startswith("{") and arg_text.endswith("}"):
        try:
            obj = json.loads(arg_text)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    if not arg_text:
        return None if required else {}
    keys = list(props.keys())
    if len(keys) == 1:
        return {keys[0]: arg_text}
    if set(keys) == {"where", "text"}:
        if "|" in arg_text:
            where, txt = arg_text.split("|", 1)
            return {"where": where.strip(), "text": txt.strip()}
        if "=>" in arg_text:
            where, txt = arg_text.split("=>", 1)
            return {"where": where.strip(), "text": txt.strip()}
    if len(required) == 1:
        return {required[0]: arg_text}
    return None


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
        if not kv and _tool_requires_input(tool):
            continue
        found.append(
            (
                start_idx,
                end_idx,
                {
                    "type": "tool_use",
                    "id": f"toolu_claf_{uuid.uuid4().hex[:24]}",
                    "name": tool["name"],
                    "input": kv or {},
                },
            )
        )

    # Pass 3: line-command patterns (call/use/invoke TOOL, TOOL: args, TOOL)
    for pattern in (_TOOL_LINE_PREFIX, _TOOL_LINE_DIRECTIVE, _TOOL_LINE_BARE):
        for m in pattern.finditer(text):
            tool_name = m.group(1)
            arg_text = m.group(2) if m.lastindex and m.lastindex >= 2 else ""
            tool = _find_tool(available_tools, tool_name)
            if not tool:
                continue
            parsed_input = _tool_input_from_text(tool, arg_text)
            if parsed_input is None and _tool_requires_input(tool):
                continue
            found.append(
                (
                    m.start(),
                    m.end(),
                    {
                        "type": "tool_use",
                        "id": f"toolu_claf_{uuid.uuid4().hex[:24]}",
                        "name": tool["name"],
                        "input": parsed_input or {},
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
    """Self-check without firing an inference. Validates config; pings Ollama
    only when the active mode includes a local provider."""
    cfg = describe()
    ollama_reachable = None  # None = not applicable in this mode
    if OLLAMA_URL:
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
    """Claude Code probes this on startup to validate the model it wants to use
    actually exists. CLAF is a transparent proxy — whatever model ID Claude
    Code requests, we serve via local Ollama (or escalate per the tier ladder).
    Advertise the common Claude model IDs so Claude Code's startup validation
    passes, plus the actual local model name for direct callers."""
    common_claude_ids = [
        "claude-opus-4-7", "claude-opus-4-7[1m]",
        "claude-opus-4-6", "claude-opus-4-5",
        "claude-sonnet-4-7", "claude-sonnet-4-6", "claude-sonnet-4-5",
        "claude-haiku-4-5",
    ]
    seen: set[str] = set()
    data: list[dict] = []
    # In cloud mode LOCAL_MODEL is None; only advertise the common Claude IDs.
    candidate_ids = [m for m in [LOCAL_MODEL, *common_claude_ids] if m]
    routed_label = LOCAL_MODEL if LOCAL_MODEL else f"cloud-pool:{MODE}"
    for mid in candidate_ids:
        if mid in seen:
            continue
        seen.add(mid)
        data.append({
            "id": mid,
            "type": "model",
            "display_name": (
                f"local:{mid}" if mid == LOCAL_MODEL else f"claf:{mid} (→ {routed_label})"
            ),
            "created_at": "2026-05-19T00:00:00Z",
        })
    return {"data": data}


def _sse_events(response: dict):
    """Re-emit a completed Anthropic response as Anthropic-format SSE events.
    Takes the exact dict produced by wrap_anthropic_response so the streaming
    path stays byte-for-byte consistent with the non-streaming path — same
    msg_id, same content, same stop_reason, same usage. Fake-streamed: one
    text_delta per text block, one input_json_delta per tool_use block. No
    token-by-token generation — provider call still completes synchronously."""
    msg_id = response.get("id")
    model = response.get("model")
    content_blocks = response.get("content", []) or []
    usage = response.get("usage", {}) or {}
    stop_reason = response.get("stop_reason", "end_turn")
    stop_sequence = response.get("stop_sequence")

    def _event(name: str, data: dict) -> str:
        return f"event: {name}\ndata: {json.dumps(data)}\n\n"

    yield _event("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": 0,
            },
        },
    })
    yield _event("ping", {"type": "ping"})

    for i, block in enumerate(content_blocks):
        btype = block.get("type", "text")
        if btype == "text":
            yield _event("content_block_start", {
                "type": "content_block_start",
                "index": i,
                "content_block": {"type": "text", "text": ""},
            })
            yield _event("content_block_delta", {
                "type": "content_block_delta",
                "index": i,
                "delta": {"type": "text_delta", "text": block.get("text", "")},
            })
        elif btype == "tool_use":
            yield _event("content_block_start", {
                "type": "content_block_start",
                "index": i,
                "content_block": {
                    "type": "tool_use",
                    "id": block.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
                    "name": block.get("name", ""),
                    "input": {},
                },
            })
            yield _event("content_block_delta", {
                "type": "content_block_delta",
                "index": i,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(block.get("input", {}) or {}),
                },
            })
        yield _event("content_block_stop", {
            "type": "content_block_stop",
            "index": i,
        })

    yield _event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": stop_sequence},
        "usage": {"output_tokens": usage.get("output_tokens", 0)},
    })
    yield _event("message_stop", {"type": "message_stop"})


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

    system_text = flatten_system(body.get("system"))
    messages = anthropic_to_ollama_messages(body.get("messages", []))
    if system_text:
        messages.insert(0, {"role": "system", "content": system_text})

    requested_model = body.get("model", "claude-sonnet-4-6")

    # LOCAL mode + hard-task signal = explicit refusal. The operator picked
    # local for a reason; don't silently serve a request that needed cloud.
    if MODE == "local" and _is_hard_task(body):
        log("mode_lock", mode=MODE, reason="hard_task_in_local_mode")
        return JSONResponse(
            status_code=423,
            content={
                "error": {
                    "type": "mode_lock",
                    "message": "local mode cannot satisfy hard-task escalation (set CLAF_MODE=hybrid or cloud)",
                }
            },
        )

    provider = select_provider(body)

    # Routing-proof fields (verification-spec layer 3): make the reason for
    # this routing decision auditable in one log line. A consumer never needs
    # to read CLAF code to understand why their request went where it went.
    hard = _is_hard_task(body)
    if MODE == "local":
        route_reason = "local_mode_only"
        local_attempted = True
        cloud_escalated = False
    elif MODE == "cloud":
        route_reason = "cloud_mode_bypass_local"
        local_attempted = False
        cloud_escalated = True
    else:  # hybrid
        if hard:
            route_reason = "hybrid_hard_task_escalated"
            local_attempted = True   # local would have been chosen for routine
            cloud_escalated = True
        else:
            route_reason = "hybrid_routine_local"
            local_attempted = True
            cloud_escalated = False

    log(
        "route_decision",
        mode=MODE,
        provider=provider.name,
        pool=provider.pool,
        model=provider.model,
        route_reason=route_reason,
        local_attempted=local_attempted,
        cloud_escalated=cloud_escalated,
        # legacy fields kept for back-compat with /stats and existing scrapers:
        picked_tier=provider.tier,
        picked_name=provider.name,
        picked_model=provider.model,
    )

    # Mode lock — defense-in-depth. claf_config already prunes PROVIDERS by
    # mode at import time, but assert here that the chosen provider matches
    # mode constraints. If this trips, something is misconfigured and the
    # request was about to take a path the mode forbids. Refuse loudly (423).
    if MODE == "local" and provider.kind != "ollama":
        log("mode_lock", mode=MODE, attempted=provider.name, kind=provider.kind)
        return JSONResponse(
            status_code=423,
            content={
                "error": {
                    "type": "mode_lock",
                    "message": f"local mode refuses non-local provider {provider.name}",
                }
            },
        )
    if MODE == "cloud" and provider.kind == "ollama":
        log("mode_lock", mode=MODE, attempted=provider.name, kind=provider.kind)
        return JSONResponse(
            status_code=423,
            content={
                "error": {
                    "type": "mode_lock",
                    "message": f"cloud mode bypasses local provider {provider.name}",
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
            assistant_text, usage, used_react = ollama_chat(provider, messages, body.get("tools"))
            if used_react:
                content_blocks, tool_use = supervisor.parse_work_response(assistant_text, body.get("tools"))
            else:
                content_blocks, tool_use = parse_directives_to_content(assistant_text, body.get("tools", []) or [])
        elif provider.kind == "openai_compat":
            assistant_text, usage = openai_compat_chat(provider, messages)
            content_blocks, tool_use = parse_directives_to_content(assistant_text, body.get("tools", []) or [])
        elif provider.kind == "anthropic":
            # Anthropic returns native content blocks — no directive parsing.
            content_blocks, usage = anthropic_direct_chat(provider, body)
            tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content_blocks)
            assistant_text = "".join(b.get("text", "") for b in content_blocks if isinstance(b, dict) and b.get("type") == "text")
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
    if body.get("stream"):
        return StreamingResponse(
            _sse_events(response),
            media_type="text/event-stream",
        )
    return response


if __name__ == "__main__":
    import uvicorn

    if LOCAL_MODEL:
        print(f"CLAF orchestrator [SENSEI mode={MODE}] → local model {LOCAL_MODEL} at {OLLAMA_URL}")
    else:
        print(f"CLAF orchestrator [SENSEI mode={MODE}] → local provider bypassed (cloud-only)")
    print(f"Listening on http://127.0.0.1:{PORT}  (set ANTHROPIC_BASE_URL=http://localhost:{PORT})")
    print(f"Log: {LOG_FILE}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")


# ---------------------------------------------------------------------------
# tool_bridge integration helpers
# Added by install_tool_bridge.sh — do not edit by hand.
# ---------------------------------------------------------------------------
def claf_apply_tool_bridge(body: dict) -> tuple[dict, bool]:
    """If the request carries tools[], rewrite as ReAct for Ollama.
    Returns (ollama_request_body, used_react_bridge)."""
    if tool_bridge.has_tools(body):
        return tool_bridge.prepare_ollama_request(body), True
    return body, False


def claf_wrap_ollama_text_as_anthropic(raw_text: str, model: str,
                                        used_react: bool, input_tokens: int = 0,
                                        output_tokens: int = 0) -> dict:
    """Parse Ollama's raw assistant text and wrap as Anthropic /v1/messages
    response. If used_react is True, parse <tool_call> blocks into tool_use."""
    if used_react:
        blocks, stop = supervisor.parse_work_response(raw_text)
    else:
        blocks = [{"type": "text", "text": raw_text}]
        stop = "end_turn"
    return tool_bridge.build_anthropic_response(
        blocks, stop, model=model,
        input_tokens=input_tokens, output_tokens=output_tokens,
    )

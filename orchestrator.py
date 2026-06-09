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

import asyncio
import json
import os
import shlex

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
from fastapi.middleware.cors import CORSMiddleware
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
    # LLM cloud peers
    "anthropic": "ANTHROPIC_API_KEY",
    "groq": "GROQ_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    # Tool keys — projected so any tool reading these env vars gets them
    "FIRECRAWL_API_KEY": "FIRECRAWL_API_KEY",
    "SERPER_API_KEY": "SERPER_API_KEY",
}


# Source-name aliases. Lets the keys file use a name like ANTHROPIC_CONSOLE_KEY
# (explicit: this is the platform/Console account, NOT the Max subscription)
# while CLAF's own code keeps reading ANTHROPIC_API_KEY from env. Anyone who
# accidentally sources ~/.master_ai_keys into a shell only ends up with
# ANTHROPIC_CONSOLE_KEY exported, so Claude Code (which reads ANTHROPIC_API_KEY)
# cannot get crossed onto the Console account by accident.
_SOURCE_NAME_ALIASES = {
    "ANTHROPIC_CONSOLE_KEY": "ANTHROPIC_API_KEY",
}


def _normalize_bootstrap_key(raw_key: str) -> str:
    key = (raw_key or "").strip()
    if not key:
        return ""
    upper = key.upper()
    if upper in _SOURCE_NAME_ALIASES:
        return _SOURCE_NAME_ALIASES[upper]
    if key in _KEY_MAP.values():
        return key
    lower = key.lower()
    if lower in _KEY_MAP:
        return _KEY_MAP[lower]
    return upper


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
from claf_config import (
    MODE, PROVIDERS, describe, select_provider, _is_hard_task,
    _select_mode, TAP_TEMPLATES, detect_tap_intent, _flatten_prompt_text,
    next_cloud_peer, pick_cloud_peer,
)
import claf_throttle as throttle
import contextlib
import threading

try:
    from orchestrator_action_bridge import execute_actions_in_text
    HAS_ACTION_BRIDGE = True
except Exception:
    HAS_ACTION_BRIDGE = False

# Serialize cloud Ollama requests — concurrent calls to the SSH-tunneled cloud
# model cause 500s. One in-flight at a time; others queue and wait.
_OLLAMA_CLOUD_LOCK = threading.Lock()


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

# Browser UIs and extensions hit CLAF cross-origin, so preflight requests must succeed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["null"],
    allow_origin_regex=r"^(https?://(localhost|127\.0\.0\.1)(:\d+)?|chrome-extension://.*|moz-extension://.*)$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def log(event: str, **fields) -> None:
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# Operational charter prepended to every CLOUD peer's system prompt. Lives in
# cloud_charter.md so it's tunable without a code edit — change the file, the
# next request picks it up (mtime-cached, no restart needed). The inline fallback
# guarantees the hard bans survive even if the file is missing/unreadable.
_CHARTER_FILE = Path(__file__).parent / "cloud_charter.md"
_CHARTER_FALLBACK = (
    "OPERATIONAL CHARTER — you are MCP, the operator's execution agent. ACT, don't plan.\n"
    "- Operator says open/go/check/click/read/run X → call the tool NOW, no preamble.\n"
    "- After acting, show evidence (screenshot/output). 'Done' without proof = forbidden.\n"
    "- NEVER call AskUserQuestion. Ambiguous → make the reasonable call and proceed.\n"
    "- NEVER invoke a skill unless the user typed a literal /command. Never list skills.\n"
    "- A casual statement ('you can read X') → acknowledge one line, continue. No config editor.\n"
    "- Browser = sensei only: tab_create, then read_full, screenshot to confirm.\n"
    "- open tab → mcp__sensei__tab_create; screenshot → mcp__sensei__screenshot;\n"
    "  read page → mcp__sensei__read_full; task list → TaskList.\n\n"
)
_charter_cache: dict = {"mtime": None, "text": None}


def _load_cloud_charter() -> str:
    """Return the cloud operational charter, reloading from disk when the file
    changes. Falls back to the inline charter if the file is missing or empty."""
    try:
        st = _CHARTER_FILE.stat()
        if _charter_cache["mtime"] != st.st_mtime:
            txt = _CHARTER_FILE.read_text(encoding="utf-8").strip()
            if txt:
                _charter_cache["mtime"] = st.st_mtime
                _charter_cache["text"] = txt + "\n\n"
        if _charter_cache["text"]:
            return _charter_cache["text"]
    except (OSError, UnicodeDecodeError) as exc:
        log("charter_load_failed", error=str(exc), using="inline_fallback")
    return _CHARTER_FALLBACK


# FULL MEMORY PACK — the complete memory corpus (every *.md file, full bodies)
# injected into full_context peers so the hybrid KNOWS the operator the same way
# the primary agent does. The memory is what makes it personal; a subset is not
# enough. Loaded from the memory dir, cached by newest-mtime so edits to any
# memory file refresh the pack on the next request (no restart).
_MEMORY_DIR = Path(os.environ.get(
    "CLAF_MEMORY_DIR",
    str(Path.home() / ".claude/projects/-home-elijah/memory"),
))
_memory_cache: dict = {"sig": None, "text": None}


def _load_memory_pack() -> str:
    """Concatenate EVERY memory .md file (full body) into one pack, with a header
    per file so the model can cite which memory a fact came from. Cached by a
    signature of (file, mtime, size) across the dir so any edit refreshes it.
    Returns '' if the dir is missing/empty — never raises into the request path."""
    try:
        files = sorted(_MEMORY_DIR.glob("*.md"))
        if not files:
            return ""
        sig = tuple((f.name, f.stat().st_mtime, f.stat().st_size) for f in files)
        if _memory_cache["sig"] == sig and _memory_cache["text"]:
            return _memory_cache["text"]
        parts = [
            "===== FULL MEMORY CORPUS — this is what you KNOW about the operator. "
            "Treat every fact here as something you already know. =====\n"
        ]
        for f in files:
            try:
                body = f.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                continue
            if body:
                parts.append(f"\n----- memory: {f.name} -----\n{body}\n")
        parts.append("\n===== END MEMORY CORPUS =====\n\n")
        pack = "".join(parts)
        _memory_cache["sig"] = sig
        _memory_cache["text"] = pack
        log("memory_pack_built", files=len(files), chars=len(pack))
        return pack
    except OSError as exc:
        log("memory_pack_failed", error=str(exc))
        return ""


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


def _anthropic_tools_to_ollama(tools: list[dict]) -> list[dict]:
    """Convert Anthropic tool schema to Ollama/OpenAI native tool format.
    Ollama and OpenAI use the identical {type:function, function:{name,
    description, parameters}} schema, so this serves both paths."""
    out = []
    for t in (tools or []):
        schema = t.get("input_schema") or {}
        out.append({
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": schema,
            },
        })
    return out


def messages_from_anthropic(claude_messages: list, flavor: str = "openai") -> list[dict]:
    """Convert Anthropic messages to OpenAI- or Ollama-flavored messages,
    PRESERVING tool_use / tool_result structure so multi-turn tool loops work.

    The old flatten_anthropic_content() path turned past tool calls into prose
    ("[Tool call: ...]"), so on turn 2 of any agent loop the model lost the
    thread. This keeps the structure native.

    flavor="openai": assistant tool calls →
        {role:assistant, content:<text|None>, tool_calls:[{id, type:function,
         function:{name, arguments:<JSON string>}}]}
        tool results → {role:tool, tool_call_id, content}
        The assistant tool_calls[].id MUST equal the later tool message's
        tool_call_id — we preserve the Anthropic tool_use.id end-to-end.
    flavor="ollama": assistant tool calls → {role:assistant, content,
        tool_calls:[{function:{name, arguments:<dict>}}]}; tool results →
        {role:tool, content} (Ollama matches by order). Images preserved via
        the per-message 'images' array.
    """
    out: list[dict] = []
    for m in claude_messages:
        role = m.get("role", "user")
        content = m.get("content", "")

        if isinstance(content, str):
            r = role if role in ("user", "assistant") else "user"
            out.append({"role": r, "content": content})
            continue
        if not isinstance(content, list):
            out.append({"role": "user", "content": str(content)})
            continue

        text_parts: list[str] = []
        tool_use_blocks: list[dict] = []
        tool_result_blocks: list[dict] = []
        images: list[str] = []
        for b in content:
            if not isinstance(b, dict):
                text_parts.append(str(b))
                continue
            bt = b.get("type")
            if bt == "text":
                text_parts.append(b.get("text", ""))
            elif bt == "tool_use":
                tool_use_blocks.append(b)
            elif bt == "tool_result":
                tool_result_blocks.append(b)
            elif bt == "image":
                src = b.get("source", {}) or {}
                if src.get("type") == "base64" and src.get("data"):
                    images.append(src["data"])
        text_joined = "\n".join(p for p in text_parts if p)

        # tool_result blocks → tool-role messages (one per result).
        if tool_result_blocks:
            for tr in tool_result_blocks:
                inner = tr.get("content", "")
                if isinstance(inner, list):
                    inner = "\n".join(
                        x.get("text", "") if isinstance(x, dict) else str(x)
                        for x in inner
                    )
                if flavor == "openai":
                    out.append({"role": "tool",
                                "tool_call_id": tr.get("tool_use_id", ""),
                                "content": inner or ""})
                else:
                    out.append({"role": "tool", "content": inner or ""})
            if text_joined:
                tmsg = {"role": "user", "content": text_joined}
                if images and flavor == "ollama":
                    tmsg["images"] = images
                out.append(tmsg)
            continue

        # assistant message carrying tool_use → native tool_calls.
        if tool_use_blocks and role == "assistant":
            if flavor == "openai":
                tcs = [{"id": tu.get("id", ""),
                        "type": "function",
                        "function": {"name": tu.get("name", ""),
                                     "arguments": json.dumps(tu.get("input", {}))}}
                       for tu in tool_use_blocks]
                out.append({"role": "assistant",
                            "content": text_joined or None,
                            "tool_calls": tcs})
            else:
                tcs = [{"function": {"name": tu.get("name", ""),
                                     "arguments": tu.get("input", {})}}
                       for tu in tool_use_blocks]
                out.append({"role": "assistant",
                            "content": text_joined or "",
                            "tool_calls": tcs})
            continue

        # Plain message (text, maybe images).
        r = role if role in ("user", "assistant") else "user"
        pmsg = {"role": r, "content": text_joined}
        if images and flavor == "ollama":
            pmsg["images"] = images
        out.append(pmsg)
    return out


def openai_tool_calls_to_anthropic(message: dict) -> tuple[list[dict], bool]:
    """OpenAI choices[0].message → Anthropic content blocks.
    OpenAI returns tool-call arguments as a JSON STRING. Returns
    (content_blocks, tool_use_bool)."""
    blocks: list[dict] = []
    # Reasoning models (Cerebras gpt-oss-120b, zai-glm-4.7) put output in
    # message.reasoning when message.content is null. Fall back to it so these
    # providers return usable text instead of empty blocks.
    text = message.get("content") or message.get("reasoning") or ""
    if text:
        blocks.append({"type": "text", "text": text})
    tool_calls = message.get("tool_calls") or []
    for tc in tool_calls:
        fn = tc.get("function", {}) or {}
        raw_args = fn.get("arguments", "{}")
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
            except Exception:
                args = {}
        else:
            args = raw_args or {}
        blocks.append({
            "type": "tool_use",
            "id": tc.get("id") or f"toolu_claf_{uuid.uuid4().hex[:24]}",
            "name": fn.get("name", ""),
            "input": args,
        })
    if not blocks:
        blocks = [{"type": "text", "text": ""}]
    return blocks, bool(tool_calls)


def _fix_bash_args(args: dict) -> dict:
    """Small models hallucinate wrong parameter names for bash tool.
    Reconstruct a valid {command: ...} dict from whatever garbage they emitted."""
    if not isinstance(args, dict):
        return {"command": "echo 'no args'"}
    # 1. Model got it right — keep it.
    if isinstance(args.get("command"), str) and args["command"].strip():
        return args
    # 2. Look for any string value that looks like a shell command.
    for v in args.values():
        if isinstance(v, str) and v.strip() and any(c in v for c in " |/;-$*><&"):
            return {"command": v.strip()}
    # 3. Reconstruct from keys: flags (-l, --help) + paths (/tmp, ./foo).
    flags = [k for k in args if isinstance(k, str) and k.startswith("-") and args[k]]
    paths = [k for k in args if isinstance(k, str) and (k.startswith("/") or k.startswith(".") or k.startswith("~"))]
    if flags or paths:
        cmd = "ls " + " ".join(flags + paths)
        return {"command": cmd.strip()}
    # 4. Fallback — echo the broken args so the user/loop sees what happened.
    return {"command": f"echo 'broken tool args: {json.dumps(args)}'"}


def ollama_tool_calls_to_anthropic(message: dict) -> tuple[list[dict], bool]:
    """Ollama message → Anthropic content blocks.
    Ollama returns tool-call arguments as a DICT (not a string)."""
    blocks: list[dict] = []
    text = message.get("content") or ""
    if text:
        blocks.append({"type": "text", "text": text})
    tool_calls = message.get("tool_calls") or []
    for tc in tool_calls:
        fn = tc.get("function", {}) or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except Exception:
                args = {}
        elif not isinstance(args, dict):
            args = {}
        name = fn.get("name", "")
        # Fix broken tool calls from small local models:
        # - empty name + bash-looking args → assume bash
        # - wrong parameter names → reconstruct command
        if not name and isinstance(args, dict):
            if any(k.startswith("-") for k in args if isinstance(k, str)) or \
               any(k.startswith("/") for k in args if isinstance(k, str)):
                name = "bash"
        if name == "bash":
            args = _fix_bash_args(args)
        blocks.append({
            "type": "tool_use",
            "id": tc.get("id") or f"toolu_claf_{uuid.uuid4().hex[:24]}",
            "name": name,
            "input": args,
        })
    if not blocks:
        blocks = [{"type": "text", "text": ""}]
    return blocks, bool(tool_calls)


def ollama_chat(provider, messages: list[dict], tools: list[dict] | None = None,
                max_tokens: int | None = None) -> tuple[list[dict], dict, bool]:
    """Ollama chat — local AND cloud, unified. Sends native tools when present
    (both fast-agent:latest and qwen3-coder:480b-cloud support them) and reads
    tool_calls back as Anthropic tool_use blocks. No more XML round-trip, no
    ReAct system-prompt hack. Returns (content_blocks, usage, tool_use_bool)."""
    is_cloud = getattr(provider, "pool", "") == "cloud"
    if is_cloud:
        num_ctx = int(os.environ.get("CLAF_OLLAMA_CLOUD_CTX", "32768"))
        num_predict = max_tokens or 4096
    else:
        num_ctx = int(os.environ.get("CLAF_OLLAMA_CTX", "2048"))
        # CRITICAL for CPU-only local: honor the client's max_tokens. A hardcoded
        # num_predict=4096 means even "hi" can ramble to 4096 tokens; at ~5 tok/s
        # on CPU that's ~13 MINUTES of generation per call. We respect the
        # requested max_tokens and hard-cap it (env CLAF_LOCAL_MAX_PREDICT,
        # default 512) so no single local turn runs away.
        cap = int(os.environ.get("CLAF_LOCAL_MAX_PREDICT", "512"))
        num_predict = min(max_tokens or cap, cap)

    payload = {
        "model": provider.model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": num_predict, "num_ctx": num_ctx,
                    "num_thread": 8},
    }
    if tools:
        payload["tools"] = _anthropic_tools_to_ollama(tools)

    lock = _OLLAMA_CLOUD_LOCK if is_cloud else None
    with (lock if lock else contextlib.nullcontext()):
        with httpx.Client(timeout=300.0) as client:
            r = client.post(provider.url, json=payload)
            r.raise_for_status()
        data = r.json()

    msg = data.get("message", {}) or {}
    tool_calls = msg.get("tool_calls") or []
    content_text = msg.get("content", "") or ""
    thinking = msg.get("thinking", "") or ""

    # Thinking-mode fallback: some qwen builds emit tool calls as plain text
    # [Tool call: name({...})] instead of native tool_calls. Recover those.
    if not tool_calls and content_text:
        _tc_pat = re.compile(r'\[Tool [Cc]all:\s*(\w+)\((\{.*?\})\)\]', re.DOTALL)
        recovered = []
        for m2 in _tc_pat.finditer(content_text):
            try:
                recovered.append({"function": {"name": m2.group(1),
                                                "arguments": json.loads(m2.group(2))}})
            except Exception:
                pass
        if recovered:
            msg = dict(msg)
            msg["tool_calls"] = recovered
            tool_calls = recovered

    usage = {
        "input_tokens": data.get("prompt_eval_count", 0),
        "output_tokens": data.get("eval_count", 0),
    }

    if tool_calls:
        blocks, tool_use = ollama_tool_calls_to_anthropic(msg)
        return blocks, usage, tool_use

    # No tool calls — plain text (surface thinking-only so Claude Code isn't blank).
    if not content_text and thinking:
        log("thinking_only_response", thinking_chars=len(thinking), model=provider.model)
        content_text = (
            "[thinking-only response — model spent its token budget on chain-of-thought "
            f"and emitted no answer. Last 240 chars of thinking: ...{thinking[-240:]}]"
        )
    return [{"type": "text", "text": content_text}], usage, False


def openai_compat_chat(provider, messages: list[dict], tools: list[dict] | None = None) -> tuple[list[dict], dict, bool]:
    """OpenAI-compatible chat completions (Groq / Cerebras / Fireworks /
    OpenRouter). Sends native tools when present and reads tool_calls back as
    Anthropic tool_use blocks. Returns (content_blocks, usage, tool_use_bool)."""
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
    if tools:
        payload["tools"] = _anthropic_tools_to_ollama(tools)  # OpenAI == Ollama tool schema
        payload["tool_choice"] = "auto"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    # Body size is the 413 signal — log it so payload-too-large is diagnosable
    # without guessing. Cloud free tiers (groq ~30KB) reject oversized bodies.
    _body_bytes = len(json.dumps(payload).encode("utf-8"))
    log("cloud_request_size", provider=provider.name, body_bytes=_body_bytes,
        tool_count=len(tools) if tools else 0, msg_count=len(messages))
    with httpx.Client(timeout=120.0) as client:
        r = client.post(provider.url, json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
    message = data["choices"][0]["message"]
    blocks, tool_use = openai_tool_calls_to_anthropic(message)
    usage = {
        "input_tokens": data.get("usage", {}).get("prompt_tokens", 0),
        "output_tokens": data.get("usage", {}).get("completion_tokens", 0),
    }
    return blocks, usage, tool_use


_CLAF_INTERNAL_METADATA_KEYS = ("force_cloud", "emergency", "escalate")


def _sanitize_for_anthropic(body: dict) -> dict:
    """Strip CLAF-internal control fields from a request body before it's sent
    upstream to api.anthropic.com. Anthropic rejects unknown metadata keys
    with a 400, so anything we use to drive trickle routing must come out."""
    payload = dict(body)
    meta = payload.get("metadata")
    if isinstance(meta, dict):
        cleaned = {k: v for k, v in meta.items() if k not in _CLAF_INTERNAL_METADATA_KEYS}
        if cleaned:
            payload["metadata"] = cleaned
        else:
            payload.pop("metadata", None)
    return payload


def anthropic_direct_chat(provider, body: dict) -> tuple[list, dict]:
    """Pass-through to the real Anthropic API. Reuses the operator's existing
    Anthropic message body shape since Claude Code is already producing it."""
    key = os.environ.get(provider.env_key or "", "")
    if not key:
        raise RuntimeError(f"{provider.name}: env var {provider.env_key} not set")
    payload = _sanitize_for_anthropic(body)
    payload["model"] = provider.model
    payload["stream"] = False
    payload.setdefault("max_tokens", 4096)
    # Strip extended-thinking fields — adaptive/budget thinking requires specific
    # model support. If present and unsupported, Anthropic returns 400 and we
    # cascade through all cloud peers to slow local Qwen (~2-3 min/turn).
    payload.pop("thinking", None)
    payload.pop("output_config", None)
    headers = {
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
        # Enable prompt caching — saves 50-90% on repeated system/tool context
        "anthropic-beta": "prompt-caching-2024-07-31",
    }
    with httpx.Client(timeout=300.0) as client:
        r = client.post(provider.url, json=payload, headers=headers)
        if not r.is_success:
            # Parse Anthropic's structured error for better diagnostics
            anthropic_error = r.text[:500]
            try:
                err_json = r.json()
                if err_json.get("type") == "error" and "error" in err_json:
                    anthropic_error = err_json["error"].get("message", anthropic_error)
            except Exception:
                pass
            log("anthropic_direct_error", status=r.status_code,
                error_body=anthropic_error,
                payload_keys=list(payload.keys()),
                betas=payload.get("betas"),
                thinking_type=payload.get("thinking", {}).get("type") if isinstance(payload.get("thinking"), dict) else payload.get("thinking"),
                tool_count=len(payload.get("tools") or []))
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
        tool_name = str(t.get("name") or t.get("function", {}).get("name", "")).strip().lower()
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

    # Pass 4: markdown fenced code blocks → bash tool (code-as-tools path)
    # Models like Command-R emit bash/python in ```bash / ```python blocks.
    _MD_FENCE = re.compile(
        r"^\s*```(?:bash|sh|shell|zsh)\n(.*?)```\s*$",
        re.DOTALL | re.MULTILINE,
    )
    _PY_FENCE = re.compile(
        r"^\s*```(?:python|python3|py)\n(.*?)```\s*$",
        re.DOTALL | re.MULTILINE,
    )
    _bash_tool = _find_tool(available_tools, "bash")
    _bash_name = (_bash_tool.get("name") or _bash_tool.get("function", {}).get("name")) if _bash_tool else None
    if _bash_name:
        for m in _MD_FENCE.finditer(text):
            cmd = m.group(1).strip()
            if cmd:
                found.append(
                    (
                        m.start(),
                        m.end(),
                        {
                            "type": "tool_use",
                            "id": f"toolu_claf_{uuid.uuid4().hex[:24]}",
                            "name": _bash_name,
                            "input": {"command": cmd},
                        },
                    )
                )
        for m in _PY_FENCE.finditer(text):
            cmd = m.group(1).strip()
            if cmd:
                found.append(
                    (
                        m.start(),
                        m.end(),
                        {
                            "type": "tool_use",
                            "id": f"toolu_claf_{uuid.uuid4().hex[:24]}",
                            "name": _bash_name,
                            "input": {"command": f"python3 -c {shlex.quote(cmd)}"},
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



# ─── OpenAI-compatible conversion helpers ────────────────────────────────────

def _openai_tools_to_anthropic(tools: list) -> list:
    out = []
    for t in tools or []:
        fn = t.get("function", {}) or {}
        out.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {}) or {},
        })
    return out


def openai_messages_to_anthropic(messages: list) -> list:
    """Convert OpenAI-format messages to Anthropic Messages API format.

    Handles the structural differences:
    - assistant + tool_calls → content blocks with tool_use
    - tool role messages → user role with tool_result blocks
    - system role messages → skipped (handled as top-level system field)
    - plain user/assistant text → passed through
    """
    out: list[dict] = []
    i = 0
    while i < len(messages):
        m = messages[i]
        role = m.get("role", "user")
        content = m.get("content", "")

        if role == "system":
            i += 1
            continue

        if role == "tool":
            # Collect consecutive tool messages into one user message
            # with multiple tool_result blocks
            tool_results: list[dict] = []
            while i < len(messages) and messages[i].get("role") == "tool":
                tm = messages[i]
                tool_content = tm.get("content", "")
                if not isinstance(tool_content, str):
                    tool_content = json.dumps(tool_content)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tm.get("tool_call_id", ""),
                    "content": tool_content,
                })
                i += 1
            out.append({"role": "user", "content": tool_results})
            continue

        if role == "assistant":
            tool_calls = m.get("tool_calls")
            if tool_calls:
                blocks: list[dict] = []
                if content:
                    blocks.append({"type": "text", "text": str(content)})
                for tc in tool_calls:
                    fn = tc.get("function", {}) or {}
                    args = fn.get("arguments", "{}")
                    try:
                        input_dict = json.loads(args) if isinstance(args, str) else args
                    except json.JSONDecodeError:
                        input_dict = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": input_dict,
                    })
                out.append({"role": "assistant", "content": blocks})
                i += 1
                continue
            # Plain assistant message
            out.append({"role": "assistant", "content": content if isinstance(content, str) else str(content)})
            i += 1
            continue

        # Default: user message
        out.append({"role": "user", "content": content if isinstance(content, str) else str(content)})
        i += 1

    return out


def _openai_to_anthropic(body: dict) -> dict:
    result = {
        "model": body.get("model", "claude-sonnet-4-6"),
        "messages": openai_messages_to_anthropic(body.get("messages", [])),
        "tools": _openai_tools_to_anthropic(body.get("tools")),
        "max_tokens": body.get("max_tokens", 4096),
        "stream": body.get("stream", False),
        "system": body.get("system", ""),
    }
    # Pass through optional params Anthropic supports
    if "temperature" in body:
        result["temperature"] = body["temperature"]
    if "top_p" in body:
        result["top_p"] = body["top_p"]
    return result


def _anthropic_to_openai(anthropic_resp: dict, model: str) -> dict:
    content_blocks = anthropic_resp.get("content", []) or []
    text_parts = []
    tool_calls = []
    for b in content_blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            text_parts.append(b.get("text", ""))
        elif isinstance(b, dict) and b.get("type") == "tool_use":
            tool_calls.append({
                "id": b.get("id", ""),
                "type": "function",
                "function": {
                    "name": b.get("name", ""),
                    "arguments": json.dumps(b.get("input", {}) or {}),
                },
            })
    text = "".join(text_parts)
    stop_reason = anthropic_resp.get("stop_reason", "end_turn")
    finish_reason = "stop" if stop_reason == "end_turn" else "tool_calls" if stop_reason == "tool_use" else stop_reason
    usage = anthropic_resp.get("usage", {}) or {}
    return {
        "id": f"chatcmpl_{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": text or None,
                "tool_calls": tool_calls if tool_calls else None,
            },
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
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
        "throttle": throttle.snapshot(),
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


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    requested_model = body.get("model", "claude-sonnet-4-6")

    # Convert OpenAI format → Anthropic format
    anthropic_body = _openai_to_anthropic(body)

    # Select provider using the same logic
    provider = select_provider(anthropic_body)

    # Build messages
    system_text = flatten_system(anthropic_body.get("system"))
    _msgs = messages_from_anthropic(anthropic_body.get("messages", []), flavor="ollama" if provider.kind == "ollama" else "openai")
    if system_text:
        _msgs.insert(0, {"role": "system", "content": system_text})

    tools = anthropic_body.get("tools")
    max_tokens = anthropic_body.get("max_tokens", 1024)

    try:
        if provider.kind == "ollama":
            _blocks, _usage, _tool_use = ollama_chat(provider, _msgs, tools, max_tokens=max_tokens)
        elif provider.kind == "openai_compat":
            _blocks, _usage, _tool_use = openai_compat_chat(provider, _msgs, tools)
        elif provider.kind == "anthropic":
            _blocks, _usage = anthropic_direct_chat(provider, anthropic_body)
            _tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in _blocks)
        else:
            raise RuntimeError(f"unknown provider kind: {provider.kind}")

        # Action bridge: auto-execute directives in text responses
        if HAS_ACTION_BRIDGE and not _tool_use:
            _text = "".join(b.get("text", "") for b in _blocks if isinstance(b, dict) and b.get("type") == "text")
            if _text:
                _new_text = execute_actions_in_text(_text)
                if _new_text != _text:
                    # Replace the text block with the augmented version
                    _blocks = [{"type": "text", "text": _new_text}] + [b for b in _blocks if not (isinstance(b, dict) and b.get("type") == "text")]

        anthropic_resp = wrap_anthropic_response(requested_model, _blocks, _usage, _tool_use)
    except Exception as e:
        log("openai_endpoint_error", error=str(e))
        return JSONResponse(
            status_code=502,
            content={"error": {"type": "api_error", "message": str(e)}},
        )

    openai_resp = _anthropic_to_openai(anthropic_resp, requested_model)
    return openai_resp

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


# ---------------------------------------------------------------------------
# Tap mode helpers — extract a fenced code block from the local draft, send
# JUST that snippet to a cheap cloud peer with an intent-specific template,
# splice the polished version back in by index.
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+\-]*\n(.*?)\n```", re.DOTALL)


def _extract_code_block(text: str):
    """Return (snippet, start, end) for the longest fenced block, or None."""
    matches = list(_FENCE_RE.finditer(text or ""))
    if not matches:
        return None
    longest = max(matches, key=lambda m: len(m.group(1)))
    return longest.group(1), longest.start(), longest.end()


def _do_tap_polish(body: dict, draft_text: str) -> str:
    """Polish the largest code block in draft_text via a cheap cloud peer.
    Returns draft_text unchanged if no snippet found or polish fails."""
    ext = _extract_code_block(draft_text)
    if not ext:
        log("tap_no_snippet", draft_chars=len(draft_text or ""))
        return draft_text
    snippet, start, end = ext

    prompt_text = _flatten_prompt_text(body)
    intent = detect_tap_intent(prompt_text)
    template = TAP_TEMPLATES.get(intent, TAP_TEMPLATES["generic"])
    polish_prompt = template.format(snippet=f"INTENT: {prompt_text[:300]}\n\nDRAFT:\n```\n{snippet}\n```")

    try:
        # Tap polish NEVER uses direct Anthropic billing (kind="anthropic").
        # openai_compat only: groq(2), cerebras(3), openrouter(4),
        # ollama-cloud-coder(5), deepseek(6), openai(7). Console key stays untouched.
        peer = pick_cloud_peer(
            prefer_tiers=(2, 4, 5, 6, 7, 1),
            allowed_kinds=("openai_compat",),
        )
        if peer is None:
            log("tap_no_cloud_peer")
            return draft_text
        polish_msgs = [{"role": "user", "content": polish_prompt}]
        if peer.kind == "openai_compat":
            polished_text, _usage = openai_compat_chat(peer, polish_msgs)
        elif peer.kind == "anthropic":
            polish_body = {
                "model": peer.model,
                "max_tokens": 800,
                "messages": polish_msgs,
            }
            polished_blocks, _usage = anthropic_direct_chat(peer, polish_body)
            polished_text = "".join(
                b.get("text", "") for b in polished_blocks
                if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            log("tap_unknown_peer_kind", kind=peer.kind)
            return draft_text
    except Exception as e:
        log("tap_polish_failed", error=str(e))
        return draft_text

    polished_ext = _extract_code_block(polished_text)
    polished_block = polished_ext[0] if polished_ext else polished_text.strip()
    new_text = draft_text[:start] + f"```\n{polished_block}\n```" + draft_text[end:]
    log("tap_polish_ok", intent=intent, peer=peer.name, before=len(snippet), after=len(polished_block))
    return new_text


# ---------------------------------------------------------------------------
# Local prompt trimming — CPU-bound local models spend most of their wall-clock
# on PROMPT EVAL, not generation. A full Claude Code system prompt (CLAUDE.md +
# MEMORY + hook injections) is thousands of tokens; on a CPU-only 3B model that
# is 1-2 minutes of eval before the first output token. The terminal feels
# instant because `ollama run` sends almost nothing. We close that gap by
# trimming the system prompt and history for LOCAL-pool calls only. Tool
# definitions are passed separately (native tool-calling), so trimming the
# system text does NOT remove agent/tool capability — qwen2.5 calls tools from
# the `tools` param via its built-in chat template.
# Tunables (env): CLAF_LOCAL_SYS_MAX_CHARS (default 1500),
#                 CLAF_LOCAL_MAX_MSGS (default 10).
# Set CLAF_LOCAL_TRIM=0 to disable entirely.
# ---------------------------------------------------------------------------
_LOCAL_CHARTER = (
    "ACT by writing code. You have bash and filesystem. "
    "Build other tools on the fly: curl for web, python3 for logic, cat/grep for files. "
    "Chain steps in one script. Zero preamble. No <think> tags. Just write the command.\n\n"
    "--- PATTERNS (copy exact syntax) ---\n"
    "Browser: python3 -c \"from playwright.sync_api import sync_playwright; "
    "p=sync_playwright().start(); browser=p.chromium.launch(); page=browser.new_page(); "
    "page.goto('URL'); print(page.title()); browser.close(); p.stop()\"\n"
    "HTTP: curl -sL 'URL' | head -n 20\n"
    "File write: echo 'DATA' > PATH\n"
    "File read: cat PATH\n"
    "--- END PATTERNS ---\n\n"
)


def _trim_for_local(system_text: str, msgs: list[dict]) -> tuple[str, list[dict], dict]:
    info = {"trimmed": False}
    if os.environ.get("CLAF_LOCAL_TRIM", "1") == "0":
        return _LOCAL_CHARTER + (system_text or ""), msgs, info
    max_sys = int(os.environ.get("CLAF_LOCAL_SYS_MAX_CHARS", "1500"))
    max_msgs = int(os.environ.get("CLAF_LOCAL_MAX_MSGS", "10"))
    sys_before = len(system_text or "")
    msgs_before = len(msgs)

    # Prepend local charter so critical instructions survive trimming
    charter_len = len(_LOCAL_CHARTER)
    budget = max_sys - charter_len
    if budget < 200:
        budget = 200  # minimum viable context

    if system_text and len(system_text) > budget:
        system_text = _LOCAL_CHARTER + system_text[:budget].rstrip() + "\n[…system prompt trimmed for local speed…]"
    else:
        system_text = _LOCAL_CHARTER + (system_text or "")

    if len(msgs) > max_msgs:
        msgs = msgs[-max_msgs:]
        # Don't start the window mid tool-exchange — a dangling tool_result with
        # no matching tool_use confuses the model. Drop leading non-user turns.
        while msgs and msgs[0].get("role") != "user":
            msgs = msgs[1:]

    if len(system_text or "") != sys_before or len(msgs) != msgs_before:
        info = {"trimmed": True, "sys_chars_before": sys_before,
                "sys_chars_after": len(system_text or ""),
                "msgs_before": msgs_before, "msgs_after": len(msgs)}
    return system_text, msgs, info


@app.post("/v1/messages")
async def messages(request: Request):
    body = await request.json()
    _sys_probe = flatten_system(body.get("system"))
    _msgs_probe = body.get("messages", [])
    _last_user = ""
    for _m in reversed(_msgs_probe):
        if _m.get("role") == "user":
            _c = _m.get("content")
            _last_user = _c if isinstance(_c, str) else json.dumps(_c)
            break
    log(
        "request_in",
        model=body.get("model"),
        message_count=len(_msgs_probe),
        has_system=bool(body.get("system")),
        stream=body.get("stream", False),
        # Diagnostics: is the native memory + hook content actually arriving?
        sys_chars=len(_sys_probe),
        sys_has_claude_md=("STANDING ORDERS" in _sys_probe or "STARTUP ROUTINE" in _sys_probe),
        sys_has_memory=("MEMORY.md" in _sys_probe or "auto-memory" in _sys_probe or "feedback_" in _sys_probe),
        prompt_has_retry_hook=("RETRY_SCHEMA" in _last_user),
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

    # Three-mode trickle routing (Local / Tap / Flash). Only meaningful in
    # hybrid — local mode refuses cloud regardless, cloud mode goes cloud
    # regardless. Caller can force flash via metadata.force_cloud=true; with
    # metadata.emergency=true that draws from the daily emergency pool instead
    # of the hourly flash cap.
    trickle_mode = "local"
    trickle_reservation: str | None = None
    trickle_scores: dict = {}
    trickle_degrade_note = ""
    if MODE == "hybrid":
        desired, trickle_scores = _select_mode(body)
        meta = body.get("metadata") or {}
        emergency = bool(meta.get("emergency"))
        if desired == "flash":
            trickle_reservation = throttle.reserve(5000, "flash", emergency=emergency)
            if trickle_reservation:
                trickle_mode = "flash"
                # Try ALL enabled cloud peers. If CLAF_PREFERRED_CLOUD is set
                # (e.g. "anthropic"), that provider's tier is tried first;
                # otherwise normal tier ordering (lowest tier wins).
                _pref_name = os.environ.get("CLAF_PREFERRED_CLOUD", "").strip().lower()
                _pref_tiers: tuple[int, ...] | None = None
                if _pref_name:
                    _pref_tiers = tuple(
                        p.tier for p in PROVIDERS
                        if p.pool == "cloud" and p.enabled and p.name.lower() == _pref_name
                    )
                provider = pick_cloud_peer(prefer_tiers=_pref_tiers if _pref_tiers else None)
                if provider is None:
                    throttle.refund(trickle_reservation)
                    trickle_reservation = None
                    trickle_mode = "local"
                    trickle_degrade_note = throttle.degrade_message("flash")
                    log("trickle_flash_degraded_to_local", scores=trickle_scores, reason="no_cloud_peer")
                else:
                    log("trickle_flash_approved", reservation=trickle_reservation,
                        emergency=emergency, scores=trickle_scores, provider=provider.name)
            else:
                tap_res = throttle.reserve(800, "tap")
                if tap_res:
                    trickle_mode = "tap"
                    trickle_reservation = tap_res
                    trickle_degrade_note = throttle.degrade_message("flash")
                    log("trickle_flash_degraded_to_tap", reservation=tap_res, scores=trickle_scores)
                else:
                    trickle_mode = "local"
                    trickle_degrade_note = throttle.degrade_message("flash")
                    log("trickle_flash_degraded_to_local", scores=trickle_scores)
        elif desired == "tap":
            trickle_reservation = throttle.reserve(800, "tap")
            if trickle_reservation:
                trickle_mode = "tap"
                log("trickle_tap_approved", reservation=trickle_reservation, scores=trickle_scores)
            else:
                trickle_mode = "local"
                trickle_degrade_note = throttle.degrade_message("tap")
                log("trickle_tap_degraded_to_local", scores=trickle_scores)
        # else: desired == "local" — no reservation, no provider override
        if trickle_mode == "local":
            # Force local provider for the actual call; select_provider may
            # have already picked local for routine traffic, but make sure.
            local_only = [p for p in PROVIDERS if p.pool == "local" and p.enabled]
            if local_only:
                provider = local_only[0]

    # Mode lock — defense-in-depth. claf_config already prunes PROVIDERS by
    # mode at import time, but assert here that the chosen provider matches
    # mode constraints. If this trips, something is misconfigured and the
    # request was about to take a path the mode forbids. Refuse loudly (423).
    # Mode lock keyed on POOL, not KIND. Ollama-Cloud peers are kind="ollama"
    # but pool="cloud" — they belong with the cloud peers, not with the
    # truly-local Ollama instance. So local mode allows only pool="local"
    # and cloud mode allows only pool="cloud".
    if MODE == "local" and provider.pool != "local":
        log("mode_lock", mode=MODE, attempted=provider.name, pool=provider.pool)
        return JSONResponse(
            status_code=423,
            content={
                "error": {
                    "type": "mode_lock",
                    "message": f"local mode refuses non-local provider {provider.name}",
                }
            },
        )
    if MODE == "cloud" and provider.pool == "local":
        log("mode_lock", mode=MODE, attempted=provider.name, pool=provider.pool)
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
    # GUARD: only apply to local-pool ollama peers — cloud-pool ollama peers
    # (e.g. ollama-cloud-coder) carry their own model name and must not be
    # overridden with the local workhorse model.
    routed_model = select_local_model(body) if (provider.kind == "ollama" and provider.pool == "local") else provider.model
    if routed_model != provider.model:
        from dataclasses import replace as _replace
        log("dual_local_route", from_model=provider.model, to_model=routed_model, reason="image_in_request")
        provider = _replace(provider, model=routed_model)

    # Routing-proof fields (verification-spec layer 3): emit the actual
    # effective provider/model after any single-call model override so the
    # watch surface answers "who took the call?" unambiguously.
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
        if trickle_mode == "flash":
            route_reason = "hybrid_flash_cloud"
            local_attempted = False
            cloud_escalated = True
        elif trickle_mode == "tap":
            route_reason = "hybrid_tap_local_then_cloud_polish"
            local_attempted = True
            cloud_escalated = True
        elif hard:
            route_reason = "hybrid_hard_task_escalated"
            local_attempted = True
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
        kind=provider.kind,
        model=provider.model,
        env_key=provider.env_key or "—",
        trickle_mode=trickle_mode,
        route_reason=route_reason,
        local_attempted=local_attempted,
        cloud_escalated=cloud_escalated,
        selected_display=f"{provider.name} -> {provider.model}",
        # legacy fields kept for back-compat with /stats and existing scrapers:
        picked_tier=provider.tier,
        picked_name=provider.name,
        picked_model=provider.model,
    )

    # Rate-limit fallback: if the selected cloud peer returns 429, walk the
    # tier list (skipping failed providers) until one succeeds or the pool
    # is exhausted. Local providers are never in the fallback loop — they
    # don't rate-limit in the same way and a local 429 would be a bug.
    _rate_limit_failed: set[str] = set()
    content_blocks: list[dict] = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    assistant_text = ""
    tool_use = False

    def _dispatch_provider(p):
        """Call the right backend for `p`. Returns (content_blocks, usage, used_react, assistant_text, tool_use).

        Tool-capable paths (ollama + openai_compat) build STRUCTURED history via
        messages_from_anthropic so tool_use/tool_result survive across turns,
        and read native tool_calls back as Anthropic tool_use blocks. The
        directive-scraper is kept only as a fallback for models that emit prose
        tool calls instead of native ones."""
        _tools = body.get("tools")
        if p.kind == "ollama":
            _msgs = messages_from_anthropic(body.get("messages", []), flavor="ollama")
            _sys = system_text
            _tools_eff = _tools
            if p.pool == "local":
                _sys, _msgs, _trim_info = _trim_for_local(_sys, _msgs)
                if _trim_info.get("trimmed"):
                    log("local_prompt_trimmed", **_trim_info)
                # CODE-AS-TOOLS: Instead of giving the local model a buffet of
                # pre-built tools it can't juggle, we only pass bash + filesystem.
                # The model WRITES CODE to create whatever other tools it needs
                # on the fly (curl for web search, python3 for complex logic, etc.)
                # This eliminates tool-overload hallucinations on small models.
                # Override: set CLAF_LOCAL_CODE_TOOLS=0 to revert to old count-based capping.
                if os.environ.get("CLAF_LOCAL_CODE_TOOLS", "1") == "1":
                    _allowed = {"bash", "filesystem"}
                    if _tools:
                        _before = len(_tools)
                        def _tool_name(t):
                            return t.get("name") or t.get("function", {}).get("name") or ""
                        _tools_eff = [t for t in _tools if _tool_name(t) in _allowed]
                        if len(_tools_eff) != _before:
                            log("local_tools_code_as_tools", tools_before=_before, tools_after=len(_tools_eff),
                                kept=[_tool_name(t) for t in (_tools_eff or [])])
                else:
                    # Legacy count-based capping
                    _max_tools = int(os.environ.get("CLAF_LOCAL_MAX_TOOLS", "0"))
                    if _tools and len(_tools) > _max_tools:
                        log("local_tools_capped", tools_before=len(_tools), tools_after=_max_tools)
                        _tools_eff = _tools[:_max_tools] if _max_tools > 0 else None
            if _sys:
                _msgs.insert(0, {"role": "system", "content": _sys})
            _blocks, _usage, _tool_use = ollama_chat(p, _msgs, _tools_eff, max_tokens=body.get("max_tokens"))
        elif p.kind == "openai_compat":
            _msgs = messages_from_anthropic(body.get("messages", []), flavor="openai")
            # Trim system + history for cloud peers too. The full Claude Code
            # context (CLAUDE.md + memory injections) is ~60K tokens per call —
            # that burned through Fireworks' free monthly allocation in 24 calls.
            # Cloud peers get a higher cap than local (4000 chars vs 1500) so
            # they retain enough context to be useful. Controlled by env vars:
            # CLAF_CLOUD_SYS_MAX_CHARS (default 4000), CLAF_CLOUD_MAX_MSGS (default 20).
            # Cloud preamble: always prepended before the (possibly trimmed) full
            # system prompt. Ensures the operational charter survives aggressive
            # sys-prompt truncation. Groq's 1500-char cap cuts into CLAUDE.md before
            # the operating rules are reached — without this the model falls back to
            # listing skills / asking questions instead of acting. Loaded from
            # cloud_charter.md (tunable without code edits); inline fallback below.
            # The charter is PROTECTED — it carries the operating rules and must
            # never be truncated. Only the appended Claude Code system_text is
            # trimmable. We trim system_text to (cap - charter_len) so the full
            # charter always survives; if the charter alone exceeds the cap, send
            # it whole and drop system_text (the rules matter more than context).
            _charter = _load_cloud_charter()
            _sys_tail = system_text or ""
            _cloud_msgs = _msgs
            # Per-provider caps override global env defaults. Body size is the 413
            # signal; with tools capped at 8, groq bodies run ~4KB (25KB headroom
            # under its ~30KB limit) so caps can be generous. max_sys_chars here is
            # the TOTAL system budget (charter + tail).
            _cloud_sys_max = p.max_sys_chars if p.max_sys_chars is not None \
                else int(os.environ.get("CLAF_CLOUD_SYS_MAX_CHARS", "8000"))
            _cloud_msgs_max = p.max_msgs if p.max_msgs is not None \
                else int(os.environ.get("CLAF_CLOUD_MAX_MSGS", "20"))
            # full_context peers (e.g. cerebras, the workhorse) get the ENTIRE
            # natively-loaded memory + history untrimmed — charter still prepended.
            # This is what gives the hybrid the same context the primary agent has,
            # so the operator stops re-teaching it. Only small hard-capped peers
            # (groq) still trim.
            _full_ctx = getattr(p, "full_context", False)
            _trim_on = (not _full_ctx) and os.environ.get("CLAF_CLOUD_TRIM", "1") != "0"
            # full_context peers get the COMPLETE memory corpus prepended (after the
            # charter). The memory is what makes the hybrid KNOW the operator — a
            # subset is not enough. The charter teaches HOW to act; the memory pack
            # is WHO it's working for and everything it has learned.
            _mem_pack = _load_memory_pack() if _full_ctx else ""
            if _full_ctx:
                _charter = _charter + _mem_pack
                log("cloud_full_context", provider=p.name,
                    charter_chars=len(_charter) - len(_mem_pack),
                    memory_pack_chars=len(_mem_pack),
                    sys_tail_chars=len(_sys_tail),
                    msg_count=len(_cloud_msgs))
            if _trim_on:
                _tail_budget = _cloud_sys_max - len(_charter)
                if _tail_budget <= 0:
                    # Charter alone fills the budget — ship it whole, drop the tail.
                    _cloud_sys = _charter
                    if _sys_tail:
                        log("cloud_sys_tail_dropped", provider=p.name,
                            charter_chars=len(_charter), tail_chars=len(_sys_tail))
                elif len(_sys_tail) > _tail_budget:
                    _cloud_sys = _charter + _sys_tail[:_tail_budget]
                    log("cloud_sys_trimmed", provider=p.name,
                        charter_chars=len(_charter),
                        tail_before=len(_sys_tail), tail_after=_tail_budget)
                else:
                    _cloud_sys = _charter + _sys_tail
            else:
                _cloud_sys = _charter + _sys_tail
            if _trim_on:
                if len(_cloud_msgs) > _cloud_msgs_max:
                    _cloud_msgs = _cloud_msgs[-_cloud_msgs_max:]
                    log("cloud_msgs_trimmed", provider=p.name,
                        msgs_before=len(_msgs), msgs_after=_cloud_msgs_max)
                # Cap per-message content. A single tool_result (file read,
                # bash output) can be 10K+ chars — enough to 413 groq even after
                # count and system trimming. Truncate each message's string content.
                _msg_content_max = p.max_msg_content if p.max_msg_content is not None \
                    else int(os.environ.get("CLAF_CLOUD_MSG_CONTENT_MAX", "2000"))
                _trimmed_content = False
                _cloud_msgs_final = []
                for _m in _cloud_msgs:
                    c = _m.get("content")
                    if isinstance(c, str) and len(c) > _msg_content_max:
                        _m = dict(_m, content=c[:_msg_content_max])
                        _trimmed_content = True
                    _cloud_msgs_final.append(_m)
                if _trimmed_content:
                    log("cloud_msg_content_trimmed", provider=p.name, max_chars=_msg_content_max)
                _cloud_msgs = _cloud_msgs_final
            if _cloud_sys:
                _cloud_msgs = [{"role": "system", "content": _cloud_sys}] + _cloud_msgs
            _tools_eff = _tools
            if _tools and p.max_tools is not None and len(_tools) > p.max_tools:
                if p.max_tools == 0:
                    _tools_eff = None
                else:
                    # Sort tools for cloud peers with small caps. Priority order:
                    # 1. High-frequency sensei browser tools (by usage frequency)
                    # 2. Other mcp__sensei__* tools
                    # 3. Other mcp__* tools
                    # 4. Everything else (excluding 'claude' meta-tool which causes
                    #    wrong-tool cascade — model picks it over the right sensei tool)
                    _EXCLUDE = {"claude"}
                    _HIGH_FREQ = [
                        # Task tools FIRST — the operator runs the task-list loop
                        # constantly; these must never be capped out ("looking for
                        # task list tool" bug).
                        "TaskList", "TaskCreate", "TaskUpdate", "TaskGet",
                        # High-frequency sensei browser tools.
                        "mcp__sensei__tab_create", "mcp__sensei__screenshot",
                        "mcp__sensei__read_full", "mcp__sensei__click",
                        "mcp__sensei__fill", "mcp__sensei__browse",
                        "mcp__sensei__scroll", "mcp__sensei__key_press",
                        "mcp__sensei__read", "mcp__sensei__js_eval",
                    ]
                    _tool_map = {t.get("name"): t for t in _tools}
                    _priority = [_tool_map[n] for n in _HIGH_FREQ if n in _tool_map]
                    _priority_names = {t.get("name") for t in _priority}
                    _sensei_rest = [t for t in _tools if t.get("name", "").startswith("mcp__sensei__") and t.get("name") not in _priority_names]
                    _other_mcp = [t for t in _tools if t.get("name", "").startswith("mcp__") and not t.get("name", "").startswith("mcp__sensei__")]
                    _rest = [t for t in _tools if not t.get("name", "").startswith("mcp__") and t.get("name") not in _EXCLUDE]
                    _ordered = _priority + _sensei_rest + _other_mcp + _rest
                    _tools_eff = _ordered[:p.max_tools]
                log("cloud_tools_capped", provider=p.name,
                    tools_before=len(_tools), tools_after=len(_tools_eff) if _tools_eff else 0,
                    first_tools=[t.get("name") for t in (_tools_eff or [])][:5])
            _blocks, _usage, _tool_use = openai_compat_chat(p, _cloud_msgs, _tools_eff)
        elif p.kind == "anthropic":
            _blocks, _usage = anthropic_direct_chat(p, body)
            _tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in _blocks)
            _text = "".join(b.get("text", "") for b in _blocks if isinstance(b, dict) and b.get("type") == "text")
            return _blocks, _usage, False, _text, _tool_use
        else:
            raise RuntimeError(f"unknown provider kind: {p.kind}")

        # Fallback: model returned plain text despite having tools available —
        # try the heuristic directive scraper (covers prose-format tool calls
        # from models that don't emit native tool_calls).
        if not _tool_use and _tools:
            _text0 = "".join(b.get("text", "") for b in _blocks
                             if isinstance(b, dict) and b.get("type") == "text")
            if _text0:
                _scraped, _scraped_tu = parse_directives_to_content(_text0, _tools or [])
                if _scraped_tu:
                    _blocks, _tool_use = _scraped, True

        # NO tools passed but model wrote bash in markdown blocks or plain text
        # commands (qwen2.5:3b code-as-tools path). Extract as tool_use.
        if not _tool_use and not _tools:
            _text0 = "".join(b.get("text", "") for b in _blocks
                             if isinstance(b, dict) and b.get("type") == "text")
            if _text0:
                # Try markdown fenced blocks first
                _MD_FENCE = re.compile(
                    r"^\s*```(?:bash|sh|shell)\n(.*?)```\s*$",
                    re.DOTALL | re.MULTILINE,
                )
                for m in _MD_FENCE.finditer(_text0):
                    cmd = m.group(1).strip()
                    if cmd:
                        _blocks.append({
                            "type": "tool_use",
                            "id": f"toolu_claf_{uuid.uuid4().hex[:24]}",
                            "name": "bash",
                            "input": {"command": cmd},
                        })
                        _tool_use = True
                # Fallback: single-line text that looks like a shell command
                if not _tool_use:
                    _lines = [ln.strip() for ln in _text0.strip().splitlines() if ln.strip()]
                    if len(_lines) == 1:
                        ln = _lines[0]
                        if any(ln.startswith(p) for p in ("python3 ", "curl ", "ls ", "cat ", "grep ", "find ", "bash ", "sh ", "echo ", "mkdir ", "rm ", "cp ", "mv ", "cd ", "pwd", "whoami", "ps ", "top", "df ", "du ", "head ", "tail ", "sort ", "uniq ", "wc ", "tar ", "zip ", "unzip ", "git ", "npm ", "pip ", "docker ", "sudo ", "apt ", "yum ", "systemctl ", "journalctl ", "ssh ", "scp ", "rsync ", "wget ", "ping ", "netstat ", "ss ", "lsof ", "fuser ", "kill ", "pkill ", "pgrep ", "nice ", "nohup ", "screen ", "tmux ", "vi ", "vim ", "nano ", "emacs ", "less ", "more ")):
                            _blocks.append({
                                "type": "tool_use",
                                "id": f"toolu_claf_{uuid.uuid4().hex[:24]}",
                                "name": "bash",
                                "input": {"command": ln},
                            })
                            _tool_use = True

        _text = "".join(b.get("text", "") for b in _blocks
                        if isinstance(b, dict) and b.get("type") == "text")

        # Action bridge: auto-execute BROWSE:/SHELL:/FILE: directives in raw text
        if HAS_ACTION_BRIDGE and _text:
            _new_text = execute_actions_in_text(_text)
            if _new_text != _text:
                # Rebuild blocks with augmented text
                _blocks = [{"type": "text", "text": _new_text}] + [b for b in _blocks if not (isinstance(b, dict) and b.get("type") == "text")]
                _text = _new_text

        return _blocks, _usage, False, _text, _tool_use

    try:
        while True:
            try:
                # Run the BLOCKING provider call (httpx.Client → Ollama/cloud) in a
                # worker thread. If called inline, the synchronous httpx call freezes
                # the asyncio event loop for the entire request — so any concurrent
                # request (a second session, /healthz, even accepting a new TCP
                # connection) hangs until it finishes, surfacing as ConnectionRefused.
                # asyncio.to_thread keeps the loop responsive; Ollama still serializes
                # inference, but the proxy no longer goes dark while it works.
                content_blocks, usage, used_react, assistant_text, tool_use = await asyncio.to_thread(_dispatch_provider, provider)

                # ─── LOCAL AUTO-RETRY LOOP ─────────────────────────────────────
                # For local code-as-tools: when the model emits a bash command,
                # execute it immediately. On non-zero exit, feed stderr back as
                # a tool_result and let the model retry (max 3). This corrects
                # syntax errors, quoting issues, and wrong API calls without
                # round-tripping through the client.
                # Opt-in: CLAF_LOCAL_AUTO_RETRY=N (default 0 = off).
                # ────────────────────────────────────────────────────────────────
                _max_auto_retry = int(os.environ.get("CLAF_LOCAL_AUTO_RETRY", "0"))
                if _max_auto_retry > 0 and provider.pool == "local" and tool_use:
                    for _retry_turn in range(_max_auto_retry):
                        _bash_blocks = [
                            b for b in content_blocks
                            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "bash"
                        ]
                        if not _bash_blocks:
                            break

                        _all_ok = True
                        _tool_results = []
                        for _b in _bash_blocks:
                            _cmd = (_b.get("input") or {}).get("command", "")
                            if not _cmd:
                                continue
                            try:
                                _proc = subprocess.run(
                                    _cmd, shell=True, capture_output=True, text=True, timeout=60
                                )
                            except Exception as _exec_err:
                                _proc = type("obj", (object,), {
                                    "returncode": 1, "stdout": "", "stderr": str(_exec_err)
                                })()
                            _out = (_proc.stdout or "") + (_proc.stderr or "")
                            if len(_out) > 2000:
                                _out = _out[:2000] + "\n[…truncated…]"
                            _tool_results.append({
                                "tool_use_id": _b.get("id", "toolu_claf_unknown"),
                                "output": _out,
                                "exit_code": _proc.returncode,
                            })
                            if _proc.returncode != 0:
                                _all_ok = False

                        if _all_ok:
                            break  # all commands succeeded

                        # Build retry messages: append assistant turn + tool_results
                        _retry_msgs = list(body.get("messages", []))
                        _retry_msgs.append({
                            "role": "assistant",
                            "content": content_blocks,
                        })
                        _retry_msgs.append({
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tr["tool_use_id"],
                                    "content": f"ERROR (exit {tr['exit_code']}):\n{tr['output']}",
                                    "is_error": True,
                                }
                                for tr in _tool_results
                            ],
                        })
                        body["messages"] = _retry_msgs
                        log("local_auto_retry", retry=_retry_turn + 1, total=_max_auto_retry)

                        # Re-call the model with error context
                        content_blocks, usage, used_react, assistant_text, tool_use = await asyncio.to_thread(_dispatch_provider, provider)

                break  # success
            except Exception as _call_exc:
                # Any CLOUD peer failure (429 rate-limit, 413 payload-too-large,
                # 5xx, timeout, etc.) advances the fallback chain — the next peer
                # or local Ollama may succeed. LOCAL Ollama failures are terminal
                # and surface immediately (nothing left to fall back to).
                _status = getattr(getattr(_call_exc, "response", None), "status_code", None)
                is_cloud_failure = provider.pool == "cloud"
                if not is_cloud_failure:
                    raise  # local failure — surface immediately
                _rate_limit_failed.add(provider.name)
                log("cloud_peer_fallback", failed_provider=provider.name,
                    failed_tier=provider.tier, status=_status,
                    failed_so_far=sorted(_rate_limit_failed))
                provider = next_cloud_peer(_rate_limit_failed)
                if provider is None:
                    # No more cloud peers. In hybrid/local mode, degrade to the
                    # LOCAL Ollama provider — it never rate-limits and is the
                    # whole point of hybrid. Only error out if no local exists
                    # (cloud-only mode).
                    _local = next(
                        (p for p in PROVIDERS if p.pool == "local" and p.enabled),
                        None,
                    )
                    if _local is not None:
                        provider = _local
                        if trickle_reservation:
                            throttle.refund(trickle_reservation)
                            trickle_reservation = None
                        trickle_mode = "local"
                        log("rate_limit_degraded_to_local",
                            failed_peers=sorted(_rate_limit_failed),
                            local_provider=_local.name)
                    else:
                        raise RuntimeError(
                            f"all cloud peers rate-limited and no local fallback: "
                            f"{sorted(_rate_limit_failed)}"
                        ) from _call_exc
                else:
                    log("rate_limit_next_peer", next_provider=provider.name, next_tier=provider.tier)
    except Exception as e:
        log("provider_error", tier=getattr(provider, 'tier', None),
            name=getattr(provider, 'name', 'unknown'), error=str(e),
            rate_limit_chain=sorted(_rate_limit_failed) if _rate_limit_failed else None)
        if trickle_reservation:
            throttle.refund(trickle_reservation)
            log("trickle_refund", reservation=trickle_reservation, reason="provider_error")
        _pname = getattr(provider, 'name', 'all-peers-exhausted')
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "type": "api_error",
                    "message": f"{_pname} call failed: {e}",
                }
            },
        )

    # Tap polish — runs after the local draft returns, sends the largest
    # fenced snippet to a cheap cloud peer with an intent-specific template,
    # splices the polished version back in. Failures here are non-fatal —
    # the local draft is still returned.
    if trickle_mode == "tap" and provider.pool == "local" and assistant_text:
        polished = _do_tap_polish(body, assistant_text)
        if polished != assistant_text:
            assistant_text = polished
            content_blocks = [{"type": "text", "text": assistant_text}]
            tool_use = False

    # Append degrade note (visible to the operator inside the response) when
    # the throttle had to step the request down. Local-mode 423s and pure
    # routine local don't get a note.
    if trickle_degrade_note:
        assistant_text = (assistant_text or "") + "\n\n" + trickle_degrade_note
        # Repack content blocks to surface the note in the final response.
        if content_blocks and isinstance(content_blocks[0], dict) and content_blocks[0].get("type") == "text":
            content_blocks[0] = {"type": "text", "text": assistant_text}
        else:
            content_blocks.append({"type": "text", "text": trickle_degrade_note})

    response = wrap_anthropic_response(requested_model, content_blocks, usage, tool_use)
    log(
        "response_out",
        tier=getattr(provider, 'tier', None),
        name=getattr(provider, 'name', 'unknown'),
        out_chars=len(assistant_text),
        tool_use=tool_use,
        tool_use_count=sum(1 for b in content_blocks if b.get("type") == "tool_use"),
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        trickle_mode=trickle_mode,
    )
    if trickle_reservation:
        throttle.commit(trickle_reservation)
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

    if LOCAL_MODEL and OLLAMA_URL:
        import threading
        def _prewarm():
            base = OLLAMA_URL.replace("/api/chat", "")
            try:
                with httpx.Client(timeout=180.0) as c:
                    c.post(f"{base}/api/generate", json={
                        "model": LOCAL_MODEL, "prompt": "hi", "stream": False,
                        "keep_alive": -1,
                    })
                print(f"[prewarm] {LOCAL_MODEL} loaded and pinned in memory (keep_alive=-1)")
            except Exception as e:
                print(f"[prewarm] warning: {e}")
        threading.Thread(target=_prewarm, daemon=True).start()

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

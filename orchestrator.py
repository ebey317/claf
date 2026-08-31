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
import contextvars
import enum
import json
import os
import platform
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

import contextlib
import threading

import claf_permissions
import claf_throttle as throttle
import sensei_supervisor as supervisor  # ReAct XML tool-call translator (off-grid MCP)
import tool_bridge  # ReAct tools[] bridge helpers (added by install_tool_bridge.sh)
from claf_config import (
    _EMAIL_SIGNALS,
    MODE,
    PROVIDERS,
    TAP_TEMPLATES,
    _flatten_prompt_text,
    _is_action_turn,
    _is_hard_task,
    _matches_toolbox_command,
    _select_mode,
    describe,
    detect_tap_intent,
    next_cloud_peer,
    pick_cloud_peer,
    select_local_tools,
    select_provider,
)
from task_state import (
    TASK_FILE,
    format_task_for_injection,
    load_task,
    save_task,
    task_belongs_to,
)

try:
    from orchestrator_action_bridge import execute_actions_in_text

    HAS_ACTION_BRIDGE = True
except Exception:
    HAS_ACTION_BRIDGE = False

# Serialize cloud Ollama requests — concurrent calls to the SSH-tunneled cloud
# model cause 500s. One in-flight at a time; others queue and wait.
_OLLAMA_CLOUD_LOCK = threading.Lock()

# How long Ollama keeps the local model loaded after a request.
# Default -1 = pinned forever (fastest for multi-turn, but can leave a stuck
# runner burning CPU on some Ollama/model combos). Set to 0 on Mary to unload
# immediately after each generation, avoiding the hermes3:3b stuck-runner bug.
_OLLAMA_KEEP_ALIVE = int(os.environ.get("CLAF_OLLAMA_KEEP_ALIVE", "-1"))


PORT = int(os.environ.get("CLAF_PORT", "8000"))
LOG_FILE = Path(
    os.environ.get("CLAF_LOG_FILE", str(Path.home() / "projects/claf/orchestrator.log"))
)
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# Persistent conversation log so operators can look back at what they asked
# Mary/CLAF. One JSON line per turn, capturing user prompt + assistant reply.
CONVERSATION_LOG_FILE = Path(
    os.environ.get(
        "CLAF_CONVERSATION_LOG_FILE",
        str(Path.home() / ".claf/conversation.log"),
    )
)
CONVERSATION_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

# Runtime platform detection so every model knows which OS commands to use.
_PLATFORM_SYSTEM = platform.system()
_PLATFORM_RELEASE = platform.release()
_PLATFORM_MACHINE = platform.machine()
_PLATFORM_CAP_FILE = Path.home() / ".claf" / "system_capabilities.json"


def _load_platform_capabilities() -> str:
    """Return a system-prompt block describing the current OS and native commands.

    Reads ~/.claf/system_capabilities.json if it exists (generated by
    system_capability_scan.py); otherwise falls back to hardcoded guidance.
    """
    try:
        if _PLATFORM_CAP_FILE.exists():
            caps = json.loads(_PLATFORM_CAP_FILE.read_text())
            os_name = caps.get("os", _PLATFORM_SYSTEM)
            pretty = caps.get("distro", {}).get("PRETTY_NAME", _PLATFORM_RELEASE)
            de = caps.get("desktop", {})
            de_name = de.get("xdg_current_desktop", "unknown")
            session_type = de.get("xdg_session_type", "unknown")
            lines = [
                f"PLATFORM: {os_name} {pretty} ({caps.get('machine', _PLATFORM_MACHINE)}).",
                f"DESKTOP ENVIRONMENT: {de_name} on {session_type}.",
                "Use native terminal commands for this OS. Preferred local-search patterns:",
            ]
            if os_name == "Linux":
                lines += [
                    "- Find executable: `which <name>` or `whereis <name>`",
                    "- Find files: `find /usr -name '<name>*'` or `locate <name>`",
                    "- List packages: `dpkg -L <package>` / `rpm -ql <package>` / `pacman -Ql <package>`",
                    "- Search installed: `apt list --installed | grep <name>`",
                ]
            elif os_name == "Darwin":
                lines += [
                    "- Find executable: `which <name>` or `whereis <name>`",
                    "- Find files: `mdfind 'kMDItemFSName == \"*<name>*\"'` or `find / -name '<name>*'`",
                    "- List apps: `ls /Applications | grep <name>`",
                    "- Homebrew: `brew list | grep <name>`",
                ]
            elif os_name == "Windows":
                lines += [
                    "- Find executable: `where <name>` or `Get-Command <name>`",
                    "- Find files: `dir /s /b *<name>*`",
                    "- Packages: `winget list | findstr <name>`",
                ]
            pms = caps.get("package_managers", [])
            if pms:
                lines.append(f"Available package managers: {', '.join(pms)}")
            terms = caps.get("terminal_emulators", [])
            if terms:
                lines.append(f"Available terminal emulators: {', '.join(terms)}")
            lines.append(
                "Use `xdg-open` (Linux), `open` (macOS), or `start` (Windows) to open files/URLs in the default desktop app."
            )
            lines.append(
                "If a needed command is missing, use Bash to install it via the native package manager."
            )
            lines.append("Do not open `file://` URLs in the browser unless explicitly asked.")
            return "\n".join(lines)
    except Exception:
        pass

    # Fallback guidance if capability file is missing/unreadable.
    return {
        "Linux": (
            "You are running on Linux. Use Linux/Unix commands and paths. "
            "For local filesystem searches (apps, files, executables), prefer the terminal: "
            "`which <name>`, `find /usr -name '<name>*'`, `ls /usr/bin | grep <name>`, "
            "`dpkg -L <package>`, `whereis <name>`. "
            "Do not open `file://` URLs in the browser unless explicitly asked."
        ),
        "Darwin": (
            "You are running on macOS. Use Unix/BSD commands and paths. "
            "For local filesystem searches (apps, files, executables), prefer the terminal: "
            "`which <name>`, `find /Applications -name '<name>*'`, "
            "`ls /usr/local/bin | grep <name>`, `mdfind 'kMDItemFSName == \"*<name>*\"'`, "
            "`whereis <name>`. "
            "Do not open `file://` URLs in the browser unless explicitly asked."
        ),
        "Windows": (
            "You are running on Windows. Use Windows commands and paths. "
            "For local filesystem searches (apps, files, executables), prefer the terminal: "
            "`where <name>`, `Get-Command <name>` (PowerShell), "
            "`dir /s /b *<name>*`, `winget list | findstr <name>`. "
            "Do not open `file://` URLs in the browser unless explicitly asked."
        ),
    }.get(
        _PLATFORM_SYSTEM,
        (
            f"You are running on {_PLATFORM_SYSTEM} {_PLATFORM_RELEASE} ({_PLATFORM_MACHINE}). "
            "Use the native terminal/shell commands for this operating system. "
            "For local filesystem searches (apps, files, executables), prefer the terminal "
            "over opening `file://` URLs in the browser."
        ),
    )


_PLATFORM_GUIDANCE = _load_platform_capabilities()

# Convenience: the local provider is the default target when present. In
# `cloud` mode there is no local provider — leave these unset/None and let
# vision-routing and /v1/models handle the absence gracefully.
_LOCAL = next((p for p in PROVIDERS if p.pool == "local"), None)
LOCAL_MODEL = _LOCAL.model if _LOCAL else None
OLLAMA_URL = _LOCAL.url if _LOCAL else None

# Three-tier local routing:
#   CLAF_VISION_MODEL  → image requests
#   CLAF_SPEED_MODEL   → short/simple text (no tools, ≤3 user msgs, ≤200 chars)
#   CLAF_LOCAL_MODEL   → everything else (workhorse: code, tools, agents)
VISION_MODEL = os.environ.get("CLAF_VISION_MODEL", "").strip() or None
SPEED_MODEL = os.environ.get("CLAF_SPEED_MODEL", "").strip() or None


def _request_has_image(body: dict) -> bool:
    """Return True if any message content block is an image."""
    for msg in body.get("messages", []) or []:
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image":
                    return True
    return False


def _request_is_simple(body: dict) -> bool:
    """True for short plain-text turns: no tools, few msgs, short prompt."""
    if body.get("tools"):
        return False
    msgs = body.get("messages", []) or []
    user_msgs = [m for m in msgs if m.get("role") == "user"]
    if len(user_msgs) > 3:
        return False
    last = user_msgs[-1].get("content", "") if user_msgs else ""
    text = (
        last
        if isinstance(last, str)
        else " ".join(b.get("text", "") for b in last if isinstance(b, dict))
    )
    return len(text) < 300


def select_local_model(body: dict) -> str:
    """Two-tier routing:
    - image → qwen3-vl:8b (vision, on-demand)
    - everything else → qwen3.5:9b (stays warm in VRAM)
      think:false when tools present (execution), think:true when planning
    hermes3:3b kept as reserve — not in active rotation (VRAM swap cost > speed gain)
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
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ts_ms": int(time.time() * 1000),
        "event": event,
        **fields,
    }
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def log_conversation(
    turn_id: str, role: str, content, model: str = None, provider: str = None
) -> None:
    """Append a conversation turn line to the persistent conversation log.

    role is 'user' or 'assistant'. content is the raw content (string or list
    of Anthropic content blocks). This gives operators a readable transcript
    independent of the verbose orchestrator.log.
    """
    try:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "ts_ms": int(time.time() * 1000),
            "turn_id": turn_id,
            "role": role,
            "model": model,
            "provider": provider,
            "content": content,
        }
        with CONVERSATION_LOG_FILE.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        # Conversation logging must never break a request.
        pass


# Per-turn instrumentation for latency analysis (Session 6 C21).
_claf_turn: contextvars.ContextVar[dict | None] = contextvars.ContextVar("claf_turn", default=None)


def _current_turn() -> dict | None:
    return _claf_turn.get()


def _inc_charter_stat_count() -> None:
    """Count a charter/memory filesystem stat() for per-turn observability."""
    turn = _current_turn()
    if turn is not None:
        turn["charter_stat_count"] = turn.get("charter_stat_count", 0) + 1


def _mark(name: str) -> None:
    """Record a monotonic timestamp (ms since turn start) in the current turn."""
    turn = _current_turn()
    if turn is None:
        return
    turn["marks"][name] = int((time.monotonic() - turn["t0"]) * 1000)


def _record_dispatch(kind: str, provider, start: float, end: float) -> None:
    """Append a dispatch record to the current turn."""
    turn = _current_turn()
    if turn is None:
        return
    turn["dispatches"].append(
        {
            "kind": kind,
            "provider": getattr(provider, "name", "unknown"),
            "model": getattr(provider, "model", "unknown"),
            "start_ms": int((start - turn["t0"]) * 1000),
            "end_ms": int((end - turn["t0"]) * 1000),
        }
    )


def _conv_fingerprint(msgs: list[dict]) -> str:
    """Short stable fingerprint of the first user text for inter-turn grouping."""
    for m in msgs:
        if m.get("role") == "user":
            c = m.get("content", "")
            text = c if isinstance(c, str) else json.dumps(c)
            import hashlib

            return hashlib.sha1(text[:256].encode("utf-8")).hexdigest()[:10]
    return "0000000000"


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
_charter_cache: dict = {"mtime": None, "text": None, "checked_at": 0}


def _load_cloud_charter() -> str:
    """Return the cloud operational charter, reloading from disk when the file
    changes. Falls back to the inline charter if the file is missing or empty.
    A 2-second monotonic TTL prevents repeated stat() calls within a burst."""
    try:
        now = time.monotonic()
        if now - _charter_cache.get("checked_at", 0) >= 2.0:
            _inc_charter_stat_count()
            st = _CHARTER_FILE.stat()
            _charter_cache["checked_at"] = now
            if _charter_cache.get("mtime") != st.st_mtime:
                txt = _CHARTER_FILE.read_text(encoding="utf-8").strip()
                if txt:
                    _charter_cache["mtime"] = st.st_mtime
                    _charter_cache["text"] = txt + "\n\n"
        if _charter_cache.get("text"):
            return _charter_cache["text"] + _PLATFORM_GUIDANCE + "\n\n"
    except (OSError, UnicodeDecodeError) as exc:
        log("charter_load_failed", error=str(exc), using="inline_fallback")
    return _CHARTER_FALLBACK + _PLATFORM_GUIDANCE + "\n\n"


# Charter slice loader — surgical context injection for local models.
# Cloud peers still use the full cloud_charter.md via _load_cloud_charter().
# Local Ollama gets only the slices relevant to the current request type,
# cutting the system prompt from 6237 → ~2600-3200 chars per turn.
_CHARTER_DIR = Path(__file__).parent / "charter"
_charter_slice_cache: dict[str, dict] = {}


def _load_charter_slice(name: str) -> str:
    """Load one charter slice file with mtime + 2s TTL caching. Returns "" on missing."""
    path = _CHARTER_DIR / f"{name}.md"
    try:
        now = time.monotonic()
        cached = _charter_slice_cache.get(name, {})
        if now - cached.get("checked_at", 0) >= 2.0:
            _inc_charter_stat_count()
            st = path.stat()
            cached["checked_at"] = now
            if cached.get("mtime") != st.st_mtime:
                text = path.read_text(encoding="utf-8").strip()
                cached["mtime"] = st.st_mtime
                cached["text"] = text
            _charter_slice_cache[name] = cached
        return _charter_slice_cache[name]["text"]
    except (OSError, UnicodeDecodeError):
        return ""


def _load_charter_slices(body: dict) -> str:
    """Return charter_core + relevant slices based on what tools are present
    and what signals appear in the last user message.

    Always: charter_core
    + charter_browser when sensei tools are in the request
    + charter_tasks when TaskList/Create/Update or task-signal words appear
    + charter_debug when error signals or tool failures are in history

    Falls back to full cloud_charter.md if charter/ directory is missing.
    """
    core = _load_charter_slice("charter_core")
    if not core:
        return _load_cloud_charter()

    # Tool-less local mode: strip the KNOWN COMMANDS block so a model that
    # has zero tool schemas does not learn to emit raw tool names as text.
    local_max_tools = int(os.environ.get("CLAF_LOCAL_MAX_TOOLS", "6") or "6")
    if local_max_tools == 0:
        core = re.sub(r"KNOWN COMMANDS[^\n]*\n(?:-[^\n]*\n)+", "", core)

    mode_block = claf_permissions.mode_prompt_block()
    if "<!-- PERMISSION_MODE_BLOCK -->" in core:
        core = core.replace("<!-- PERMISSION_MODE_BLOCK -->", mode_block, 1)
        slices = [core]
    else:
        slices = [core, mode_block]

    # Last user message text for signal scoring
    last_user = ""
    for m in reversed(body.get("messages", [])):
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, list):
                c = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
            last_user = str(c).lower()
            break

    tool_names = {t.get("name", "") for t in (body.get("tools") or [])}

    # Browser slice: sensei tools present in request
    if any("mcp__sensei__" in n for n in tool_names):
        s = _load_charter_slice("charter_browser")
        if s:
            slices.append(s)

    # Tasks slice: Task* tools present OR task words in prompt
    _task_words = {
        "tasklist",
        "task list",
        "task",
        "backlog",
        "claim",
        "batch",
        "memory",
        "write a memory",
        "create a memory",
    }
    if any(n.startswith("Task") for n in tool_names) or any(w in last_user for w in _task_words):
        s = _load_charter_slice("charter_tasks")
        if s:
            slices.append(s)

    # Debug slice: error signals in prompt or failed tool_results in history
    _debug_words = {
        "error",
        "fail",
        "broken",
        "debug",
        "log",
        "not working",
        "why",
        "check log",
        "what went wrong",
        "diagnose",
    }
    _has_tool_error = any(
        isinstance(m.get("content"), list)
        and any(
            isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error")
            for b in m["content"]
        )
        for m in body.get("messages", [])
        if m.get("role") == "user"
    )
    if _has_tool_error or any(w in last_user for w in _debug_words):
        s = _load_charter_slice("charter_debug")
        if s:
            slices.append(s)

    slices.insert(0, _PLATFORM_GUIDANCE)

    # Tool-less local mode: explicitly instruct the model it cannot call tools.
    # This blocks hallucinated raw tool names (e.g. "mcp__sensei__screenshot")
    # when the runtime has stripped all tool schemas from the local payload.
    if local_max_tools == 0:
        slices.append(
            "[TOOL MODE: NONE] You have no tools available. "
            "Respond conversationally only. "
            "Never output mcp__, TaskList, Bash:, or command strings."
        )

    log(
        "charter_slices_selected",
        slices=[s[:20] for s in slices],
        total_chars=sum(len(s) for s in slices),
        platform=_PLATFORM_SYSTEM,
    )
    return "\n\n---\n\n".join(slices) + "\n\n"


# FULL MEMORY PACK — the complete memory corpus (every *.md file, full bodies)
# injected into full_context peers so the hybrid KNOWS the operator the same way
# the primary agent does. The memory is what makes it personal; a subset is not
# enough. Loaded from the memory dir, cached by newest-mtime so edits to any
# memory file refresh the pack on the next request (no restart).
_MEMORY_DIR = Path(
    os.environ.get(
        "CLAF_MEMORY_DIR",
        str(Path.home() / ".claude/projects/-home-elijah/memory"),
    )
)
_memory_cache: dict = {"sig": None, "text": None, "checked_at": 0}


def _load_memory_pack() -> str:
    """Concatenate EVERY memory .md file (full body) into one pack, with a header
    per file so the model can cite which memory a fact came from. Cached by a
    signature of (file, mtime, size) across the dir so any edit refreshes it.
    A 2-second monotonic TTL skips stat() bursts between rapid turns.
    Returns '' if the dir is missing/empty — never raises into the request path."""
    try:
        now = time.monotonic()
        if now - _memory_cache.get("checked_at", 0) < 2.0 and _memory_cache.get("text"):
            return _memory_cache["text"]
        files = sorted(_MEMORY_DIR.glob("*.md"))
        if not files:
            return ""
        sig = tuple((f.name, f.stat().st_mtime, f.stat().st_size) for f in files)
        _inc_charter_stat_count()
        for _ in files:
            _inc_charter_stat_count()
        if _memory_cache.get("sig") == sig and _memory_cache.get("text"):
            _memory_cache["checked_at"] = now
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
        _memory_cache["checked_at"] = now
        log("memory_pack_built", files=len(files), chars=len(pack))
        return pack
    except OSError as exc:
        log("memory_pack_failed", error=str(exc))
        return ""


# MD FILES LOADER — reads all *.md files from ~/MD/ (handoff dock, session notes,
# etc.) and injects them for local models AFTER _trim_for_local so they are
# never truncated. Cap controlled by CLAF_MD_MAX_CHARS (default 3000).
_MD_FILES_DIR = Path(os.environ.get("CLAF_MD_FILES_DIR", str(Path.home() / "MD")))
_md_files_cache: dict = {"sig": None, "text": None, "checked_at": 0}


def _load_md_files() -> str:
    """Read every *.md file in ~/MD/ (follows symlinks), concatenate with headers,
    cap at CLAF_MD_MAX_CHARS, cache by mtime signature. Returns '' on error."""
    try:
        max_chars = int(os.environ.get("CLAF_MD_MAX_CHARS", "3000"))
        now = time.monotonic()
        if (
            now - _md_files_cache.get("checked_at", 0) < 2.0
            and _md_files_cache.get("text") is not None
        ):
            return _md_files_cache["text"]
        files = sorted(_MD_FILES_DIR.glob("*.md"))
        if not files:
            _md_files_cache.update({"sig": (), "text": "", "checked_at": now})
            return ""
        sig = tuple((f.name, f.stat().st_mtime, f.stat().st_size) for f in files)
        if _md_files_cache.get("sig") == sig and _md_files_cache.get("text") is not None:
            _md_files_cache["checked_at"] = now
            return _md_files_cache["text"]
        parts = ["===== MD FILES (~/MD/) — handoff dock, open work =====\n"]
        total = len(parts[0])
        for f in files:
            try:
                body = f.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                continue
            if not body:
                continue
            header = f"\n----- {f.name} -----\n"
            available = max_chars - total - len(header) - 30
            if available <= 0:
                break
            chunk = body[:available]
            if len(body) > available:
                chunk = chunk.rstrip() + "\n[…trimmed…]"
            parts.append(header + chunk + "\n")
            total += len(header) + len(chunk)
            if total >= max_chars:
                break
        parts.append("\n===== END MD FILES =====\n")
        pack = "".join(parts)
        _md_files_cache.update({"sig": sig, "text": pack, "checked_at": now})
        log("md_files_loaded", files=len(files), chars=len(pack))
        return pack
    except OSError as exc:
        log("md_files_load_failed", error=str(exc))
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
                inner = "\n".join(
                    b.get("text", "") if isinstance(b, dict) else str(b) for b in inner
                )
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
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in system)
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
    for t in tools or []:
        schema = t.get("input_schema") or {}
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": schema,
                },
            }
        )
    return out


def _tools_to_ollama_format(tools: list[dict]) -> dict:
    """Build a JSON schema for Ollama `format` that constrains the model to emit
    exactly one valid tool call. This is the mechanical enforcement layer for
    small local models that otherwise hallucinate tool names or arguments.

    Output schema shape:
        {
          "name": "<tool_name>",
          "arguments": { <tool-specific parameters> }
        }
    """
    if not tools:
        return {"type": "object"}

    one_of = []
    for t in tools:
        name = t.get("name", "")
        schema = t.get("input_schema") or t.get("parameters") or {}
        # Make sure nested schemas declare themselves objects so Ollama's
        # JSON-schema validator does not choke on bare parameter lists.
        if schema.get("type") != "object":
            schema = {"type": "object", "properties": {}, "required": []}
        one_of.append(
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "enum": [name]},
                    "arguments": schema,
                },
                "required": ["name", "arguments"],
            }
        )

    if len(one_of) == 1:
        return one_of[0]
    return {"oneOf": one_of}


def messages_from_anthropic(claude_messages: list, flavor: str = "openai"):
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
                        x.get("text", "") if isinstance(x, dict) else str(x) for x in inner
                    )
                if flavor == "openai":
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": tr.get("tool_use_id", ""),
                            "content": inner or "",
                        }
                    )
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
                tcs = [
                    {
                        "id": tu.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": tu.get("name", ""),
                            "arguments": json.dumps(tu.get("input", {})),
                        },
                    }
                    for tu in tool_use_blocks
                ]
                out.append({"role": "assistant", "content": text_joined or None, "tool_calls": tcs})
            else:
                tcs = [
                    {"function": {"name": tu.get("name", ""), "arguments": tu.get("input", {})}}
                    for tu in tool_use_blocks
                ]
                out.append({"role": "assistant", "content": text_joined or "", "tool_calls": tcs})
            continue

        # Plain message (text, maybe images).
        r = role if role in ("user", "assistant") else "user"
        pmsg = {"role": r, "content": text_joined}
        if images and flavor == "ollama":
            pmsg["images"] = images
        out.append(pmsg)
    return out


def openai_tool_calls_to_anthropic(
    message: dict, available_tools: list[dict] | None = None
) -> tuple[list[dict], bool]:
    """OpenAI choices[0].message → Anthropic content blocks.
    OpenAI returns tool-call arguments as a JSON STRING. Returns
    (content_blocks, tool_use_bool)."""
    blocks: list[dict] = []
    # Reasoning models (Cerebras gpt-oss-120b, zai-glm-4.7) put output in
    # message.reasoning when message.content is null. Fall back to it ONLY when
    # there is no tool call either — raw analysis-channel text surfacing as the
    # assistant's reply reads like leaked chain-of-thought to the operator
    # (gaming PC live test 2026-06-11: "The user posted a message that seems…").
    text = message.get("content") or ""
    if not text and not message.get("tool_calls"):
        text = message.get("reasoning") or ""
    if text:
        blocks.append({"type": "text", "text": text})
    tool_calls = message.get("tool_calls") or []

    # Recovery: <function=ToolName>{"args"}</function> XML-tag format.
    # gpt-oss-120b (Cerebras) and some Qwen-based models embed tool calls as
    # XML tags in the content string instead of the native tool_calls field.
    # ONLY run recovery when tools were actually sent to the provider; otherwise
    # text-only fallbacks (Groq) hallucinate tool-call prose that gets parsed
    # into dozens of bogus tool_use blocks.
    if available_tools and not tool_calls and text:
        _fn_re = re.compile(
            r"<function=([A-Za-z_][A-Za-z0-9_.:-]*)>\s*(.*?)\s*</function>",
            re.DOTALL,
        )
        _recovered: list[dict] = []
        for _m in _fn_re.finditer(text):
            try:
                _recovered.append(
                    {
                        "id": f"toolu_claf_{uuid.uuid4().hex[:24]}",
                        "type": "function",
                        "function": {
                            "name": _m.group(1),
                            "arguments": json.loads(_m.group(2).strip()),
                        },
                    }
                )
            except Exception:
                pass
        if _recovered:
            tool_calls = _recovered
            # Strip function tags from display text
            text = _fn_re.sub("", text).strip()
            blocks = [{"type": "text", "text": text}] if text else []

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
        blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id") or f"toolu_claf_{uuid.uuid4().hex[:24]}",
                "name": fn.get("name", ""),
                "input": args,
            }
        )
    if not blocks:
        blocks = [{"type": "text", "text": ""}]
    return blocks, bool(tool_calls)


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
        blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id") or f"toolu_claf_{uuid.uuid4().hex[:24]}",
                "name": fn.get("name", ""),
                "input": args,
            }
        )
    if not blocks:
        blocks = [{"type": "text", "text": ""}]
    return blocks, bool(tool_calls)


def _repair_malformed_tool_json(
    text: str, tools: list[dict], model: str | None = None
) -> list[dict]:
    """Recover tool calls from models that emit tool JSON as text but with
    syntax errors (e.g. hermes3:3b on CPU). Looks for JSON-like objects with
    'name' and 'arguments' keys, fixes common quoting mistakes, validates
    against tool input schemas, and maps the result to Ollama's tool_call
    format."""
    import ast as _ast
    import json as _json

    if not text or not tools:
        return []
    available = {t.get("name"): t for t in tools if t.get("name")}
    repaired = []
    saw_known_tool = False

    def _try_json_loads(raw: str):
        try:
            return _normalize_keys(_json.loads(raw))
        except Exception:
            return None

    def _normalize_keys(obj):
        """Strip whitespace from JSON object keys (hermes3 emits " name":)."""
        if isinstance(obj, dict):
            return {k.strip(): _normalize_keys(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_normalize_keys(v) for v in obj]
        return obj

    def _fix_json_text(raw: str) -> str:
        # Strip markdown ```json ... ``` fences.
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
        if fenced:
            raw = fenced.group(1)
        # Balanced single-quote -> double-quote swap for dict bodies.
        # Only swap when the body appears to use single quotes as delimiters.
        if "'" in raw and raw.count("'") % 2 == 0 and raw.count('"') == 0:
            raw = raw.replace("'", '"')
        # Remove trailing commas before } or ].
        raw = re.sub(r",(\s*[}\]])", r"\1", raw)
        # Normalize weirdly-spaced quoted keys: " name": -> "name":
        fixed = re.sub(r'([{,]\s*)"\s*(\w+)\s*"(\s*:)', r'\1"\2"\3', raw)
        # Fix unquoted keys like {name: ...}.
        fixed = re.sub(r"([{,]\s*)(\w+)(\s*:)", r'\1"\2"\3', fixed)
        # Fix "name: ..." where the opening quote before the key is never closed.
        fixed = re.sub(r'["\'](\w+)(\s*:)', r'"\1":', fixed)
        # Fix missing closing quote on string values: "foo} -> "foo"}
        fixed = re.sub(r'(:\s*"[^"]*?)(\s*\})', r'\1"\2', fixed)
        # Fix single-quoted string values inside double-quoted JSON.
        fixed = re.sub(r"(:\s*)'([^']*)'", r'\1"\2"', fixed)
        return fixed

    for match in re.finditer(r"\{(?:[^{}]|\{[^{}]*\})*\}", text, re.DOTALL):
        cand = match.group(0)
        data = _try_json_loads(cand)
        if data is None:
            data = _try_json_loads(_fix_json_text(cand))
        if data is None:
            # Last resort: pull name/arguments out with regex and parse
            # arguments as a Python dict literal.
            try:
                nm = re.search(r'["\']?\s*name\s*["\']?\s*:\s*["\']([^"\']+)["\']', cand)
                am = re.search(r'["\']?\s*arguments\s*["\']?\s*:\s*(\{.*?\})', cand, re.DOTALL)
                if nm and am:
                    name_str = nm.group(1).strip()
                    args_dict = _normalize_keys(_ast.literal_eval(am.group(1)))
                    if isinstance(args_dict, dict):
                        data = {"name": name_str, "arguments": args_dict}
            except Exception:
                pass
        if not isinstance(data, dict):
            continue
        name = data.get("name") or data.get("tool")
        args = data.get("arguments") or data.get("input") or data.get("parameters") or {}
        if not name or not isinstance(name, str):
            continue
        name = name.split()[0].strip("\"'")
        if name not in available:
            continue
        saw_known_tool = True
        if not isinstance(args, dict):
            if isinstance(args, str):
                s = args.strip()
                if name == "Bash":
                    args = {"command": s}
                elif name == "Write":
                    # Case 1: Python dict literal in the string, e.g.
                    # "{'content': 'step 1', 'file_path': '/tmp/x/file_1.txt'}"
                    if s.startswith("{") and s.endswith("}"):
                        try:
                            d = _ast.literal_eval(s)
                            if isinstance(d, dict):
                                path = d.get("file_path") or d.get("path") or d.get("file")
                                content = d.get("content") or d.get("text") or d.get("data", "")
                                if path:
                                    args = {"file_path": path, "content": str(content)}
                                else:
                                    args = {}
                            else:
                                args = {}
                        except Exception:
                            args = {}
                    else:
                        # Case 2: "file_path content" shorthand
                        parts = s.split(None, 1)
                        if len(parts) == 2:
                            content = parts[1].strip("'") if parts[1].startswith("'") else parts[1]
                            args = {"file_path": parts[0], "content": content}
                        else:
                            args = {"file_path": parts[0], "content": ""} if parts else {}
                elif name == "Read":
                    args = {"file_path": s}
                else:
                    args = {}
            else:
                args = {}

        # Schema validation: reject repaired calls that miss required params.
        tool_def = available.get(name, {})
        schema = tool_def.get("input_schema") or tool_def.get("parameters") or {}
        required = schema.get("required", []) if isinstance(schema, dict) else []
        missing = [k for k in required if k not in args]
        if missing:
            log("tool_call_repair_rejected", tool=name, missing=missing, snippet=text[:160])
            continue

        repaired.append({"function": {"name": name, "arguments": args}})

    if not repaired and saw_known_tool and model:
        log("tool_call_repair_failed", model=model, snippet=text[:160])
    return repaired


def ollama_chat(
    provider, messages: list[dict], tools: list[dict] | None = None, max_tokens: int | None = None
) -> tuple[list[dict], dict, bool]:
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

    THINKING_MODELS = {
        "qwen3.5:2b",
        "qwen3.5:9b",
        "qwen3:8b",
        "qwen3:14b",
        "qwen3:30b",
        "qwen3:32b",
    }
    # Thinking budget tiers: light=512 medium=1024 heavy=2048 extra=4096 tokens
    THINK_BUDGETS = {"light": 512, "medium": 1024, "heavy": 2048, "extra": 4096}
    think_level = os.environ.get("CLAF_THINK_LEVEL", "medium").strip().lower()
    think_budget = THINK_BUDGETS.get(think_level, 1024)

    opts = {"temperature": 0.1, "num_predict": num_predict, "num_ctx": num_ctx}
    num_thread = int(os.environ.get("CLAF_OLLAMA_NUM_THREADS", "0"))
    if not is_cloud and num_thread > 0:
        opts["num_thread"] = num_thread

    payload = {
        "model": provider.model,
        "messages": messages,
        "stream": False,
        "options": opts,
    }
    if not is_cloud:
        # Keep the local model pinned in RAM. Without this, each request resets
        # ollama's keep_alive to the 5m default — overriding the prewarm pin —
        # and any idle gap >5m costs a ~3.6s model reload on the next turn.
        payload["keep_alive"] = (
            _OLLAMA_KEEP_ALIVE  # default -1; override via CLAF_OLLAMA_KEEP_ALIVE
        )
    if provider.model in THINKING_MODELS:
        if (
            think_level == "none"
            or bool(tools)
            or _request_is_simple({"tools": tools, "messages": messages})
        ):
            # thinking disabled, execution mode, or simple chat — thinking off
            payload["think"] = False
        else:
            # complex planning — thinking on at configured budget
            payload["think"] = True
            payload["options"]["num_predict"] = think_budget
    if tools:
        payload["tools"] = _anthropic_tools_to_ollama(tools)

    # Tool calling strategy: native (default) vs. constrained decoding (opt-in).
    # Benchmark (2026-08-26): native tool_calls = 29.1s, correct; constrained = 300s+ timeout.
    # Native is now DEFAULT; constrained is OPT-IN via CLAF_LOCAL_CONSTRAINED=1.
    # Modern Ollama models (master-ai, qwen, hermes) support native tool_calls and emit
    # properly-formed tool_use blocks without forced grammar, so constrained decoding
    # is a workaround only needed for older/non-capable models.
    _constrained = not is_cloud and tools and os.environ.get("CLAF_LOCAL_CONSTRAINED", "0") == "1"
    if _constrained:
        # Force output to match a specific JSON schema. Use ONLY when model doesn't
        # support native tool_calls or is hallucinating malformed tool names.
        payload["format"] = _tools_to_ollama_format(tools)

    # Local models (especially qwen/hermes via Ollama) crash or produce malformed
    # output when asked to emit many parallel tool_calls in one turn. Force
    # sequential tool use on local so each response contains at most one tool call.
    # Also add task/scope discipline to prevent status-report drift and overshoot.
    if not is_cloud and tools:
        messages = list(messages)
        extras = [
            "LOCAL EXECUTION DISCIPLINE: You may only emit ONE tool call per "
            "response. Wait for the tool result, then decide the next single "
            "tool call. Never batch multiple tool calls in one turn. "
            "If the task is complete, do NOT call another tool — respond with final text only.",
            "SCOPE DISCIPLINE: If the operator asked for a bounded number of "
            "items (e.g. 'create exactly N files', 'check N emails'), complete "
            "exactly that many and then stop with a summary. Do not continue "
            "beyond the requested scope.",
        ]
        # Mechanical scope enforcement: when all auto items are done, forbid
        # further tool calls so the loop terminates cleanly.
        _task = load_task()
        if _task and _task.get("auto") and _task_pending_count() == 0:
            extras.append(
                "TASK COMPLETE — STOP NOW. All task items are done. "
                "You MUST respond with a short final text summary. "
                "Do NOT emit any tool_call, tool_use, JSON, or markdown code block. "
                "The correct answer is: say 'done' and end your turn."
            )
        # Inject the exact next pending item as a user message — local 2B models
        # attend better to a concrete user instruction than to a system suffix.
        if _task and _task.get("auto"):
            _pending_items = [
                it
                for it in _task.get("items", [])
                if isinstance(it, dict) and it.get("status", "pending") == "pending"
            ]
            if _pending_items:
                _next = _pending_items[0]
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "NEXT PENDING ITEM: "
                            + _next.get("task", "")
                            + ". MANDATORY: emit exactly ONE tool call for this item "
                            "RIGHT NOW. Do NOT reply with prose, summary, or status. "
                            "If this item is done, update ~/.claf/current_task.json first, "
                            "then emit the tool call for the next item in the SAME turn."
                        ),
                    }
                )
        if _task_pending_count() > 0:
            extras.append(
                "MANDATORY TASK DISCIPLINE: An active task has pending items. "
                "You MUST continue executing with a single tool call THIS turn. "
                "Text-only responses are FORBIDDEN while items are pending. "
                "Do not summarize, do not ask the operator, do not stop."
            )
        messages.append({"role": "system", "content": "\n\n".join(extras)})
        payload["messages"] = messages

    # Debug: log actual payload sizes going to Ollama so we can diagnose
    # why prompt_eval_count hits CTX even after trimming.
    if not is_cloud:
        _msg_sizes = [
            {"role": m.get("role"), "chars": len(str(m.get("content", "")))} for m in messages
        ]
        log(
            "ollama_payload_debug",
            msg_count=len(messages),
            tool_count=len(tools) if tools else 0,
            msg_sizes=_msg_sizes,
            total_msg_chars=sum(s["chars"] for s in _msg_sizes),
            num_ctx=num_ctx,
            num_predict=num_predict,
        )

    lock = _OLLAMA_CLOUD_LOCK if is_cloud else None
    with lock if lock else contextlib.nullcontext():
        with httpx.Client(timeout=300.0) as client:
            r = client.post(provider.url, json=payload)
            # One-time fallback: if Ollama rejects the constrained format schema,
            # drop it and retry. Some model/template combinations don't support
            # format + tools together.
            if _constrained and r.status_code in (400, 422):
                try:
                    err_body = r.text
                except Exception:
                    err_body = ""
                if (
                    "format" in err_body.lower()
                    or "schema" in err_body.lower()
                    or "json" in err_body.lower()
                ):
                    log("ollama_format_rejected", model=provider.model, error=err_body[:200])
                    payload.pop("format", None)
                    r = client.post(provider.url, json=payload)
            r.raise_for_status()
        data = r.json()

    msg = data.get("message", {}) or {}
    tool_calls = msg.get("tool_calls") or []
    content_text = msg.get("content", "") or ""
    thinking = msg.get("thinking", "") or ""

    # When constrained decoding is active, Ollama may return the tool call as
    # raw JSON text matching the format schema rather than native tool_calls.
    # Convert it so the rest of the pipeline sees a normal tool_use block.
    if _constrained and content_text and not tool_calls:
        try:
            parsed = json.loads(content_text.strip())
            if isinstance(parsed, dict) and "name" in parsed and "arguments" in parsed:
                tool_calls = [
                    {
                        "function": {
                            "name": parsed["name"],
                            "arguments": parsed["arguments"],
                        }
                    }
                ]
                log("tool_call_from_format", name=parsed["name"], model=provider.model)
        except Exception:
            pass

    # Hermes/qwen sometimes emit tool calls as malformed JSON text instead of
    # native tool_calls. Try aggressive repair first, then the cleaner markup.
    if not tool_calls and content_text and tools:
        repaired = _repair_malformed_tool_json(content_text, tools, provider.model)
        if repaired:
            msg = dict(msg)
            msg["tool_calls"] = repaired
            tool_calls = repaired
            log("tool_call_repaired", count=len(repaired), model=provider.model)

    # Thinking-mode fallback: some qwen builds emit tool calls as plain text
    # [Tool call: name({...})] instead of native tool_calls. Recover those.
    # Only run when tools were actually sent, otherwise prose gets mis-parsed.
    if not tool_calls and content_text and tools:
        _tc_pat = re.compile(r"\[Tool [Cc]all:\s*(\w+)\((\{.*?\})\)\]", re.DOTALL)
        recovered = []
        for m2 in _tc_pat.finditer(content_text):
            try:
                recovered.append(
                    {"function": {"name": m2.group(1), "arguments": json.loads(m2.group(2))}}
                )
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


def openai_compat_chat(
    provider, messages: list[dict], tools: list[dict] | None = None
) -> tuple[list[dict], dict, bool]:
    """OpenAI-compatible chat completions (Groq / Cerebras / Fireworks /
    OpenRouter). Sends native tools when present and reads tool_calls back as
    Anthropic tool_use blocks. Returns (content_blocks, usage, tool_use_bool)."""
    key = os.environ.get(provider.env_key or "", "")
    if provider.env_key and not key:
        raise RuntimeError(f"{provider.name}: env var {provider.env_key} not set")
    # keyless providers (e.g. opencode-free) pass through with empty Authorization
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
    headers = {"Authorization": f"Bearer {key.strip()}", "Content-Type": "application/json"} if key else {"Content-Type": "application/json"}
    # OpenRouter requires/appends these headers for proper request routing and
    # site attribution; without them it can return 401/403 on some keys.
    if provider.name == "openrouter":
        headers.setdefault("HTTP-Referer", "https://claf.local")
        headers.setdefault("X-Title", "CLAF")
    # Body size is the 413 signal — log it so payload-too-large is diagnosable
    # without guessing. Cloud free tiers (groq ~30KB) reject oversized bodies.
    _body_bytes = len(json.dumps(payload).encode("utf-8"))
    log(
        "cloud_request_size",
        provider=provider.name,
        body_bytes=_body_bytes,
        tool_count=len(tools) if tools else 0,
        msg_count=len(messages),
    )
    with httpx.Client(timeout=120.0) as client:
        r = client.post(provider.url, json=payload, headers=headers)
        if not r.is_success:
            log(
                "cloud_provider_error",
                provider=provider.name,
                model=provider.model,
                status=r.status_code,
                error_body=r.text[:800],
                payload_keys=list(payload.keys()),
                tool_count=len(tools) if tools else 0,
            )
        r.raise_for_status()
        data = r.json()
    message = data["choices"][0]["message"]
    blocks, tool_use = openai_tool_calls_to_anthropic(message, available_tools=tools)
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
    }
    with httpx.Client(timeout=300.0) as client:
        r = client.post(provider.url, json=payload, headers=headers)
        if not r.is_success:
            log(
                "anthropic_direct_error",
                status=r.status_code,
                error_body=r.text[:500],
                payload_keys=list(payload.keys()),
                betas=payload.get("betas"),
                thinking_type=(
                    payload.get("thinking", {}).get("type")
                    if isinstance(payload.get("thinking"), dict)
                    else payload.get("thinking")
                ),
                tool_count=len(payload.get("tools") or []),
            )
        r.raise_for_status()
        data = r.json()
    # Anthropic returns native content blocks (text + tool_use); pass them
    # through unchanged so Claude Code's native tool dispatcher fires.
    content_blocks = data.get("content", []) or []
    usage = {
        "input_tokens": data.get("usage", {}).get("input_tokens", 0),
        "output_tokens": data.get("usage", {}).get("output_tokens", 0),
    }
    # Warn when Anthropic returns a suspiciously empty response (1-token /
    # 0-char) so the root cause is visible in the log rather than silent.
    _out_text = "".join(
        b.get("text", "") for b in content_blocks if isinstance(b, dict) and b.get("type") == "text"
    )
    if usage["output_tokens"] <= 2 and not _out_text:
        log(
            "anthropic_empty_response",
            output_tokens=usage["output_tokens"],
            stop_reason=data.get("stop_reason"),
            content_block_types=[b.get("type") for b in content_blocks if isinstance(b, dict)],
            max_tokens_sent=payload.get("max_tokens"),
            model=payload.get("model"),
            tool_count=len(payload.get("tools") or []),
            message_count=len(payload.get("messages") or []),
        )
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
        val = (
            m.group(2)
            if m.group(2) is not None
            else (m.group(3) if m.group(3) is not None else m.group(4))
        )
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

    # Pass 4: <function=ToolName>{...}</function> XML-tag format.
    # gpt-oss-120b (Cerebras) falls back to this when native tool_calls don't
    # fire — JSON args are embedded between XML function tags in the content.
    _FN_TAG_RE = re.compile(
        r"<function=([A-Za-z_][A-Za-z0-9_.:-]*)>\s*(.*?)\s*</function>",
        re.DOTALL,
    )
    for _m in _FN_TAG_RE.finditer(text):
        _fname = _m.group(1)
        _fargs_raw = _m.group(2).strip()
        _tool = _find_tool(available_tools, _fname)
        if not _tool:
            continue
        try:
            _fargs = json.loads(_fargs_raw) if _fargs_raw else {}
        except Exception:
            continue
        found.append(
            (
                _m.start(),
                _m.end(),
                {
                    "type": "tool_use",
                    "id": f"toolu_claf_{uuid.uuid4().hex[:24]}",
                    "name": _tool["name"],
                    "input": _fargs,
                },
            )
        )

    # Pass 5: bare "TOOL key=value ..." with NO parens. Cloud-backup peers
    # (groq, gpt-oss-120b) emit tool calls as plain text when native tool_calls
    # don't fire, e.g. `mcp__sensei__tab_create url=https://x.com`. Without this
    # the scraper returns text-only, the giveup interceptor fires, and a whole
    # redispatch is burned. Real sample found in orchestrator.log 2026-06-11
    # (giveup_detected_forcing_replan, provider=groq). The _find_tool gate keeps
    # prose like "x = 5" from false-matching — the first token must resolve to a
    # real tool. De-dup at the end drops overlap with the paren form (Pass 2).
    _BARE_KV_LINE = re.compile(
        r"^[ \t]*([A-Za-z_][A-Za-z0-9_.:-]*)[ \t]+(\S+[ \t]*=.*\S)[ \t]*$", re.M
    )
    for _bm in _BARE_KV_LINE.finditer(text):
        _bname, _bargs = _bm.group(1), _bm.group(2)
        _btool = _find_tool(available_tools, _bname)
        if not _btool:
            continue
        _bkv = _kv_args(_bargs)
        if not _bkv:
            continue
        found.append(
            (
                _bm.start(),
                _bm.end(),
                {
                    "type": "tool_use",
                    "id": f"toolu_claf_{uuid.uuid4().hex[:24]}",
                    "name": _btool["name"],
                    "input": _bkv,
                },
            )
        )

    # Pass 6: bare JSON tool-call objects in text, e.g.
    # {"name": "mcp__sensei__screenshot", "arguments": {...}}
    # Hermes-class models emit their native ChatML tool-call JSON as plain
    # content when Ollama's tool binding doesn't fire — found live in the
    # 2026-06-11 Madam bake-off (hermes3:3b p2: picked the right tool, emitted
    # it as text, scored CAPABILITY=N). raw_decode from each '{"name"'
    # candidate gives balanced-brace parsing; works inside <tool_call> wrappers.
    _decoder = json.JSONDecoder()
    for _jm in re.finditer(r'\{\s*"name"\s*:', text):
        _jstart = _jm.start()
        try:
            _jobj, _jconsumed = _decoder.raw_decode(text[_jstart:])
        except Exception:
            continue
        if not isinstance(_jobj, dict):
            continue
        _jname = _jobj.get("name")
        _jtool = _find_tool(available_tools, _jname) if isinstance(_jname, str) else None
        if not _jtool:
            continue
        _jargs = next(
            (
                _jobj[k]
                for k in ("arguments", "parameters", "input")
                if isinstance(_jobj.get(k), dict)
            ),
            {},
        )
        # Hermes emits placeholder {"": ""} when it has no args — strip empties.
        _jargs = {k: v for k, v in _jargs.items() if k}
        found.append(
            (
                _jstart,
                _jstart + _jconsumed,
                {
                    "type": "tool_use",
                    "id": f"toolu_claf_{uuid.uuid4().hex[:24]}",
                    "name": _jtool["name"],
                    "input": _jargs,
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


def wrap_anthropic_response(
    model_id: str, content_blocks: list, usage: dict, tool_use: bool
) -> dict:
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
        out.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {}) or {},
            }
        )
    return out


def _openai_to_anthropic(body: dict) -> dict:
    return {
        "model": body.get("model", "claude-sonnet-4-6"),
        "messages": body.get("messages", []),
        "tools": _openai_tools_to_anthropic(body.get("tools")),
        "max_tokens": body.get("max_tokens", 1024),
        "stream": body.get("stream", False),
        "system": body.get("system", ""),
    }


def _anthropic_to_openai(anthropic_resp: dict, model: str) -> dict:
    content_blocks = anthropic_resp.get("content", []) or []
    text_parts = []
    tool_calls = []
    for b in content_blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            text_parts.append(b.get("text", ""))
        elif isinstance(b, dict) and b.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": b.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": b.get("name", ""),
                        "arguments": json.dumps(b.get("input", {}) or {}),
                    },
                }
            )
    text = "".join(text_parts)
    stop_reason = anthropic_resp.get("stop_reason", "end_turn")
    finish_reason = (
        "stop"
        if stop_reason == "end_turn"
        else "tool_calls" if stop_reason == "tool_use" else stop_reason
    )
    usage = anthropic_resp.get("usage", {}) or {}
    return {
        "id": f"chatcmpl_{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": text or None,
                    "tool_calls": tool_calls if tool_calls else None,
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    }


@app.get("/")
def root():
    return {
        "name": "claf-orchestrator",
        "version": "0.4.0",
        "local_model": LOCAL_MODEL,
        "mode": MODE,
    }


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
                slot = by_tier.setdefault(
                    tier, {"name": name, "calls": 0, "input_tokens": 0, "output_tokens": 0}
                )
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
        "claude-opus-4-7",
        "claude-opus-4-7[1m]",
        "claude-opus-4-6",
        "claude-opus-4-5",
        "claude-sonnet-4-7",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
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
        data.append(
            {
                "id": mid,
                "type": "model",
                "display_name": (
                    f"local:{mid}" if mid == LOCAL_MODEL else f"claf:{mid} (→ {routed_label})"
                ),
                "created_at": "2026-05-19T00:00:00Z",
            }
        )

    return {"object": "list", "data": data}


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
    _msgs = messages_from_anthropic(
        anthropic_body.get("messages", []),
        flavor="ollama" if provider.kind == "ollama" else "openai",
    )
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
            _text = "".join(
                b.get("text", "")
                for b in _blocks
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if _text:
                _new_text = execute_actions_in_text(_text)
                if _new_text != _text:
                    # Replace the text block with the augmented version
                    _blocks = [{"type": "text", "text": _new_text}] + [
                        b for b in _blocks if not (isinstance(b, dict) and b.get("type") == "text")
                    ]

        anthropic_resp = wrap_anthropic_response(requested_model, _blocks, _usage, _tool_use)
    except Exception as e:
        log("openai_endpoint_error", error=str(e))
        return JSONResponse(
            status_code=502,
            content={"error": {"type": "api_error", "message": str(e)}},
        )

    openai_resp = _anthropic_to_openai(anthropic_resp, requested_model)
    return openai_resp


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

    yield _event(
        "message_start",
        {
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
        },
    )
    yield _event("ping", {"type": "ping"})

    for i, block in enumerate(content_blocks):
        btype = block.get("type", "text")
        if btype == "text":
            yield _event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": i,
                    "content_block": {"type": "text", "text": ""},
                },
            )
            yield _event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {"type": "text_delta", "text": block.get("text", "")},
                },
            )
        elif btype == "tool_use":
            yield _event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": i,
                    "content_block": {
                        "type": "tool_use",
                        "id": block.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
                        "name": block.get("name", ""),
                        "input": {},
                    },
                },
            )
            yield _event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": i,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(block.get("input", {}) or {}),
                    },
                },
            )
        yield _event(
            "content_block_stop",
            {
                "type": "content_block_stop",
                "index": i,
            },
        )

    yield _event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": stop_sequence},
            "usage": {"output_tokens": usage.get("output_tokens", 0)},
        },
    )
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
    polish_prompt = template.format(
        snippet=f"INTENT: {prompt_text[:300]}\n\nDRAFT:\n```\n{snippet}\n```"
    )

    try:
        # Tap polish NEVER uses direct Anthropic billing (kind="anthropic",
        # tier 9). openai_compat only: groq(2), cerebras(3), fireworks(6),
        # openrouter(7), ollama-cloud-coder(1). Console key stays untouched.
        peer = pick_cloud_peer(
            prefer_tiers=(2, 3, 6, 7, 1),
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
                b.get("text", "")
                for b in polished_blocks
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
    log(
        "tap_polish_ok",
        intent=intent,
        peer=peer.name,
        before=len(snippet),
        after=len(polished_block),
    )
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
def _count_tool_cycles_since_reset(msgs: list) -> int:
    """Count assistant tool_use turns after the most recent [CLAF-LOOP-RESET] marker.
    This gives the per-epoch count so the cap resets after each replan injection."""
    last_reset = -1
    for i, m in enumerate(msgs):
        c = m.get("content", "")
        if isinstance(c, list):
            c = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
        if m.get("role") == "user" and "[CLAF-LOOP-RESET" in str(c):
            last_reset = i
    count = 0
    for m in msgs[last_reset + 1 :]:
        if m.get("role") == "assistant":
            ct = m.get("content", [])
            if isinstance(ct, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_use" for b in ct
            ):
                count += 1
    return count


def _count_replan_epochs(msgs: list) -> int:
    """Count how many [CLAF-LOOP-RESET] markers exist in the history."""
    return sum(
        1
        for m in msgs
        if m.get("role") == "user" and "[CLAF-LOOP-RESET" in str(m.get("content", ""))
    )


class _LoopState(str, enum.Enum):
    """Linear states for the tool-call loop state machine."""

    IDLE = "idle"
    ACTING = "acting"  # assistant emitted tool_use, waiting for result
    OBSERVING = "observing"  # last message carries tool_result(s)
    SUMMARIZING = "summarizing"  # per-epoch turn cap hit; compress + reset
    PAUSED = "paused"  # hard cap / max epochs exhausted; ask operator
    DONE = "done"  # no tools or task complete


def _derive_loop_state(
    msgs: list,
    tools: list[dict] | None,
    _task_pending: int | None = None,
) -> tuple[_LoopState, dict]:
    """Return the current tool-loop state plus diagnostic counts.

    The state machine is the single source of truth for deciding whether to
    dispatch to a model, compress context, or stop and ask the operator.
    """
    _max_loop_turns = int(os.environ.get("CLAF_MAX_LOOP_TURNS", "12") or "12")
    _max_epochs = int(os.environ.get("CLAF_MAX_REPLAN_EPOCHS", "1") or "1")
    _max_total = int(os.environ.get("CLAF_MAX_TOTAL_TOOL_TURNS", "24") or "24")

    total_cycles = 0
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        ct = m.get("content", [])
        if isinstance(ct, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_use" for b in ct
        ):
            total_cycles += 1

    cycles_since_reset = _count_tool_cycles_since_reset(msgs)
    epochs = _count_replan_epochs(msgs)

    info = {
        "total_cycles": total_cycles,
        "cycles_since_reset": cycles_since_reset,
        "epochs": epochs,
        "max_loop_turns": _max_loop_turns,
        "max_epochs": _max_epochs,
        "max_total": _max_total,
    }

    if not tools:
        return _LoopState.DONE, info

    if _task_pending is not None and _task_pending == 0:
        return _LoopState.DONE, info

    # Absolute hard stop: never exceed the total tool-turn budget.
    if _max_total > 0 and total_cycles >= _max_total:
        return _LoopState.PAUSED, info

    # Per-epoch cap: when we hit it, either reset+compress once or stop.
    if _max_loop_turns > 0 and cycles_since_reset >= _max_loop_turns:
        if epochs >= _max_epochs:
            return _LoopState.PAUSED, info
        return _LoopState.SUMMARIZING, info

    # Transition based on the most recent message.
    if not msgs:
        return _LoopState.IDLE, info
    last = msgs[-1]
    role = last.get("role")
    content = last.get("content", [])
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]

    if role == "assistant" and any(
        isinstance(b, dict) and b.get("type") == "tool_use" for b in content
    ):
        return _LoopState.ACTING, info

    if role == "user" and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    ):
        return _LoopState.OBSERVING, info

    return _LoopState.IDLE, info


def _compress_loop_history(
    msgs: list[dict],
    keep_recent_pairs: int = 2,
    max_tool_result_chars: int = 400,
) -> list[dict]:
    """Return a shallow copy of `msgs` with older tool-result pairs compressed.

    Keeps the most recent `keep_recent_pairs` assistant tool_use + user
    tool_result pairs intact. Older pairs are summarized so the 8K local
    context window does not explode during long sequential loops.
    """
    if not msgs:
        return msgs

    # Identify indices of assistant messages that contain tool_use blocks.
    tool_use_indices: list[int] = []
    tool_use_ids: set[str] = set()
    for i, m in enumerate(msgs):
        if m.get("role") != "assistant":
            continue
        ct = m.get("content", [])
        if not isinstance(ct, list):
            continue
        for b in ct:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                tool_use_ids.add(b.get("id", ""))
                tool_use_indices.append(i)
                break

    # Pair each tool_use with the following user tool_result(s).
    pairs: list[tuple[int, int]] = []
    for tu_idx in tool_use_indices:
        for j in range(tu_idx + 1, len(msgs)):
            m = msgs[j]
            if m.get("role") != "user":
                continue
            ct = m.get("content", [])
            if not isinstance(ct, list):
                continue
            if any(
                isinstance(b, dict)
                and b.get("type") == "tool_result"
                and b.get("tool_use_id") in tool_use_ids
                for b in ct
            ):
                pairs.append((tu_idx, j))
                break

    if len(pairs) <= keep_recent_pairs:
        return list(msgs)

    keep_set = set()
    for tu_idx, tr_idx in pairs[-keep_recent_pairs:]:
        keep_set.add(tu_idx)
        keep_set.add(tr_idx)

    out: list[dict] = []
    for i, m in enumerate(msgs):
        if i in keep_set or m.get("role") != "user":
            out.append(m)
            continue

        ct = m.get("content", [])
        if not isinstance(ct, list):
            out.append(m)
            continue

        new_blocks = []
        compressed_any = False
        for b in ct:
            if (
                isinstance(b, dict)
                and b.get("type") == "tool_result"
                and b.get("tool_use_id") in tool_use_ids
            ):
                raw = ""
                if isinstance(b.get("content"), str):
                    raw = b["content"]
                elif isinstance(b.get("content"), list):
                    raw = " ".join(x.get("text", "") for x in b["content"] if isinstance(x, dict))
                snippet = raw[:max_tool_result_chars].replace("\n", " ")
                if len(raw) > max_tool_result_chars:
                    snippet += " …"
                name = "tool"
                for tu in msgs:
                    tuc = tu.get("content", [])
                    if not isinstance(tuc, list):
                        continue
                    for tub in tuc:
                        if (
                            isinstance(tub, dict)
                            and tub.get("type") == "tool_use"
                            and tub.get("id") == b.get("tool_use_id")
                        ):
                            name = tub.get("name", name)
                            break
                new_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": b.get("tool_use_id", ""),
                        "content": f"[summarized {name} result]: {snippet}",
                    }
                )
                compressed_any = True
            else:
                new_blocks.append(b)

        if compressed_any:
            out.append({**m, "content": new_blocks})
        else:
            out.append(m)

    return out


def _parse_bounded_file_task(text: str) -> list[dict] | None:
    """Detect 'create/write exactly N files' patterns and expand to explicit items.

    Supports path/content templates with a single 'N' placeholder, e.g.:
      'create exactly 5 files /tmp/x/file_N.txt containing step N'
    Returns a list of items with auto_done criteria, or None if no pattern matches.
    """
    text_lower = text.lower()
    m = re.search(
        r"(?:create|write|make)\s+(?:exactly\s+)?(\d+)\s+(?:numbered\s+)?files?", text_lower
    )
    if not m:
        return None
    n = int(m.group(1))
    if n <= 0 or n > 1000:
        return None
    # Look for a path template containing N or file_N
    path_tmpl = None
    # First try to find an explicit path with _N or {N}
    pm = re.search(r"(/[\w\-./{}]+[_N](?:\.[\w]+)?)", text)
    if pm:
        path_tmpl = pm.group(1)
    else:
        # Fallback: any absolute-looking path
        pm2 = re.search(r"(/[\w\-./]+/)([\w\-{}N_]+\.[\w]+)", text)
        if pm2:
            path_tmpl = pm2.group(1) + pm2.group(2)
    if not path_tmpl:
        path_tmpl = "/tmp/claf_files/file_N.txt"
    # Content template: look for quoted 'step N', 'file N', or just 'N'
    cm = re.search(r"['\"]([^'\"]*N[^'\"]*)['\"]", text)
    content_tmpl = cm.group(1) if cm else "step N"
    items = []
    for i in range(1, n + 1):
        p = path_tmpl.replace("{N}", str(i)).replace("_N", f"_{i}").replace("N", str(i))
        c = content_tmpl.replace("{N}", str(i)).replace("N", str(i))
        items.append(
            {
                "id": i,
                "task": f"Write {p} with content {c!r}",
                "status": "pending",
                "auto_done": {"tool": "Write", "file_path": p, "content": c},
            }
        )
    return items


def _enforce_auto_task_scope(content_blocks: list[dict]) -> list[dict]:
    """Mechanical safety net for bounded auto tasks.

    - If an auto task has pending items and the model emits a Write call that
      does NOT match the next pending item, rewrite it to the correct
      path/content so the task advances.
    - If an auto task has NO pending items, the task is complete: strip any
      tool_use blocks and force a text-only final response so the model cannot
      overshoot the requested scope.
    """
    task = load_task()
    if not task or not task.get("auto"):
        return content_blocks
    pending = [
        it
        for it in task.get("items", [])
        if isinstance(it, dict) and it.get("status", "pending") == "pending" and it.get("auto_done")
    ]
    # Task complete — mechanically stop any further tool calls.
    if not pending:
        has_tool = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content_blocks)
        if has_tool:
            text_blocks = [
                b for b in content_blocks if isinstance(b, dict) and b.get("type") == "text"
            ]
            raw_text = " ".join(b.get("text", "") for b in text_blocks).strip()
            # If the model emitted malformed tool JSON as text, replace it with
            # a clean completion summary instead of echoing garbage.
            if raw_text and "{" not in raw_text and len(raw_text) < 200:
                summary = raw_text
            else:
                total = len(task.get("items", []))
                summary = f"Task complete. {total} item(s) finished. done"
            log("auto_task_scope_stop", stripped_tools=True, summary=summary[:80])
            return [{"type": "text", "text": summary}]
        return content_blocks
    # Task in progress — enforce the next pending item.
    next_item = pending[0]
    ad = next_item.get("auto_done", {})
    if ad.get("tool") != "Write":
        return content_blocks
    exp_path = ad.get("file_path", "")
    exp_content = ad.get("content", "")
    changed = False
    out = []
    for block in content_blocks:
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("name") == "Write"
        ):
            inp = block.get("input", {})
            actual_path = inp.get("file_path", "")
            actual_content = inp.get("content", "")
            if actual_path != exp_path or actual_content != exp_content:
                block = dict(block)
                block["input"] = {"file_path": exp_path, "content": exp_content}
                changed = True
        out.append(block)
    if changed:
        log("auto_task_scope_rewrite", expected_path=exp_path, expected_content=exp_content)
    return out


def _auto_resolve_task_items(body: dict) -> bool:
    """Scan incoming messages for tool_results that match pending auto_done items.

    This lets the orchestrator track progress for bounded auto-seeded tasks
    instead of relying on the 3B model to update ~/.claf/current_task.json.
    Returns True if any item was updated.
    """
    task = load_task()
    if not task:
        return False
    items = task.get("items", [])
    if not any(isinstance(it, dict) and it.get("auto_done") for it in items):
        return False
    # Gather Write tool results from message history
    done_paths = {}  # path -> content
    for msg in body.get("messages", []) or []:
        content = msg.get("content", [])
        if isinstance(content, str):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            tc_id = block.get("tool_use_id", "")
            result_text = ""
            if isinstance(block.get("content"), str):
                result_text = block["content"]
            elif isinstance(block.get("content"), list):
                result_text = "\n".join(
                    c.get("text", "") for c in block["content"] if isinstance(c, dict)
                )
            # Try to extract path from result text like "Wrote N chars to /path"
            wm = re.search(r"Wrote \d+ chars? to (\S+)", result_text)
            if not wm:
                continue
            path = wm.group(1)
            # Find the matching tool_use in history to get content
            content_val = None
            for m2 in body.get("messages", []) or []:
                c2 = m2.get("content", [])
                if isinstance(c2, str):
                    continue
                for b2 in c2:
                    if (
                        isinstance(b2, dict)
                        and b2.get("type") == "tool_use"
                        and b2.get("id") == tc_id
                    ):
                        inp = b2.get("input", {})
                        content_val = inp.get("content")
                        break
                if content_val is not None:
                    break
            if path and content_val is not None:
                done_paths[path] = content_val
    changed = False
    for it in items:
        if not isinstance(it, dict) or it.get("status") != "pending":
            continue
        ad = it.get("auto_done")
        if not isinstance(ad, dict):
            continue
        if ad.get("tool") != "Write":
            continue
        exp_path = ad.get("file_path", "")
        exp_content = ad.get("content", "")
        if exp_path in done_paths and done_paths[exp_path] == exp_content:
            it["status"] = "done"
            changed = True
    if changed:
        save_task(task)
    return changed


def _task_pending_count() -> int:
    """Ground-truth count of UNRESOLVED items in the active task file
    (~/.claf/current_task.json). Resolved = done/failed/skip; anything else
    (incl. 'pending' or a missing/garbled status) counts as still-to-do.
    Returns 0 when no task file is active — the signal the loop is free to end."""
    task = load_task()
    if not task:
        return 0
    _resolved = {"done", "failed", "skip"}
    return sum(
        1
        for it in task.get("items", [])
        if isinstance(it, dict)
        and str(it.get("status", "pending")).strip().lower() not in _resolved
    )


def _looks_agentic_task(text: str) -> bool:
    """Return True if the operator's text looks like a multi-step or agentic
    workflow that should have a task file. Simple commands and questions should
    return False so the orchestrator does not manufacture fake pending work.
    """
    t = text.lower()
    # Bounded file creation — proven local automation path.
    if _parse_bounded_file_task(text):
        return True
    # Explicit agentic / task keywords.
    agentic_signals = (
        "create a task",
        "make a task",
        "do this task",
        "automate this",
        "run this workflow",
        "every day",
        "every morning",
        "every night",
        "on a schedule",
        "repeat this",
        "for each of",
        "for every",
    )
    if any(sig in t for sig in agentic_signals):
        return True
    # Numbered lists with action verbs look like step-by-step instructions.
    if re.search(r"\b\d+\.\s+(create|write|run|check|open|send|read|edit|delete|make)\b", t):
        return True
    return False


def _auto_seed_task(body: dict, conv_fp: str) -> bool:
    """Seed ~/.claf/current_task.json from the operator's instruction when an
    agentic request starts and no task file exists. The 3B local model does not
    reliably WRITE the file the charter asks for — and the continuation guard
    can't protect a file that never exists. Seeding from the orchestrator makes
    the ground truth unconditional. Marked auto:true so the guard can clean it
    up when the model insists the work is finished (model-written files are
    never auto-deleted). Returns True if a file was written."""
    if not body.get("tools"):
        return False  # not an agentic request
    msgs = body.get("messages", []) or []
    if not msgs:
        return False
    last = msgs[-1]
    if last.get("role") != "user":
        return False
    c = last.get("content")
    if isinstance(c, str):
        text = c
    elif isinstance(c, list):
        # A genuine operator turn is text-only; tool_result blocks mean we are
        # mid-loop and the task (if any) already exists or the model owns it.
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return False
        text = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
    else:
        return False
    text = text.strip()
    if len(text) < 20 or "[CLAF-" in text:
        return False  # too short to be a task, or our own injected marker
    if not _looks_agentic_task(text):
        return False  # simple command/question — no fake task
    existing = load_task()
    if existing:
        if not existing.get("auto"):
            return False  # model-written file — never clobber
        # If the new request is a bounded file task, reseed whenever the
        # expected paths/content differ. This prevents a stale auto task from
        # steering the model toward old files when the operator starts a new
        # bounded task within the TTL window.
        _bounded_new = _parse_bounded_file_task(text)
        if _bounded_new:
            _existing_items = [
                it
                for it in existing.get("items", [])
                if isinstance(it, dict) and it.get("auto_done")
            ]
            if len(_bounded_new) != len(_existing_items):
                pass  # counts differ → reseed below
            elif _bounded_new and _existing_items:
                _new_keys = {
                    (it["auto_done"]["file_path"], it["auto_done"]["content"])
                    for it in _bounded_new
                }
                _old_keys = {
                    (it["auto_done"]["file_path"], it["auto_done"]["content"])
                    for it in _existing_items
                }
                if _new_keys == _old_keys:
                    return False  # same bounded task still running
            # otherwise fall through to reseed
        else:
            try:
                import time as _t

                ttl_min = int(os.environ.get("CLAF_AUTO_TASK_TTL_MIN", "30"))
                if _t.time() - TASK_FILE.stat().st_mtime < ttl_min * 60:
                    return False  # fresh auto task — same task still running
            except OSError:
                pass  # stat failed → treat as stale, reseed
    goal = text[:200]
    bounded_items = _parse_bounded_file_task(text)
    if bounded_items:
        save_task({"goal": goal, "auto": True, "conv_fp": conv_fp, "items": bounded_items})
        log(
            "auto_task_seeded",
            goal_chars=len(goal),
            conv_fp=conv_fp,
            replaced_stale=bool(existing),
            bounded_items=len(bounded_items),
        )
        return True
    save_task(
        {
            "goal": goal,
            "auto": True,
            "conv_fp": conv_fp,
            "items": [{"id": 1, "task": goal, "status": "pending"}],
        }
    )
    log("auto_task_seeded", goal_chars=len(goal), conv_fp=conv_fp, replaced_stale=bool(existing))
    return True


def _trim_for_local(system_text: str, msgs: list[dict]) -> tuple[str, list[dict], dict]:
    info = {"trimmed": False}
    if os.environ.get("CLAF_LOCAL_TRIM", "1") == "0":
        return system_text, msgs, info
    max_sys = int(os.environ.get("CLAF_LOCAL_SYS_MAX_CHARS", "1500"))
    max_msgs = int(os.environ.get("CLAF_LOCAL_MAX_MSGS", "10"))
    # Per-message content cap. The UserPromptSubmit hook injects MEMORY.md +
    # session snapshot into user messages — on an 8K-ctx local model that single
    # injection can fill the entire window before the task even starts. Truncate
    # each message's string content to keep total token budget sane.
    # Default 1500 chars/msg: 5 msgs × 1500 = 7500 chars ≈ ~1900 tokens, leaving
    # headroom for system prompt + tool schemas + output.
    max_msg_chars = int(os.environ.get("CLAF_LOCAL_MSG_CONTENT_MAX", "1500"))
    sys_before = len(system_text or "")
    msgs_before = len(msgs)

    if system_text and len(system_text) > max_sys:
        # Keep the head — identity/role/standing instructions live at the top.
        system_text = system_text[:max_sys].rstrip() + "\n[…system prompt trimmed for local speed…]"

    if len(msgs) > max_msgs:
        msgs = msgs[-max_msgs:]
        # Drop leading orphaned assistant turns (no prior user/tool context).
        # Keep "tool" (converted tool_results) — valid Ollama starting point.
        # Never drop below 1 message so the model always has something to answer.
        while len(msgs) > 1 and msgs[0].get("role") == "assistant":
            msgs = msgs[1:]

    # Truncate per-message string content (catches hook-injected memory blocks).
    trimmed_content = False
    capped_msgs = []
    for m in msgs:
        c = m.get("content")
        if isinstance(c, str) and len(c) > max_msg_chars:
            # Head AND tail — the hook preamble fills the head; the operator's
            # actual words are at the end. Same fix as the cloud trim.
            half = max(max_msg_chars // 2 - 20, 50)
            m = dict(m, content=c[:half] + "\n[…msg trimmed for local ctx…]\n" + c[-half:])
            trimmed_content = True
        capped_msgs.append(m)
    msgs = capped_msgs

    if len(system_text or "") != sys_before or len(msgs) != msgs_before or trimmed_content:
        info = {
            "trimmed": True,
            "sys_chars_before": sys_before,
            "sys_chars_after": len(system_text or ""),
            "msgs_before": msgs_before,
            "msgs_after": len(msgs),
            "msg_content_capped": trimmed_content,
        }
    return system_text, msgs, info


@app.post("/v1/messages")
async def messages(request: Request):
    body = await request.json()
    turn = {
        "turn_id": uuid.uuid4().hex[:12],
        "t0": time.monotonic(),
        "marks": {},
        "dispatches": [],
        "redispatch_count": 0,
    }
    token = _claf_turn.set(turn)
    try:
        response = await _messages_impl(request, body, turn)
        return response
    finally:
        _claf_turn.reset(token)
        turn["total_ms"] = int((time.monotonic() - turn["t0"]) * 1000)
        log(
            "turn_summary",
            turn_id=turn["turn_id"],
            conv_fp=_conv_fingerprint(body.get("messages", [])),
            message_count=len(body.get("messages", [])),
            total_ms=turn["total_ms"],
            marks=turn["marks"],
            dispatches=turn["dispatches"],
            redispatch_count=turn["redispatch_count"],
            charter_stat_count=turn.get("charter_stat_count", 0),
            provider=turn.get("provider"),
            provider_pool=turn.get("provider_pool"),
            model=turn.get("model"),
            tool_use=turn.get("tool_use"),
            stream=body.get("stream", False),
            status=turn.get("status", "ok"),
        )


async def _messages_impl(request: Request, body: dict, turn: dict):
    _sys_probe = flatten_system(body.get("system"))
    _msgs_probe = body.get("messages", [])
    _last_user = ""
    for _m in reversed(_msgs_probe):
        if _m.get("role") == "user":
            _c = _m.get("content")
            _last_user = _c if isinstance(_c, str) else json.dumps(_c)
            break
    _mark("t_request_in")
    # Strip hook-injected blocks from prompt before snapshotting so the snippet
    # shows the actual operator intent, not the prepended STANDING ORDERS wall.
    import re as _re_req

    _prompt_clean = _last_user if isinstance(_last_user, str) else json.dumps(_last_user)
    for _hdr in (
        r"\[standing orders\][^\[]*",
        r"\[task_seed_required[^\]]*\][^\[]*",
        r"\[session snapshot\][^\[]*",
        r"\[heartbeat[^\]]*\][^\[]*",
        r"\[non-negotiables\][^\[]*",
        r"\[topology\][^\[]*",
        r"\[retry_schema[^\]]*\][^\[]*",
        r"\[open tasks[^\]]*\][^\[]*",
    ):
        _prompt_clean = _re_req.sub(_hdr, " ", _prompt_clean, flags=_re_req.DOTALL)
    _prompt_clean = " ".join(_prompt_clean.split())
    log(
        "request_in",
        turn_id=turn["turn_id"],
        model=body.get("model"),
        message_count=len(_msgs_probe),
        has_system=bool(body.get("system")),
        stream=body.get("stream", False),
        # Diagnostics: is the native memory + hook content actually arriving?
        sys_chars=len(_sys_probe),
        sys_has_claude_md=("STANDING ORDERS" in _sys_probe or "STARTUP ROUTINE" in _sys_probe),
        sys_has_memory=(
            "MEMORY.md" in _sys_probe or "auto-memory" in _sys_probe or "feedback_" in _sys_probe
        ),
        prompt_has_retry_hook=("RETRY_SCHEMA" in _last_user),
        prompt_snippet=_prompt_clean[:200],
    )
    log_conversation(
        turn["turn_id"],
        "user",
        _prompt_clean,
        model=body.get("model"),
    )

    # Auto-seed the task file at the start of an agentic task. The continuation
    # guard reads this as ground truth; without it, a local model that never
    # writes the file leaves the guard blind (observed live 2026-06-12 01:34).
    _conv_fp = _conv_fingerprint(body.get("messages", []))
    _auto_seed_task(body, _conv_fp)
    _auto_resolve_task_items(body)

    system_text = flatten_system(body.get("system"))
    messages = anthropic_to_ollama_messages(body.get("messages", []))
    if system_text:
        messages.insert(0, {"role": "system", "content": system_text})

    requested_model = body.get("model", "claude-sonnet-4-6")

    # Linear state machine for the long sequential tool-call loop.
    # Replaces the old nested cap logic with explicit states:
    #   IDLE / ACTING / OBSERVING / SUMMARIZING / PAUSED / DONE.
    _loop_state, _loop_info = _derive_loop_state(
        _msgs_probe,
        body.get("tools"),
        _task_pending_count(),
    )
    turn["loop_state"] = _loop_state.value
    turn["loop_info"] = _loop_info
    log("loop_state", state=_loop_state.value, **_loop_info)

    if _loop_state == _LoopState.PAUSED:
        # Hard cap or max replan epochs exhausted — stop and ask the operator.
        _stop_text = (
            f"[CLAF: {_loop_info['epochs']} replan epoch(s), "
            f"{_loop_info['total_cycles']} total tool turns. "
            "Context budget or iteration cap reached. What specific information or action do you "
            "need from the operator to finish? Ask ONE question.]"
        )
        _stop_resp = {
            "id": f"msg_loop_pause_{_loop_info['epochs']}_{_loop_info['total_cycles']}",
            "type": "message",
            "role": "assistant",
            "model": requested_model,
            "content": [{"type": "text", "text": _stop_text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": len(_stop_text.split())},
        }
        if body.get("stream"):
            return StreamingResponse(_sse_events(_stop_resp), media_type="text/event-stream")
        return JSONResponse(_stop_resp)

    # Per-step context compression: older tool_use/tool_result pairs are
    # summarized so the local 8K window doesn't explode on long loops.
    _compress_threshold = int(os.environ.get("CLAF_COMPRESS_AFTER_CYCLES", "2") or "2")
    if _loop_info["total_cycles"] > _compress_threshold:
        _compressed = _compress_loop_history(_msgs_probe)
        if _compressed != _msgs_probe:
            body["messages"] = _compressed
            log("loop_history_compressed", total_cycles=_loop_info["total_cycles"])

    if _loop_state == _LoopState.SUMMARIZING:
        # Inject a context-reset marker and let the model continue.
        _reset_content = (
            f"[CLAF-LOOP-RESET epoch={_loop_info['epochs'] + 1}/{_loop_info['max_epochs']}] "
            f"{_loop_info['cycles_since_reset']} tool turns used this epoch. "
            "Briefly summarize what you completed, then immediately continue toward the goal with your next tool call. Do NOT stop."
        )
        body["messages"] = list(body.get("messages", [])) + [
            {"role": "user", "content": _reset_content}
        ]
        log(
            "loop_replan_injected",
            epoch=_loop_info["epochs"] + 1,
            turns_this_epoch=_loop_info["cycles_since_reset"],
        )

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
                # Prefer tier-1 (Ollama Cloud: SSH-signed, no per-token billing,
                # not Anthropic-Tier-1-rate-limited). If tier-1 is unavailable,
                # degrade to LOCAL ONLY — do NOT fall through to paid cloud peers.
                # Paid Anthropic tiers are explicit escalation only (force_cloud/escalate).
                provider = pick_cloud_peer(prefer_tiers=(1,))
                if provider is None:
                    throttle.refund(trickle_reservation)
                    trickle_reservation = None
                    trickle_mode = "local"
                    trickle_degrade_note = throttle.degrade_message("flash")
                    log(
                        "trickle_flash_degraded_to_local",
                        scores=trickle_scores,
                        reason="no_cloud_peer",
                    )
                else:
                    log(
                        "trickle_flash_approved",
                        reservation=trickle_reservation,
                        emergency=emergency,
                        scores=trickle_scores,
                        provider=provider.name,
                    )
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
    routed_model = (
        select_local_model(body)
        if (provider.kind == "ollama" and provider.pool == "local")
        else provider.model
    )
    if routed_model != provider.model:
        from dataclasses import replace as _replace

        log(
            "dual_local_route",
            from_model=provider.model,
            to_model=routed_model,
            reason="image_in_request",
        )
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

    _mark("t_route")
    turn["provider"] = provider.name
    turn["provider_pool"] = provider.pool
    turn["model"] = provider.model
    log(
        "route_decision",
        turn_id=turn["turn_id"],
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
    #
    # Tier cap: flash = tier 1 only (free Groq-class peers); tap = tier 1-3
    # (no paid Anthropic); explicit escalation = unbounded. Without this, a
    # flash request cascading through Groq 429 → Cerebras 429 → OpenRouter 402
    # would land on Anthropic (paid tier 4), run the operator's Console budget,
    # and return 1 token when the body is malformed for that path.
    # Correction / negative feedback should never be handled by Groq, because
    # Groq is text-only and cannot fix the action. Cap fallback tier at 1
    # (Cerebras) when the user is correcting or rejecting a previous action.
    _CORRECTION_SIGNALS = [
        "not what i asked",
        "not what i meant",
        "not what i wanted",
        "that is not what",
        "that's not what",
        "this is not what",
        "you misunderstood",
        "you misread",
        "you missed",
        "do it again",
        "try again",
        "do that again",
        "that's wrong",
        "you're wrong",
        "is wrong",
        "was wrong",
        "no, i said",
        "i didn't ask",
        "i didn't mean",
        "i didn't want",
        "i meant",
        "i meant to",
        "i meant the",
        "actually i meant",
        "stop doing",
        "forget that",
        "back up",
        "undo that",
        "start over",
        "revert",
        "cancel that",
        "abort that",
    ]
    _prompt_text_lower = _flatten_prompt_text(body).lower()
    _is_correction = any(s in _prompt_text_lower for s in _CORRECTION_SIGNALS)
    if _is_correction:
        log("correction_detected", prompt_snippet=_prompt_text_lower[:120])
    _max_fallback_tier: int = (
        3
        if _is_correction  # allow OpenRouter so corrections can fix tool mistakes
        else (
            3
            if trickle_mode == "flash"  # Cerebras -> Groq -> OpenRouter before local
            else 3 if trickle_mode == "tap" else 999
        )  # explicit cloud escalation — all tiers allowed
    )
    # ── SERVER-SIDE TOOLBOX DISPATCH ─────────────────────────────────────────
    # Toolbox-matched commands bypass the 3B entirely. The model's xdg-open prior
    # and weak JSON emission make it unreliable for these deterministic tools.
    # Run the minted tool directly here; return the result as a clean response.
    _tb_tool = _matches_toolbox_command(body)
    if _tb_tool:
        try:
            import importlib.util as _ilu

            _tool_path = Path(__file__).resolve().parent / "toolbox" / f"{_tb_tool}.py"
            if _tool_path.exists():
                _spec = _ilu.spec_from_file_location(f"toolbox_{_tb_tool}", _tool_path)
                _mod = _ilu.module_from_spec(_spec)
                _spec.loader.exec_module(_mod)
                _tb_args: dict = {}
                _last_msg_raw = ""
                for _m in reversed(body.get("messages", [])):
                    if _m.get("role") == "user":
                        _c = _m.get("content")
                        _last_msg_raw = _c if isinstance(_c, str) else json.dumps(_c)
                        break
                import re as _re_tb

                _jm = _re_tb.search(r"\{[^{}]*\}", _last_msg_raw)
                if _jm:
                    try:
                        _tb_args = json.loads(_jm.group(0))
                    except Exception:
                        _tb_args = {}
                # Always pass raw user text so tools can parse positional args
                _tb_args.setdefault("_raw_command", _last_msg_raw)
                _result_text = _mod.run(_tb_args)
                log(
                    "toolbox_direct_dispatch",
                    tool=_tb_tool,
                    args=_tb_args,
                    result_len=len(_result_text),
                )
                _tb_resp = wrap_anthropic_response(
                    requested_model,
                    [{"type": "text", "text": _result_text}],
                    {"input_tokens": 0, "output_tokens": len(_result_text.split())},
                    False,
                )
                turn["status"] = turn.get("status", "ok")
                log(
                    "response_out",
                    turn_id=turn["turn_id"],
                    tier=getattr(provider, "tier", None),
                    name=getattr(provider, "name", "unknown"),
                    out_chars=len(_result_text),
                    tool_use=False,
                    tool_use_count=0,
                    input_tokens=0,
                    output_tokens=len(_result_text.split()),
                    trickle_mode=trickle_mode,
                )
                log_conversation(
                    turn["turn_id"],
                    "assistant",
                    [{"type": "text", "text": _result_text}],
                    model=requested_model,
                    provider=getattr(provider, "name", "unknown"),
                )
                if body.get("stream"):
                    return StreamingResponse(_sse_events(_tb_resp), media_type="text/event-stream")
                return JSONResponse(_tb_resp)
        except Exception as _tb_exc:
            log("toolbox_direct_dispatch_error", tool=_tb_tool, error=str(_tb_exc))
            # Fall through to normal model dispatch on any error
    # ── END SERVER-SIDE TOOLBOX DISPATCH ─────────────────────────────────────

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
        _mark("t_prompt_ready")
        _tools = body.get("tools")
        if p.kind == "ollama":
            _msgs = messages_from_anthropic(body.get("messages", []), flavor="ollama")
            _sys = system_text
            _tools_eff = _tools
            if p.pool == "local":
                # Prepend surgical charter slices (core + request-relevant).
                # Cuts injection from 6237 → ~2600-3200 chars, freeing context
                # for CLAUDE.md identity content after trim.
                _charter_local = _load_charter_slices(body)
                _sys = _charter_local + "\n\n" + (_sys or "")
                _sys, _msgs, _trim_info = _trim_for_local(_sys, _msgs)
                # Append active task state AFTER the trim, at the END of system
                # text. Two reasons: (a) appended post-trim it can never be cut,
                # (b) keeping the DYNAMIC block last preserves ollama's KV prefix
                # cache — the static charter+sys head stays byte-identical across
                # turns, so ~1500 tokens skip prompt re-eval (~60s/turn on Mary's
                # CPU at 25 tok/s). Task-block-first was costing the whole cache.
                _task = load_task()
                _task_block = format_task_for_injection(_task) if _task else ""
                if _task_block:
                    _sys = (_sys or "") + "\n\n" + _task_block
                # Inject ~/MD/ files (HANDOFF.md, handoff notes) after trim so
                # they are never cut. Capped by CLAF_MD_MAX_CHARS (default 3000).
                _md_block = _load_md_files()
                if _md_block:
                    _sys = (_sys or "") + "\n\n" + _md_block
                if _trim_info.get("trimmed"):
                    log("local_prompt_trimmed", **_trim_info)
                # Select the right tool group for this request. Sending all 32+
                # tools blows the local 8K CTX (tool schemas alone = 6400+ tokens).
                # select_local_tools() reads the request and picks 4-6 tools from
                # the matching group (browser/filesystem/tasks/core) = ~800-1200 tokens.
                _tools_eff = select_local_tools(body, _tools) if _tools else None
                log(
                    "local_tools_grouped",
                    tools_before=len(_tools) if _tools else 0,
                    tools_after=len(_tools_eff) if _tools_eff else 0,
                    first_tools=[t.get("name") for t in (_tools_eff or [])][:6],
                )
            if _sys:
                _msgs.insert(0, {"role": "system", "content": _sys})
            _blocks, _usage, _tool_use = ollama_chat(
                p, _msgs, _tools_eff, max_tokens=body.get("max_tokens")
            )
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
            _cloud_sys_max = (
                p.max_sys_chars
                if p.max_sys_chars is not None
                else int(os.environ.get("CLAF_CLOUD_SYS_MAX_CHARS", "8000"))
            )
            _cloud_msgs_max = (
                p.max_msgs
                if p.max_msgs is not None
                else int(os.environ.get("CLAF_CLOUD_MAX_MSGS", "20"))
            )
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
                log(
                    "cloud_full_context",
                    provider=p.name,
                    charter_chars=len(_charter) - len(_mem_pack),
                    memory_pack_chars=len(_mem_pack),
                    sys_tail_chars=len(_sys_tail),
                    msg_count=len(_cloud_msgs),
                )
            if _trim_on:
                _tail_budget = _cloud_sys_max - len(_charter)
                if _tail_budget <= 0:
                    # Charter alone fills the budget — ship it whole, drop the tail.
                    _cloud_sys = _charter
                    if _sys_tail:
                        log(
                            "cloud_sys_tail_dropped",
                            provider=p.name,
                            charter_chars=len(_charter),
                            tail_chars=len(_sys_tail),
                        )
                elif len(_sys_tail) > _tail_budget:
                    _cloud_sys = _charter + _sys_tail[:_tail_budget]
                    log(
                        "cloud_sys_trimmed",
                        provider=p.name,
                        charter_chars=len(_charter),
                        tail_before=len(_sys_tail),
                        tail_after=_tail_budget,
                    )
                else:
                    _cloud_sys = _charter + _sys_tail
            else:
                _cloud_sys = _charter + _sys_tail
            # Inject active task state for cloud providers too, so every model in
            # the hybrid loop follows the same task list. Keep it last so trimming
            # never cuts the operator's actual words above.
            _task = load_task()
            if _task and task_belongs_to(_task, body.get("conversation_fingerprint")):
                _task_block = format_task_for_injection(_task)
                if _task_block:
                    _cloud_sys = (_cloud_sys or "") + "\n\n" + _task_block
            if _trim_on:
                if len(_cloud_msgs) > _cloud_msgs_max:
                    # Never cut inside a tool_calls/tool_result group: a "tool"
                    # message with no preceding assistant tool_calls message in
                    # the slice is an orphan. Strict upstreams (OpenRouter's
                    # Anthropic backend) don't just ignore it — they reject the
                    # WHOLE request ("messages: at least one message is
                    # required"), which silently killed every multi-tool-call
                    # turn (e.g. 12 parallel tool results) past this cap.
                    # Walk the cut point back to the owning assistant message
                    # so the group stays intact, even if that means keeping
                    # more than max_msgs for this turn.
                    _cut = len(_cloud_msgs) - _cloud_msgs_max
                    while _cut > 0 and _cloud_msgs[_cut].get("role") == "tool":
                        _cut -= 1
                    _cloud_msgs = _cloud_msgs[_cut:]
                    log(
                        "cloud_msgs_trimmed",
                        provider=p.name,
                        msgs_before=len(_msgs),
                        msgs_after=len(_cloud_msgs),
                    )
                # Cap per-message content. A single tool_result (file read,
                # bash output) can be 10K+ chars — enough to 413 groq even after
                # count and system trimming. Truncate each message's string content.
                _msg_content_max = (
                    p.max_msg_content
                    if p.max_msg_content is not None
                    else int(os.environ.get("CLAF_CLOUD_MSG_CONTENT_MAX", "2000"))
                )
                _trimmed_content = False
                _cloud_msgs_final = []
                for _m in _cloud_msgs:
                    c = _m.get("content")
                    if isinstance(c, str) and len(c) > _msg_content_max:
                        # Keep head AND tail. The voice hook prepends standing-orders
                        # boilerplate, so the operator's actual words sit at the END
                        # of user messages — head-only truncation deleted them
                        # (gaming PC live test 2026-06-11: model saw only the
                        # injected preamble, never the "open tab" command).
                        _half = max(_msg_content_max // 2 - 12, 50)
                        _m = dict(_m, content=c[:_half] + " …[trim]… " + c[-_half:])
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
                        "TaskList",
                        "TaskCreate",
                        "TaskUpdate",
                        "TaskGet",
                        # High-frequency sensei browser tools.
                        "mcp__sensei__tab_create",
                        "mcp__sensei__screenshot",
                        "mcp__sensei__read_full",
                        "mcp__sensei__click",
                        "mcp__sensei__fill",
                        "mcp__sensei__browse",
                        "mcp__sensei__scroll",
                        "mcp__sensei__key_press",
                        "mcp__sensei__read",
                        "mcp__sensei__js_eval",
                    ]
                    # Email intent boost: when the user asks about email/inbox,
                    # force the email-bridge tools into the cloud tool cap so the
                    # model uses them instead of opening browser tabs.
                    _prompt_text = _flatten_prompt_text(body)
                    if any(s in _prompt_text for s in _EMAIL_SIGNALS):
                        _HIGH_FREQ.extend(
                            [
                                "mcp__email-bridge__check_inbox",
                                "mcp__email-bridge__search_inbox",
                                "mcp__email-bridge__read_email",
                                "mcp__email-bridge__list_accounts",
                                "mcp__email-bridge__list_folders",
                            ]
                        )
                        log("email_tools_boosted", provider=p.name)
                    _tool_map = {t.get("name"): t for t in _tools}
                    _priority = [_tool_map[n] for n in _HIGH_FREQ if n in _tool_map]
                    _priority_names = {t.get("name") for t in _priority}
                    _sensei_rest = [
                        t
                        for t in _tools
                        if t.get("name", "").startswith("mcp__sensei__")
                        and t.get("name") not in _priority_names
                    ]
                    _other_mcp = [
                        t
                        for t in _tools
                        if t.get("name", "").startswith("mcp__")
                        and not t.get("name", "").startswith("mcp__sensei__")
                    ]
                    _rest = [
                        t
                        for t in _tools
                        if not t.get("name", "").startswith("mcp__")
                        and t.get("name") not in _EXCLUDE
                    ]
                    _ordered = _priority + _sensei_rest + _other_mcp + _rest
                    _tools_eff = _ordered[: p.max_tools]
                log(
                    "cloud_tools_capped",
                    provider=p.name,
                    tools_before=len(_tools),
                    tools_after=len(_tools_eff) if _tools_eff else 0,
                    first_tools=[t.get("name") for t in (_tools_eff or [])][:5],
                )
            _blocks, _usage, _tool_use = openai_compat_chat(p, _cloud_msgs, _tools_eff)
        elif p.kind == "anthropic":
            _blocks, _usage = anthropic_direct_chat(p, body)
            _tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in _blocks)
            _text = "".join(
                b.get("text", "")
                for b in _blocks
                if isinstance(b, dict) and b.get("type") == "text"
            )
            return _blocks, _usage, False, _text, _tool_use
        else:
            raise RuntimeError(f"unknown provider kind: {p.kind}")

        # Fallback: model returned plain text despite having tools available —
        # try the heuristic directive scraper (covers prose-format tool calls
        # from models that don't emit native tool_calls).
        if not _tool_use and _tools:
            _text0 = "".join(
                b.get("text", "")
                for b in _blocks
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if _text0:
                _scraped, _scraped_tu = parse_directives_to_content(_text0, _tools or [])
                if _scraped_tu:
                    _blocks, _tool_use = _scraped, True
        _text = "".join(
            b.get("text", "") for b in _blocks if isinstance(b, dict) and b.get("type") == "text"
        )

        # Action bridge: auto-execute BROWSE:/SHELL:/FILE: directives in raw text
        if HAS_ACTION_BRIDGE and _text:
            _new_text = execute_actions_in_text(_text)
            if _new_text != _text:
                # Rebuild blocks with augmented text
                _blocks = [{"type": "text", "text": _new_text}] + [
                    b for b in _blocks if not (isinstance(b, dict) and b.get("type") == "text")
                ]
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
                _d_start = time.monotonic()
                content_blocks, usage, used_react, assistant_text, tool_use = (
                    await asyncio.to_thread(_dispatch_provider, provider)
                )
                _dispatch_kind = "fallback" if _rate_limit_failed else "primary"
                _record_dispatch(_dispatch_kind, provider, _d_start, time.monotonic())
                # Mechanical scope enforcement: for bounded auto tasks, ensure
                # the emitted tool call matches the next pending item. Weak local
                # models often emit the wrong file or malformed arguments.
                content_blocks = _enforce_auto_task_scope(content_blocks)
                tool_use = any(
                    isinstance(b, dict) and b.get("type") == "tool_use" for b in content_blocks
                )
                assistant_text = "".join(
                    b.get("text", "")
                    for b in content_blocks
                    if isinstance(b, dict) and b.get("type") == "text"
                )
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
                log(
                    "cloud_peer_fallback",
                    failed_provider=provider.name,
                    failed_tier=provider.tier,
                    status=_status,
                    failed_so_far=sorted(_rate_limit_failed),
                )
                provider = next_cloud_peer(_rate_limit_failed, max_tier=_max_fallback_tier)
                if provider is not None:
                    turn["provider"] = provider.name
                    turn["provider_pool"] = provider.pool
                    turn["model"] = provider.model
                if provider is None:
                    # No more cloud peers within the allowed tier budget. In
                    # hybrid/local mode, degrade to LOCAL Ollama — it never
                    # rate-limits and is the
                    # whole point of hybrid. Only error out if no local exists
                    # (cloud-only mode).
                    _local = next(
                        (p for p in PROVIDERS if p.pool == "local" and p.enabled),
                        None,
                    )
                    if _local is not None:
                        # If the request has tools and CLAF_LOCAL_MAX_TOOLS=0,
                        # local can't do tool calls — return an explicit message
                        # instead of a silent 1-token failure.
                        _has_tools = bool(body.get("tools"))
                        _local_max_tools = int(os.environ.get("CLAF_LOCAL_MAX_TOOLS", "6"))
                        if _has_tools and _local_max_tools == 0:
                            if trickle_reservation:
                                throttle.refund(trickle_reservation)
                                trickle_reservation = None
                            log("rate_limit_tool_block", failed_peers=sorted(_rate_limit_failed))
                            _rl_msg = (
                                "[CLAF: all cloud peers rate-limited — local model has no tools. "
                                "Wait 30s and retry, or set CLAF_LOCAL_MAX_TOOLS=6 in .env "
                                "to enable local tool groups.]"
                            )
                            _rl_resp = wrap_anthropic_response(
                                requested_model,
                                [{"type": "text", "text": _rl_msg}],
                                {"input_tokens": 0, "output_tokens": len(_rl_msg.split())},
                                False,
                            )
                            if body.get("stream"):
                                return StreamingResponse(
                                    _sse_events(_rl_resp), media_type="text/event-stream"
                                )
                            return _rl_resp
                        provider = _local
                        turn["provider"] = provider.name
                        turn["provider_pool"] = provider.pool
                        turn["model"] = provider.model
                        if trickle_reservation:
                            throttle.refund(trickle_reservation)
                            trickle_reservation = None
                        trickle_mode = "local"
                        log(
                            "rate_limit_degraded_to_local",
                            failed_peers=sorted(_rate_limit_failed),
                            local_provider=_local.name,
                        )
                    else:
                        raise RuntimeError(
                            f"all cloud peers rate-limited and no local fallback: "
                            f"{sorted(_rate_limit_failed)}"
                        ) from _call_exc
                else:
                    log(
                        "rate_limit_next_peer", next_provider=provider.name, next_tier=provider.tier
                    )
    except Exception as e:
        log(
            "provider_error",
            tier=getattr(provider, "tier", None),
            name=getattr(provider, "name", "unknown"),
            error=str(e),
            rate_limit_chain=sorted(_rate_limit_failed) if _rate_limit_failed else None,
        )
        if trickle_reservation:
            throttle.refund(trickle_reservation)
            log("trickle_refund", reservation=trickle_reservation, reason="provider_error")
        _pname = getattr(provider, "name", "all-peers-exhausted")
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "type": "api_error",
                    "message": f"{_pname} call failed: {e}",
                }
            },
        )

    # Detect local context overflow: 8192-token window fully consumed by input,
    # model generated 1 token or nothing. Surface an explicit message instead of
    # silently returning an empty response that makes Claude Code stop with no output.
    _local_overflow = False
    if (
        provider.pool == "local"
        and usage.get("output_tokens", 0) <= 1
        and not assistant_text
        and not tool_use
    ):
        _local_overflow = True
        _overflow_msg = (
            f"[CLAF: local model context overflow — "
            f"input consumed {usage.get('input_tokens', '?')} of 8192 tokens, "
            "no response generated. Run /clear to reset context, "
            "or prefix your message with 'escalate:' to force cloud.]"
        )
        log(
            "local_ctx_overflow_detected",
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            provider=provider.name,
        )
        assistant_text = _overflow_msg
        content_blocks = [{"type": "text", "text": _overflow_msg}]

    # Task-continuation guard — GROUND-TRUTH backstop, runs BEFORE the giveup
    # interceptor so the cheap local retry gets first crack (the giveup path
    # force-escalates to a paid cloud peer; this one stays on the SAME provider).
    #
    # Signal: the active task file (~/.claf/current_task.json) still lists
    # UNRESOLVED items, yet the model returned text only. That means it narrated
    # progress and stopped mid-task — Claude Code would end the loop with work
    # still pending, and the operator (voice-only, may have walked away) never
    # sees it finish. Re-invoke ONCE on the same provider with a hard nudge to
    # emit the next tool call. One-shot via _claf_task_pushed; if this still
    # returns text-only, the giveup interceptor below escalates to cloud. Cost
    # ladder: cheap local retry → paid cloud only when local is truly stuck.
    if (
        not tool_use
        and body.get("tools")
        and not _local_overflow
        and not body.get("_claf_task_pushed")
    ):
        _task = load_task()
        _pending = _task_pending_count() if _task else 0
        if _pending > 0:
            if not task_belongs_to(_task, _conv_fp):
                # Stale task from another conversation — do not tax this turn.
                if _task and _task.get("auto"):
                    try:
                        TASK_FILE.unlink(missing_ok=True)
                        log(
                            "stale_auto_task_cleared_skipping_redispatch",
                            task_conv_fp=_task.get("conv_fp"),
                            current_conv_fp=_conv_fp,
                        )
                    except OSError as _clr_exc:
                        log("stale_auto_task_clear_failed", error=str(_clr_exc))
                else:
                    log(
                        "stale_model_task_skipping_redispatch",
                        task_conv_fp=_task.get("conv_fp"),
                        current_conv_fp=_conv_fp,
                    )
            else:
                log(
                    "task_continuation_forcing_redispatch",
                    provider=provider.name,
                    pending_items=_pending,
                    snippet=(assistant_text or "")[:120],
                )
                _continue_msg = {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"[CLAF-TASK-CONTINUE] Your active task file still has {_pending} "
                                "unresolved item(s) — you are MID-TASK, not done. Do NOT reply with "
                                "prose, do NOT summarize, do NOT stop. Emit your NEXT tool call now "
                                "to advance the next pending item. If an item just finished, first "
                                'update ~/.claf/current_task.json (set its status to "done"), then '
                                "call the tool for the next item this same turn."
                            ),
                        }
                    ],
                }
                body["messages"] = list(body.get("messages", [])) + [_continue_msg]
                body["_claf_task_pushed"] = True
                # Local-first: let the same provider finish its task. Cloud is the
                # giveup/replan fallback below, not the continuation path.
                try:
                    _tc_start = time.monotonic()
                    content_blocks, usage, used_react, assistant_text, tool_use = (
                        await asyncio.to_thread(_dispatch_provider, provider)
                    )
                    _record_dispatch("task_continue", provider, _tc_start, time.monotonic())
                    turn["redispatch_count"] = turn.get("redispatch_count", 0) + 1
                    log(
                        "task_continuation_redispatch_done",
                        provider=provider.name,
                        tool_use=tool_use,
                        out_chars=len(assistant_text or ""),
                    )
                except Exception as _continue_exc:
                    log(
                        "task_continuation_redispatch_failed",
                        provider=provider.name,
                        error=str(_continue_exc),
                    )
                # If the model STILL replied text-only and the task file was seeded
                # by us (auto:true), the model is saying the work is done — clear
                # the auto file so it can't linger and tax every later turn with an
                # extra dispatch. Model-WRITTEN files are never auto-deleted; the
                # model owns that lifecycle (charter: delete on completion).
                if not tool_use:
                    _t_after = load_task()
                    if _t_after and _t_after.get("auto"):
                        try:
                            TASK_FILE.unlink(missing_ok=True)
                            log("auto_task_cleared_after_redispatch")
                        except OSError as _clr_exc:
                            log("auto_task_clear_failed", error=str(_clr_exc))

    # Giveup interceptor (system-level backstop to the charter REPLAN rule).
    # When a response is text-only (no tool call) AND contains giveup language
    # while tools were available, the model has STOPPED instead of routing
    # around a failure. Claude Code would hand control back to the operator —
    # who is voice-only and may have walked away. Re-invoke ONCE with a forced
    # REPLAN nudge on a CLOUD peer (which has browser tools) so the loop keeps
    # going. The _claf_replanned flag caps this at one re-dispatch per request.
    _GIVEUP_MARKERS = (
        "i cannot access",
        "unable to connect",
        "connectivity issue",
        "service is unavailable",
        "service may not be accessible",
        "let me check if there are local files",
        "let me check if you have any local",
        "let me check if we have any local",
        "i'll stop here",
        "please try again",
        "cannot establish connection",
        "persistent connectivity",
        "timing out when trying",
        "appears to be unavailable",
        "i'm unable to",
    )
    if not tool_use and body.get("tools") and assistant_text and not body.get("_claf_replanned"):
        _low = assistant_text.lower()
        _hit = next((m for m in _GIVEUP_MARKERS if m in _low), None)
        if _hit is None and _is_action_turn(body):
            # Action-shaped turn answered with words instead of a tool call —
            # same failure as giveup language, just politer. Retry once on a
            # tool-bearing peer instead of ending the turn.
            _hit = "action_turn_text_only"
        if _hit:
            # Force a cloud peer for the replan turn so browser tools are present.
            _replan_provider = (
                provider if provider.pool == "cloud" else pick_cloud_peer(prefer_tiers=(1,))
            )
            if _replan_provider is not None:
                log(
                    "giveup_detected_forcing_replan",
                    provider=provider.name,
                    redispatch_to=_replan_provider.name,
                    marker=_hit,
                    snippet=assistant_text[:120],
                )
                _replan_msg = {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "[CLAF-REPLAN] The previous approach failed and you started "
                                "to give up. Do NOT stop, do NOT ask the operator, do NOT "
                                "look for local files. Pick a DIFFERENT tool for the same "
                                "goal and call it THIS turn. If a tab call timed out, try "
                                "mcp__sensei__browse or mcp__sensei__screenshot. If a click "
                                "failed, try mcp__sensei__js_eval. Act with a tool now."
                            ),
                        }
                    ],
                }
                # Cerebras rejects empty assistant content blocks (assistant
                # messages with content=[] or content=[{type:text,text:""}]).
                # Prune them before appending the replan nudge.
                _clean_msgs = []
                for _m in list(body.get("messages", [])):
                    if _m.get("role") == "assistant":
                        _c = _m.get("content", [])
                        if (
                            isinstance(_c, list)
                            and all(
                                isinstance(_b, dict)
                                and not _b.get("text", "").strip()
                                and _b.get("type") == "text"
                                for _b in _c
                            )
                            and _c
                        ):
                            continue  # skip empty assistant turns
                    _clean_msgs.append(_m)
                body["messages"] = _clean_msgs + [_replan_msg]
                body["_claf_replanned"] = True
                try:
                    _rp_start = time.monotonic()
                    content_blocks, usage, used_react, assistant_text, tool_use = (
                        await asyncio.to_thread(_dispatch_provider, _replan_provider)
                    )
                    _record_dispatch("replan", _replan_provider, _rp_start, time.monotonic())
                    turn["redispatch_count"] = turn.get("redispatch_count", 0) + 1
                    provider = _replan_provider
                    turn["provider"] = provider.name
                    turn["provider_pool"] = provider.pool
                    turn["model"] = provider.model
                    log(
                        "replan_redispatch_done",
                        provider=_replan_provider.name,
                        tool_use=tool_use,
                        out_chars=len(assistant_text or ""),
                    )
                except Exception as _replan_exc:
                    log("replan_redispatch_failed", error=str(_replan_exc))

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
        if (
            content_blocks
            and isinstance(content_blocks[0], dict)
            and content_blocks[0].get("type") == "text"
        ):
            content_blocks[0] = {"type": "text", "text": assistant_text}
        else:
            content_blocks.append({"type": "text", "text": trickle_degrade_note})

    response = wrap_anthropic_response(requested_model, content_blocks, usage, tool_use)
    _mark("t_response_out")
    turn["tool_use"] = tool_use
    turn["status"] = turn.get("status", "ok")
    log(
        "response_out",
        turn_id=turn["turn_id"],
        tier=getattr(provider, "tier", None),
        name=getattr(provider, "name", "unknown"),
        out_chars=len(assistant_text),
        tool_use=tool_use,
        tool_use_count=sum(1 for b in content_blocks if b.get("type") == "tool_use"),
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        trickle_mode=trickle_mode,
    )
    log_conversation(
        turn["turn_id"],
        "assistant",
        content_blocks,
        model=requested_model,
        provider=getattr(provider, "name", "unknown"),
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
            ctx = int(os.environ.get("CLAF_OLLAMA_CTX", "2048"))
            try:
                with httpx.Client(timeout=180.0) as c:
                    c.post(
                        f"{base}/api/generate",
                        json={
                            "model": LOCAL_MODEL,
                            "prompt": "hi",
                            "stream": False,
                            "keep_alive": _OLLAMA_KEEP_ALIVE,
                            "options": {"num_ctx": ctx},
                        },
                    )
                print(f"[prewarm] {LOCAL_MODEL} loaded (keep_alive={_OLLAMA_KEEP_ALIVE})")
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


def claf_wrap_ollama_text_as_anthropic(
    raw_text: str, model: str, used_react: bool, input_tokens: int = 0, output_tokens: int = 0
) -> dict:
    """Parse Ollama's raw assistant text and wrap as Anthropic /v1/messages
    response. If used_react is True, parse <tool_call> blocks into tool_use."""
    if used_react:
        blocks, stop = supervisor.parse_work_response(raw_text)
    else:
        blocks = [{"type": "text", "text": raw_text}]
        stop = "end_turn"
    return tool_bridge.build_anthropic_response(
        blocks,
        stop,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

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

app = FastAPI(title="CLAF orchestrator", version="0.4.0")


def log(event: str, **fields) -> None:
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
    with LOG_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def flatten_anthropic_content(content) -> str:
    """Claude content is either a string or a list of blocks (text / image / tool_use / tool_result).
    Flatten to a single text string so a small chat model can read it."""
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
            src = block.get("source", {})
            parts.append(f"[image: {src.get('media_type','?')} elided]")
        else:
            parts.append(f"[unknown block type={btype}]")
    return "\n".join(p for p in parts if p)


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
        out.append({"role": role, "content": flatten_anthropic_content(m.get("content", ""))})
    return out


def ollama_chat(provider, messages: list[dict]) -> tuple[str, dict]:
    payload = {
        "model": provider.model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 4096},
    }
    with httpx.Client(timeout=300.0) as client:
        r = client.post(provider.url, json=payload)
        r.raise_for_status()
        data = r.json()
    text = data.get("message", {}).get("content", "")
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


def wrap_anthropic_response(model_id: str, assistant_text: str, usage: dict) -> dict:
    return {
        "id": f"msg_claf_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model_id,
        "content": [{"type": "text", "text": assistant_text}],
        "stop_reason": "end_turn",
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

    response = wrap_anthropic_response(requested_model, assistant_text, usage)
    log(
        "response_out",
        tier=provider.tier,
        name=provider.name,
        out_chars=len(assistant_text),
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

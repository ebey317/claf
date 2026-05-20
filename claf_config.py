"""CLAF routing config — single source of truth for SENSEI mode + provider selection.

Three SENSEI modes (set via env var CLAF_MODE):

    local     — MADAM only: local Ollama. NO cloud peers exist in PROVIDERS.
                select_provider() literally cannot return a non-local provider.
                (Alias: off_grid)
    hybrid    — MADAM first. Local Ollama for routine traffic; cloud peer pool
                on hard-task escalation. (Alias: with_convenience)  [default]
    cloud     — Cloud peer pool only. Local Ollama is bypassed.

Cloud pool is a flat set of peers — Groq, Gemini, Cerebras, Fireworks,
OpenRouter, Anthropic. Each peer is gated by env-key presence. No provider
hardcoded as preferred or excluded. Selection ordering is data-driven via
the per-provider `tier` field (lower tier = picked first); the ordering is
configuration, not a quality ranking baked into routing logic.

API keys for cloud peers are only consulted when MODE is `hybrid` or `cloud`.
In `local` mode the cloud-peer code path doesn't run at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


# ----------------------------------------------------------------------------
# Mode — three SENSEI modes; legacy names accepted as aliases for one cycle.
# ----------------------------------------------------------------------------

_MODE_ALIASES = {
    "off_grid": "local",
    "with_convenience": "hybrid",
}

_VALID_MODES = ("local", "hybrid", "cloud")
_raw_mode = os.environ.get("CLAF_MODE", "hybrid").strip().lower()
MODE = _MODE_ALIASES.get(_raw_mode, _raw_mode)
assert MODE in _VALID_MODES, (
    f"CLAF_MODE must be one of {_VALID_MODES} "
    f"(or legacy aliases off_grid/with_convenience), got: {_raw_mode!r}"
)


# ----------------------------------------------------------------------------
# Provider type — `pool` distinguishes local from cloud peers.
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Provider:
    tier: int                # ordering within pool; lower = picked first
    name: str                # short slug used in logs
    pool: str                # "local" | "cloud"
    kind: str                # "ollama" | "openai_compat" | "anthropic"
    model: str               # model tag/id at the provider
    url: str                 # endpoint
    env_key: str | None      # env var holding the auth token; None for local
    enabled: bool            # gated by env-key presence (always True for local)
    notes: str = ""


def _env_present(name: str | None) -> bool:
    return bool(name) and bool(os.environ.get(name, "").strip())


# Local provider — always present in `local` and `hybrid` modes; absent in `cloud`.
_LOCAL_PROVIDER = Provider(
    tier=0,
    name="local-ollama",
    pool="local",
    kind="ollama",
    model=os.environ.get("CLAF_LOCAL_MODEL", "qwen3-vl:2b"),
    url=os.environ.get("CLAF_OLLAMA_URL", "http://localhost:11434/api/chat"),
    env_key=None,
    enabled=True,
    notes="local; runs offline; tools + vision + thinking",
)


def _cloud_peers() -> list[Provider]:
    """Flat cloud pool. All peers; no provider hardcoded as preferred.
    Tier numbers are a configurable ordering hint, not a quality ranking."""
    return [
        Provider(
            tier=1, name="groq", pool="cloud", kind="openai_compat",
            model="llama-3.3-70b-versatile",
            url="https://api.groq.com/openai/v1/chat/completions",
            env_key="GROQ_API_KEY",
            enabled=_env_present("GROQ_API_KEY"),
            notes="rate-limited free tier; fast",
        ),
        Provider(
            tier=2, name="gemini", pool="cloud", kind="openai_compat",
            model="gemini-2.5-flash",
            url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            env_key="GEMINI_API_KEY",
            enabled=_env_present("GEMINI_API_KEY"),
            notes="long-context free tier",
        ),
        Provider(
            tier=3, name="cerebras", pool="cloud", kind="openai_compat",
            model="llama-3.3-70b",
            url="https://api.cerebras.ai/v1/chat/completions",
            env_key="CEREBRAS_API_KEY",
            enabled=_env_present("CEREBRAS_API_KEY"),
            notes="fast inference",
        ),
        Provider(
            tier=4, name="fireworks", pool="cloud", kind="openai_compat",
            model="accounts/fireworks/models/qwen2p5-72b-instruct",
            url="https://api.fireworks.ai/inference/v1/chat/completions",
            env_key="FIREWORKS_API_KEY",
            enabled=_env_present("FIREWORKS_API_KEY"),
            notes="hosted open models",
        ),
        Provider(
            tier=5, name="openrouter", pool="cloud", kind="openai_compat",
            model="anthropic/claude-sonnet-4.6",
            url="https://openrouter.ai/api/v1/chat/completions",
            env_key="OPENROUTER_API_KEY",
            enabled=_env_present("OPENROUTER_API_KEY"),
            notes="multi-provider gateway, BYO key",
        ),
        Provider(
            tier=6, name="anthropic", pool="cloud", kind="anthropic",
            model="claude-sonnet-4-5",
            url="https://api.anthropic.com/v1/messages",
            env_key="ANTHROPIC_API_KEY",
            enabled=_env_present("ANTHROPIC_API_KEY"),
            notes="peer in cloud pool; not special-cased",
        ),
    ]


# Registry computed at import time, based on mode.
# - local : only the local provider
# - hybrid: local + cloud peer pool
# - cloud : cloud peer pool only (local provider NOT constructed)
PROVIDERS: list[Provider] = []
if MODE in ("local", "hybrid"):
    PROVIDERS.append(_LOCAL_PROVIDER)
if MODE in ("hybrid", "cloud"):
    PROVIDERS.extend(_cloud_peers())


# ----------------------------------------------------------------------------
# Hard-task heuristic — only consulted in hybrid mode. Thresholds raised so
# routine Claude Code traffic (system prompts in the 5-15k range with normal
# tool defs) stays local and doesn't silently escalate.
# ----------------------------------------------------------------------------

def _is_hard_task(body: dict) -> bool:
    """Should this request escalate above local in hybrid mode?

    Triggers (any one):
      - explicit `metadata.escalate = True`
      - system prompt > 40k characters (truly large agent system)
      - message count > 60 (very long conversation)
      - last message content contains `[ESCALATE]` marker
    """
    meta = body.get("metadata") or {}
    if meta.get("escalate") is True:
        return True

    system = body.get("system") or ""
    if isinstance(system, list):
        system_text = "".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in system
        )
    else:
        system_text = str(system)
    if len(system_text) > 40_000:
        return True

    msgs = body.get("messages") or []
    if len(msgs) > 60:
        return True

    if msgs:
        last = msgs[-1]
        content = last.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in content
            )
        if "[ESCALATE]" in str(content):
            return True

    return False


# ----------------------------------------------------------------------------
# Routing decision
# ----------------------------------------------------------------------------

def _pick_cloud_peer() -> Provider:
    """Pick the enabled cloud peer with the lowest tier (configurable ordering).
    No provider hardcoded as preferred — tier is the only ordering signal."""
    cloud = [p for p in PROVIDERS if p.pool == "cloud" and p.enabled]
    if not cloud:
        raise RuntimeError(
            f"mode={MODE} requires at least one cloud peer; none enabled. "
            "Set one of: GROQ_API_KEY, GEMINI_API_KEY, CEREBRAS_API_KEY, "
            "FIREWORKS_API_KEY, OPENROUTER_API_KEY, ANTHROPIC_API_KEY."
        )
    return min(cloud, key=lambda p: p.tier)


def select_provider(body: dict, prefer_tier: int | None = None) -> Provider:
    """Pick a provider given the request body and current SENSEI mode.

    - MODE=local  : local Ollama only; cloud peers absent from PROVIDERS.
                    select_provider cannot return a non-local provider.
    - MODE=hybrid : local for routine traffic; cloud peer pool on hard-task
                    escalation. Pool selection uses lowest enabled tier.
    - MODE=cloud  : cloud peer pool only; local Ollama bypassed.
    """
    enabled = [p for p in PROVIDERS if p.enabled]
    if not enabled:
        raise RuntimeError("no providers enabled")

    if MODE == "local":
        local = next((p for p in enabled if p.pool == "local"), None)
        if local is None:
            raise RuntimeError("local mode but no local provider enabled")
        assert all(p.pool == "local" for p in PROVIDERS), (
            "local mode must contain only local-pool providers"
        )
        return local

    if MODE == "cloud":
        if prefer_tier is not None:
            match = [p for p in enabled if p.pool == "cloud" and p.tier == prefer_tier]
            if match:
                return match[0]
        return _pick_cloud_peer()

    # MODE == hybrid
    if prefer_tier is not None:
        match = [p for p in enabled if p.tier == prefer_tier]
        if match:
            return match[0]

    if _is_hard_task(body):
        return _pick_cloud_peer()

    local = next((p for p in enabled if p.pool == "local"), None)
    if local is None:
        # Defensive: hybrid without a local provider would be ill-defined.
        raise RuntimeError("hybrid mode requires a local provider")
    return local


# ----------------------------------------------------------------------------
# Self-check (no network)
# ----------------------------------------------------------------------------

def describe() -> dict:
    """Render the current config as a dict suitable for /healthz / startup banner."""
    return {
        "mode": MODE,
        "providers": [
            {
                "tier": p.tier,
                "name": p.name,
                "pool": p.pool,
                "kind": p.kind,
                "model": p.model,
                "enabled": p.enabled,
                "env_key": p.env_key,
                "notes": p.notes,
            }
            for p in PROVIDERS
        ],
        "local_provider": next(
            (p.name for p in PROVIDERS if p.pool == "local" and p.enabled), None
        ),
        "cloud_peers_enabled": [
            p.name for p in PROVIDERS if p.pool == "cloud" and p.enabled
        ],
    }

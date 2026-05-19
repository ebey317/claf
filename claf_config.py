"""CLAF routing config — single source of truth for which tier handles a request.

Two modes:
    local_only  — every request → local Ollama. No API keys touched. Default.
    all         — escalation tiers: local → free APIs → paid Anthropic.
                  Routing decided by request shape (system prompt size, tool count,
                  explicit escalate-hint, last-tier-failed).

Mode is selected via env var CLAF_MODE. Both modes share the same provider
registry below; "local_only" simply restricts the routing function to the
local tier.

Ranking is by power × ability — heaviest model the operator has access to is
at the top of the escalation ladder. Local model is always tier 0 (default
target). The router tries tier 0 first; on a "hard task" signal or a tier-0
failure, it walks UP the ladder.

API keys are read from env at startup, not committed to disk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


# ----------------------------------------------------------------------------
# Mode
# ----------------------------------------------------------------------------

MODE = os.environ.get("CLAF_MODE", "local_only").strip().lower()
assert MODE in ("local_only", "all"), f"CLAF_MODE must be 'local_only' or 'all', got: {MODE!r}"


# ----------------------------------------------------------------------------
# Provider registry — ordered low-to-high by escalation priority.
# Tier 0 is the default target. Tier N is the last-resort top-tier brain.
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Provider:
    tier: int                # 0 = local (cheapest, default), higher = paid/premium
    name: str                # short slug used in logs
    kind: str                # "ollama" | "openai_compat" | "anthropic"
    model: str               # model tag/id at the provider
    url: str                 # API endpoint
    env_key: str | None      # env var holding the auth token; None for local
    enabled: bool            # gated by env presence (auto-disable if no key)
    notes: str = ""


def _env_present(name: str | None) -> bool:
    return bool(name) and bool(os.environ.get(name, "").strip())


PROVIDERS: list[Provider] = [
    Provider(
        tier=0,
        name="local-ollama",
        kind="ollama",
        model=os.environ.get("CLAF_LOCAL_MODEL", "qwen3-vl:2b"),
        url=os.environ.get("CLAF_OLLAMA_URL", "http://localhost:11434/api/chat"),
        env_key=None,
        enabled=True,
        notes="default; runs offline; tools + vision + thinking",
    ),
    Provider(
        tier=1,
        name="groq-free",
        kind="openai_compat",
        model="llama-3.3-70b-versatile",
        url="https://api.groq.com/openai/v1/chat/completions",
        env_key="GROQ_API_KEY",
        enabled=_env_present("GROQ_API_KEY"),
        notes="fast inference, free tier (rate-limited)",
    ),
    Provider(
        tier=2,
        name="gemini-free",
        kind="openai_compat",
        model="gemini-2.5-flash",
        url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        env_key="GEMINI_API_KEY",
        enabled=_env_present("GEMINI_API_KEY"),
        notes="long-context free tier",
    ),
    Provider(
        tier=3,
        name="openrouter",
        kind="openai_compat",
        model="anthropic/claude-sonnet-4.6",
        url="https://openrouter.ai/api/v1/chat/completions",
        env_key="OPENROUTER_API_KEY",
        enabled=_env_present("OPENROUTER_API_KEY"),
        notes="multi-provider gateway, BYO key",
    ),
    Provider(
        tier=4,
        name="anthropic-direct",
        kind="anthropic",
        model="claude-sonnet-4-6",
        url="https://api.anthropic.com/v1/messages",
        env_key="CLAF_ANTHROPIC_API_KEY",
        enabled=_env_present("CLAF_ANTHROPIC_API_KEY"),
        notes="paid top-tier; selective use only",
    ),
]


# ----------------------------------------------------------------------------
# Routing decision
# ----------------------------------------------------------------------------

def _is_hard_task(body: dict) -> bool:
    """Heuristic: should this request escalate above the local tier?

    Triggers:
      - explicit hint in `metadata.escalate` = True
      - system prompt > 10k characters (heavy tool surface, likely complex agent step)
      - message count > 20 (long conversation, more reasoning needed)
      - last assistant message contained the string '[ESCALATE]' (router hint)
    """
    meta = body.get("metadata") or {}
    if meta.get("escalate") is True:
        return True

    system = body.get("system") or ""
    if isinstance(system, list):
        system_text = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in system)
    else:
        system_text = str(system)
    if len(system_text) > 10_000:
        return True

    msgs = body.get("messages") or []
    if len(msgs) > 20:
        return True

    if msgs:
        last = msgs[-1]
        content = last.get("content", "")
        if isinstance(content, list):
            content = " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
        if "[ESCALATE]" in str(content):
            return True

    return False


def select_provider(body: dict, prefer_tier: int | None = None) -> Provider:
    """Pick a provider given the request body and current mode.

    In local_only mode: always returns the tier-0 (local) provider, regardless
    of hardness signals. The escalation ladder is unused.

    In all mode: starts at the lowest enabled tier; if the task is "hard",
    skips ahead to the lowest enabled tier >= ceil(N/2) where N = highest
    enabled tier. Caller may pin a specific tier via prefer_tier.
    """
    enabled = [p for p in PROVIDERS if p.enabled]
    if not enabled:
        raise RuntimeError("no providers enabled — at minimum tier-0 local must be enabled")

    if MODE == "local_only":
        return next(p for p in enabled if p.tier == 0)

    # all mode
    if prefer_tier is not None:
        match = [p for p in enabled if p.tier == prefer_tier]
        if match:
            return match[0]

    if _is_hard_task(body):
        # Jump to mid-ladder or higher among enabled
        top = max(p.tier for p in enabled)
        midpoint = (top + 1) // 2
        for p in sorted(enabled, key=lambda p: p.tier):
            if p.tier >= midpoint:
                return p
        return enabled[-1]

    # Default: lowest enabled tier (local first)
    return min(enabled, key=lambda p: p.tier)


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
                "kind": p.kind,
                "model": p.model,
                "enabled": p.enabled,
                "env_key": p.env_key,
                "notes": p.notes,
            }
            for p in PROVIDERS
        ],
        "tier_0_default": next(p.name for p in PROVIDERS if p.tier == 0),
    }

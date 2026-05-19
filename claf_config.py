"""CLAF routing config — single source of truth for which tier handles a request.

Off-grid is the architecture, not a toggle. The default mode is `off_grid`,
which means: the proxy NEVER reaches the internet, regardless of what
env keys are set. Cloud-tier providers are pruned from the registry at
import time, not just disabled — there's no live code path that could
call them. A stray API key in the operator's shell does not change behavior.

Two modes:
    off_grid          — local Ollama only. NO cloud entries exist in
                        PROVIDERS. `select_provider()` literally cannot
                        return a non-local provider. Default.
    with_convenience  — opt-in. Adds the cloud tiers (Groq / Gemini /
                        OpenRouter / Anthropic-direct) for scenarios where
                        the operator deliberately wants escalation.

Mode is selected via env var CLAF_MODE.

API keys are only read if mode is `with_convenience`. In off_grid mode
the env-var lookup code for cloud providers does not run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


# ----------------------------------------------------------------------------
# Mode — off_grid is the brand. with_convenience is opt-in.
# ----------------------------------------------------------------------------

_VALID_MODES = ("off_grid", "with_convenience")
MODE = os.environ.get("CLAF_MODE", "off_grid").strip().lower()
assert MODE in _VALID_MODES, (
    f"CLAF_MODE must be one of {_VALID_MODES}, got: {MODE!r}"
)


# ----------------------------------------------------------------------------
# Provider type
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Provider:
    tier: int                # 0 = local (default), higher = upstream/paid
    name: str                # short slug used in logs
    kind: str                # "ollama" | "openai_compat" | "anthropic"
    model: str               # model tag/id at the provider
    url: str                 # endpoint
    env_key: str | None      # env var holding the auth token; None for local
    enabled: bool            # in off_grid mode, only the tier-0 entry exists
    notes: str = ""


def _env_present(name: str | None) -> bool:
    return bool(name) and bool(os.environ.get(name, "").strip())


# Tier-0 (local) is always present in every mode. This is the off-grid spine.
_LOCAL_TIER = Provider(
    tier=0,
    name="local-ollama",
    kind="ollama",
    model=os.environ.get("CLAF_LOCAL_MODEL", "qwen3-vl:2b"),
    url=os.environ.get("CLAF_OLLAMA_URL", "http://localhost:11434/api/chat"),
    env_key=None,
    enabled=True,
    notes="default; runs offline; tools + vision + thinking",
)


def _convenience_tiers() -> list[Provider]:
    """Built only when MODE == with_convenience. Not constructed at all
    in off_grid mode — env vars for cloud keys are never read."""
    return [
        Provider(
            tier=1,
            name="groq-free",
            kind="openai_compat",
            model="llama-3.3-70b-versatile",
            url="https://api.groq.com/openai/v1/chat/completions",
            env_key="GROQ_API_KEY",
            enabled=_env_present("GROQ_API_KEY"),
            notes="convenience; rate-limited free tier",
        ),
        Provider(
            tier=2,
            name="gemini-free",
            kind="openai_compat",
            model="gemini-2.5-flash",
            url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            env_key="GEMINI_API_KEY",
            enabled=_env_present("GEMINI_API_KEY"),
            notes="convenience; long-context free tier",
        ),
        Provider(
            tier=3,
            name="openrouter",
            kind="openai_compat",
            model="anthropic/claude-sonnet-4.6",
            url="https://openrouter.ai/api/v1/chat/completions",
            env_key="OPENROUTER_API_KEY",
            enabled=_env_present("OPENROUTER_API_KEY"),
            notes="convenience; multi-provider gateway, BYO key",
        ),
        Provider(
            tier=4,
            name="anthropic-direct",
            kind="anthropic",
            model="claude-sonnet-4-6",
            url="https://api.anthropic.com/v1/messages",
            env_key="CLAF_ANTHROPIC_API_KEY",
            enabled=_env_present("CLAF_ANTHROPIC_API_KEY"),
            notes="convenience; paid top-tier; selective use only",
        ),
    ]


# Registry is computed at import time. In off_grid mode it contains ONLY
# the local tier — there is no live code path to a cloud provider.
PROVIDERS: list[Provider] = [_LOCAL_TIER]
if MODE == "with_convenience":
    PROVIDERS.extend(_convenience_tiers())


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

    In off_grid mode: PROVIDERS contains only the tier-0 local entry by
    construction. There is no path to a cloud provider — this function
    cannot return one, regardless of arguments or env state.

    In with_convenience mode: starts at the lowest enabled tier; if the
    task is "hard", skips ahead to the lowest enabled tier >= ceil(N/2)
    where N = highest enabled tier. Caller may pin a tier via prefer_tier.
    """
    enabled = [p for p in PROVIDERS if p.enabled]
    if not enabled:
        raise RuntimeError("no providers enabled — at minimum tier-0 local must be enabled")

    if MODE == "off_grid":
        # PROVIDERS is already pruned to just tier-0 in off_grid mode,
        # but assert explicitly so a future code mistake can't slip past.
        local = next(p for p in enabled if p.tier == 0)
        assert all(p.tier == 0 for p in PROVIDERS), "off_grid mode must contain only tier-0"
        return local

    # with_convenience mode
    if prefer_tier is not None:
        match = [p for p in enabled if p.tier == prefer_tier]
        if match:
            return match[0]

    if _is_hard_task(body):
        top = max(p.tier for p in enabled)
        midpoint = (top + 1) // 2
        for p in sorted(enabled, key=lambda p: p.tier):
            if p.tier >= midpoint:
                return p
        return enabled[-1]

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

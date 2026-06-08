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
    model=os.environ.get("CLAF_LOCAL_MODEL", "qwen2.5:3b"),
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
            # Ollama Cloud — proxied through the local ollama CLI at :11434,
            # which signs requests with ~/.ollama/id_ed25519. No per-token
            # billing; subject only to the operator's Ollama Cloud account
            # quota. Kind is "ollama" because the request shape is Ollama's
            # /api/chat, not OpenAI-compat. Pool is "cloud" so it counts as
            # an escalation peer for routing/budget purposes.
            tier=1, name="ollama-cloud-coder", pool="cloud", kind="ollama",
            model="qwen3-coder:480b-cloud",
            url=os.environ.get("CLAF_OLLAMA_URL", "http://localhost:11434/api/chat"),
            env_key=None,  # SSH-key auth, no env var
            enabled=True,
            notes="480B coder via Ollama Cloud (SSH-signed); 1-2s latency",
        ),
        # NOTE: qwen3.5:cloud and kimi-k2.5:cloud peers were removed 2026-05-22.
        # Operator (account 'ebey317') never subscribed to Ollama Cloud paid tier;
        # those models return "subscription required". If/when subscribed, re-add
        # them as kind="ollama", pool="cloud", url=localhost:11434/api/chat,
        # env_key=None. SSH-signed via local ollama CLI.
        Provider(
            tier=2, name="groq", pool="cloud", kind="openai_compat",
            model="llama-3.3-70b-versatile",
            url="https://api.groq.com/openai/v1/chat/completions",
            env_key="GROQ_API_KEY",
            enabled=_env_present("GROQ_API_KEY"),
            notes="free tier, fast; rate-limited",
        ),
        Provider(
            tier=3, name="cerebras", pool="cloud", kind="openai_compat",
            # Cerebras account 'ebey317' has: llama3.1-8b, zai-glm-4.7,
            # qwen-3-235b-a22b-instruct-2507, gpt-oss-120b. 235B Qwen is the
            # strongest available — verified by /v1/models 2026-05-22.
            model="qwen-3-235b-a22b-instruct-2507",
            url="https://api.cerebras.ai/v1/chat/completions",
            env_key="CEREBRAS_API_KEY",
            enabled=_env_present("CEREBRAS_API_KEY"),
            notes="ultra-fast inference; 235B Qwen via Cerebras",
        ),
        Provider(
            tier=4, name="deepseek", pool="cloud", kind="openai_compat",
            model="deepseek-chat",
            url="https://api.deepseek.com/v1/chat/completions",
            env_key="DEEPSEEK_API_KEY",
            enabled=_env_present("DEEPSEEK_API_KEY"),
            notes="DeepSeek direct; enabled when DEEPSEEK_API_KEY is present",
        ),
        Provider(
            tier=5, name="openai", pool="cloud", kind="openai_compat",
            model="gpt-4o-mini",
            url="https://api.openai.com/v1/chat/completions",
            env_key="OPENAI_API_KEY",
            enabled=_env_present("OPENAI_API_KEY"),
            notes="OpenAI direct; enabled when OPENAI_API_KEY is present",
        ),
        Provider(
            tier=6, name="fireworks", pool="cloud", kind="openai_compat",
            # Fireworks account has deepseek-v4-pro (verified by /v1/models
            # 2026-05-22). 17 models total — change if a different default
            # is preferred.
            model="accounts/fireworks/models/deepseek-v4-pro",
            url="https://api.fireworks.ai/inference/v1/chat/completions",
            env_key="FIREWORKS_API_KEY",
            enabled=_env_present("FIREWORKS_API_KEY"),
            notes="DeepSeek V4 Pro via Fireworks",
        ),
        Provider(
            tier=7, name="openrouter", pool="cloud", kind="openai_compat",
            model="anthropic/claude-sonnet-4.6",
            url="https://openrouter.ai/api/v1/chat/completions",
            env_key="OPENROUTER_API_KEY",
            enabled=_env_present("OPENROUTER_API_KEY"),
            notes="multi-provider gateway; routes to Sonnet 4.6",
        ),
        Provider(
            tier=8, name="gemini", pool="cloud", kind="openai_compat",
            model="gemini-2.5-flash",
            url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            env_key="GEMINI_API_KEY",
            enabled=_env_present("GEMINI_API_KEY"),
            notes="long-context free tier (no key set 2026-05-22)",
        ),
        Provider(
            tier=9, name="anthropic", pool="cloud", kind="anthropic",
            # Was claude-opus-4-7. Tier-1 Console accounts have very tight
            # rate limits on Opus/Sonnet (every call 429s) while Haiku has
            # headroom. Restore Opus once the operator's Anthropic spend
            # raises the tier — until then Haiku is the only model that
            # actually serves on this key. 2026-05-22.
            model="claude-haiku-4-5-20251001",
            url="https://api.anthropic.com/v1/messages",
            env_key="ANTHROPIC_API_KEY",
            enabled=_env_present("ANTHROPIC_API_KEY"),
            notes="peer in cloud pool; Haiku-pinned while tier is rate-limited",
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
      - active tool loop: any message contains tool_use or tool_result blocks
        (local Qwen has CLAF_LOCAL_MAX_TOOLS=0 and can't continue a tool loop)
    """
    import os as _os
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

    # Active tool loop detection: escalate if any turn contains tool_use or
    # tool_result blocks. Local Qwen has CLAF_LOCAL_MAX_TOOLS=0 and cannot
    # continue a tool loop — routing it local silently breaks agent automation.
    local_max_tools = int(_os.environ.get("CLAF_LOCAL_MAX_TOOLS", "0"))
    if local_max_tools == 0:
        for msg in msgs:
            c = msg.get("content", [])
            if isinstance(c, list):
                for blk in c:
                    if isinstance(blk, dict) and blk.get("type") in ("tool_use", "tool_result"):
                        return True

    return False


# ----------------------------------------------------------------------------
# Tap intent templates — used when _select_mode picks "tap" so the cloud
# polish call gets an intent-specific prompt instead of a generic "fix this".
# ----------------------------------------------------------------------------

TAP_TEMPLATES = {
    "regex": (
        "Write a robust regex for the user's stated intent. Handle common edge "
        "cases (anchoring, escaping, unicode where appropriate). Return only the "
        "regex pattern in a single fenced code block, followed by one short line "
        "of explanation. Intent and current draft follow.\n\n{snippet}"
    ),
    "bash": (
        "Write a production-ready bash one-liner or short script for the user's "
        "stated intent. Use proper quoting, set -euo pipefail when multi-line, "
        "include short inline comments. Return the script in a single fenced "
        "code block. Intent and current draft follow.\n\n{snippet}"
    ),
    "sql": (
        "Write an optimized, portable SQL query for the user's stated intent. "
        "Use parameterized placeholders where values are external, prefer "
        "indexable predicates, avoid SELECT *. Return the query in a single "
        "fenced code block, followed by one short line on performance notes. "
        "Intent and current draft follow.\n\n{snippet}"
    ),
    "debug": (
        "Analyze the following error/symptom and respond with: (1) root cause "
        "in one sentence, (2) the fix as a fenced code block, (3) one-line "
        "prevention tip. No preamble.\n\n{snippet}"
    ),
    "generic": (
        "Improve the following snippet for correctness, clarity, and robustness. "
        "Return only the improved version in a single fenced code block, no "
        "prose around it.\n\n{snippet}"
    ),
}


_TAP_INTENT_PATTERNS = {
    "regex": ("regex", "regular expression", "pattern match", "validate email"),
    "sql": ("sql query", "select ", " from ", " where ", "join on", "group by"),
    "bash": ("bash script", "bash one-liner", "shell script", "find -", "awk ", "sed "),
    "debug": ("explain this error", "stack trace", "traceback", "why is this failing", "root cause"),
}


def detect_tap_intent(prompt_text: str) -> str:
    """Return the TAP_TEMPLATES key that best fits the prompt. Defaults to 'generic'."""
    low = (prompt_text or "").lower()
    for intent, needles in _TAP_INTENT_PATTERNS.items():
        if any(n in low for n in needles):
            return intent
    return "generic"


# ----------------------------------------------------------------------------
# Three-mode selector — Local / Tap / Flash. Score-based; lower-scored
# requests stay on the cheap path. metadata.force_cloud wins all races.
# ----------------------------------------------------------------------------


def _flatten_prompt_text(body: dict) -> str:
    """Best-effort concat of user-visible prompt text for scoring."""
    parts: list[str] = []
    msgs = body.get("messages") or []
    for m in msgs:
        c = m.get("content", "")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text":
                    parts.append(b.get("text", ""))
    return " ".join(parts)


def _select_mode(body: dict):
    """Return ('local'|'tap'|'flash', score_dict). Pure function — does NOT
    consume throttle budget. Caller decides whether to reserve.

    Routing waterfall (2026-05-24 operator change):
      PRIMARY  → flash (qwen3-coder:480b-cloud, Tier 1, free SSH-signed)
      FALLBACK → local Ollama (when Tier-1 cloud is down)
      ESCALATE → paid Anthropic tiers (explicit only, metadata.force_cloud/escalate)

    Cloud routing is FREE-FIRST. Tier-1 (Ollama Cloud) is the default target.
    Paid cloud is EXPLICIT ONLY — force_cloud or escalate in metadata.
    Triggers:
      - metadata.force_cloud=True  → flash (full cloud handoff, any tier)
      - metadata.escalate=True     → flash (operator-requested escalation)
      - anything else              → flash (default, Tier-1 free cloud primary)
    """
    meta = body.get("metadata") or {}
    if meta.get("force_cloud") is True:
        return "flash", {"reason": "force_cloud_metadata"}
    if meta.get("escalate") is True:
        return "flash", {"reason": "escalate_metadata"}
    return "flash", {"reason": "default_free_cloud_primary"}


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


def next_cloud_peer(failed_names: set[str]) -> "Provider | None":
    """Return the next enabled cloud peer not in `failed_names`, sorted by tier.

    Used by the rate-limit fallback loop in the orchestrator:

        failed: set[str] = set()
        peer = _pick_cloud_peer()
        while True:
            try:
                response = await call_provider(peer, body)
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 429:
                    raise
                failed.add(peer.name)
                peer = next_cloud_peer(failed)
                if peer is None:
                    raise RuntimeError("all cloud peers rate-limited") from e

    Returns None when the pool is exhausted (all enabled cloud peers are in
    `failed_names`). The caller should surface a 429 or fall back to local.
    """
    cloud = sorted(
        [p for p in PROVIDERS if p.pool == "cloud" and p.enabled and p.name not in failed_names],
        key=lambda p: p.tier,
    )
    return cloud[0] if cloud else None


def pick_cloud_peer(
    *,
    prefer_tiers: tuple[int, ...] | None = None,
    allowed_kinds: tuple[str, ...] | None = None,
    failed_names: set[str] | None = None,
) -> "Provider | None":
    """Pick an enabled cloud peer with optional tier/kind filters.

    This is used by orchestration helpers that need a cloud peer, but not
    necessarily the default tier-ordered one. If filters eliminate the pool,
    returns None instead of raising.
    """
    cloud = [p for p in PROVIDERS if p.pool == "cloud" and p.enabled]
    if failed_names:
        cloud = [p for p in cloud if p.name not in failed_names]
    if allowed_kinds:
        cloud = [p for p in cloud if p.kind in allowed_kinds]
    if not cloud:
        return None
    if prefer_tiers:
        for tier in prefer_tiers:
            match = next((p for p in cloud if p.tier == tier), None)
            if match is not None:
                return match
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

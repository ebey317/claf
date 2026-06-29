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

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

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
    tier: int  # ordering within pool; lower = picked first
    name: str  # short slug used in logs
    pool: str  # "local" | "cloud"
    kind: str  # "ollama" | "openai_compat" | "anthropic"
    model: str  # model tag/id at the provider
    url: str  # endpoint
    env_key: str | None  # env var holding the auth token; None for local
    enabled: bool  # gated by env-key presence (always True for local)
    notes: str = ""
    max_tools: int | None = None  # cap tools array before sending; None = no cap
    max_sys_chars: int | None = None  # override CLAF_CLOUD_SYS_MAX_CHARS for this peer
    max_msgs: int | None = None  # override CLAF_CLOUD_MAX_MSGS for this peer
    max_msg_content: int | None = None  # override CLAF_CLOUD_MSG_CONTENT_MAX per message
    full_context: bool = False  # True = send FULL memory/history untrimmed
    # (capable peers that tolerate large bodies).
    # Charter still prepended; nothing is cut.


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
    notes="local; runs offline; text + tools (NO vision — pull moondream or qwen2.5vl for image understanding)",
)


def _cloud_peers() -> list[Provider]:
    """Flat cloud pool. All peers; no provider hardcoded as preferred.
    Tier numbers are a configurable ordering hint, not a quality ranking."""
    return [
        Provider(
            tier=1,
            name="cerebras",
            pool="cloud",
            kind="openai_compat",
            model="gpt-oss-120b",
            url="https://api.cerebras.ai/v1/chat/completions",
            env_key="CEREBRAS_API_KEY",
            enabled=_env_present("CEREBRAS_API_KEY"),
            notes="PRIMARY flash / OVERSEER — ultra-fast. 32 tools carries a "
            "representative of EVERY capability (terminal+email+task+browser) "
            "so the model can REASON which tool fits instead of being handed "
            "browser-only. 64K ctx, ~55KB body at 32 tools — under limits.",
            max_tools=32,
            max_sys_chars=6000,
            max_msgs=6,
            # 500 beheaded /command skill prompts and hook-prefixed user turns
            # (live test 2026-06-11). 64K ctx: 6 msgs × 3000 ≈ 4.5K tokens — safe.
            max_msg_content=3000,
        ),
        Provider(
            tier=2,
            name="groq",
            pool="cloud",
            kind="openai_compat",
            model="llama-3.1-8b-instant",
            url="https://api.groq.com/openai/v1/chat/completions",
            env_key="GROQ_API_KEY",
            enabled=_env_present("GROQ_API_KEY"),
            notes="text-only fallback — 413s on any tool payload, 14400 req/day.",
            max_tools=0,  # body too large even at 2 tools (11.6KB hits 413)
            max_sys_chars=3000,
            max_msgs=4,
            max_msg_content=400,
        ),
        Provider(
            tier=3,
            name="openrouter",
            pool="cloud",
            kind="openai_compat",
            model="anthropic/claude-sonnet-4.6",
            url="https://openrouter.ai/api/v1/chat/completions",
            env_key="OPENROUTER_API_KEY",
            enabled=_env_present("OPENROUTER_API_KEY"),
            notes="UNIVERSAL GATEWAY — one key, every model. Primary paid tier. Routes to cheapest provider.",
            max_tools=20,
            max_sys_chars=4000,
            max_msgs=8,
        ),
        Provider(
            tier=4,
            name="anthropic",
            pool="cloud",
            kind="anthropic",
            model="claude-sonnet-4-6",
            url="https://api.anthropic.com/v1/messages",
            env_key="ANTHROPIC_API_KEY",
            enabled=_env_present("ANTHROPIC_API_KEY"),
            notes="OVERSEER — Claude Sonnet 4.6 direct. Fallback when OpenRouter fails. Add credits at console.anthropic.com",
            max_tools=20,
            max_sys_chars=4000,
            max_msgs=8,
        ),
        Provider(
            # Ollama Cloud — model deleted (operator removed unused models).
            # Re-enable if model is re-pulled. Disabled to prevent failed calls
            # blocking legitimate cloud peers (Anthropic, etc.).
            tier=5,
            name="ollama-cloud-coder",
            pool="cloud",
            kind="ollama",
            model="qwen3-coder:480b-cloud",
            url=os.environ.get("CLAF_OLLAMA_URL", "http://localhost:11434/api/chat"),
            env_key=None,
            enabled=False,
            notes="DISABLED — model deleted; re-enable after re-pulling qwen3-coder:480b-cloud",
        ),
        Provider(
            tier=6,
            name="deepseek",
            pool="cloud",
            kind="openai_compat",
            model="deepseek-chat",
            url="https://api.deepseek.com/v1/chat/completions",
            env_key="DEEPSEEK_API_KEY",
            enabled=_env_present("DEEPSEEK_API_KEY"),
            notes="DeepSeek direct; enabled when DEEPSEEK_API_KEY is present",
        ),
        Provider(
            tier=7,
            name="openai",
            pool="cloud",
            kind="openai_compat",
            model="gpt-4o-mini",
            url="https://api.openai.com/v1/chat/completions",
            env_key="OPENAI_API_KEY",
            enabled=_env_present("OPENAI_API_KEY"),
            notes="OpenAI direct; enabled when OPENAI_API_KEY is present",
        ),
        Provider(
            tier=8,
            name="fireworks",
            pool="cloud",
            kind="openai_compat",
            model="accounts/fireworks/models/deepseek-v4-pro",
            url="https://api.fireworks.ai/inference/v1/chat/completions",
            env_key="FIREWORKS_API_KEY",
            enabled=False,
            notes="SUSPENDED — monthly limit; re-enable after billing resolved",
            max_tools=40,
        ),
        Provider(
            tier=9,
            name="gemini",
            pool="cloud",
            kind="openai_compat",
            model="gemini-2.5-flash",
            url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            env_key="GEMINI_API_KEY",
            enabled=_env_present("GEMINI_API_KEY"),
            notes="long-context free tier (no key set 2026-05-22)",
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

# Content signals that local 3B models struggle with
_HARD_TASK_SIGNALS = {
    "complex_reasoning": [
        "analyze",
        "evaluate",
        "compare and contrast",
        "deep dive",
        "explain why",
        "root cause",
        "trade-off",
        "pros and cons",
    ],
    "creative": [
        "write a story",
        "write an essay",
        "creative writing",
        "poem",
        "narrative",
        "dialogue",
    ],
    "debug": [
        "debug",
        "fix this",
        "what went wrong",
        "traceback",
        "stack trace",
        "error message",
        "exception",
    ],
    "math_logic": [
        "prove",
        "theorem",
        "equation",
        "calculate",
        "solve for",
        "algorithm",
        "complex logic",
    ],
    "multi_step": [
        "step by step",
        "walk me through",
        "how do i build",
        "create a system",
        "design a",
        "architecture",
    ],
    "action": [
        # Browser/tool intents. The gaming PC local runs CLAF_LOCAL_MAX_TOOLS=0
        # (talk-only), so any turn that needs hands must reach a cloud peer.
        "open a tab",
        "open tab",
        "open mcp",
        "mcp tab",
        "new tab",
        "open chrome",
        "screenshot",
        "take a picture",
        "click",
        "browse",
        "navigate",
        "scroll",
        "web search",
        "search the web",
        "go to http",
        "fill out",
        "fill the form",
        "apply to",
        "read the page",
        ".com",
        ".net",
        ".org",
        # Task-management intents: local 3b misses TaskCreate/TaskUpdate schema.
        # When local returns text-only on these, giveup interceptor replans to cloud.
        "create task",
        "add task",
        "new task",
        "mark complete",
        "mark task",
        "update task",
        "complete task",
        "claim task",
        "finish task",
        "taskcreate",
        "taskupdate",
        "task complete",
        # Email intents: local returns text instead of calling email-bridge tools.
        "check email",
        "check emails",
        "check my email",
        "check inbox",
        "scan email",
        "read email",
        "job related email",
        "email job",
        "any emails",
        "check mail",
        "check all email",
        # Inventory / tool listing intents
        "tool inventory",
        "list tools",
        "what tools",
        "tool list",
    ],
}

# Compile into single regex for fast scanning. The "action" category is
# EXCLUDED here — action turns are local-first work (local holds browser
# tools). Action intents only force cloud when local is tool-less
# (CLAF_LOCAL_MAX_TOOLS=0); that check lives in _is_hard_task/_select_mode.
_HARD_NEEDLES = re.compile(
    r"(?i)("
    + "|".join(
        re.escape(w) for cat, words in _HARD_TASK_SIGNALS.items() if cat != "action" for w in words
    )
    + r")"
)

# Action intents compiled separately so the orchestrator's giveup interceptor
# can ask "was this turn supposed to act?" without re-running full hard-task
# detection (which also matches analysis/creative turns where text is correct).
_ACTION_NEEDLES = re.compile(
    r"(?i)(" + "|".join(re.escape(w) for w in _HARD_TASK_SIGNALS["action"]) + r")"
)

# Web-search intents must reach CLOUD for accurate, current answers — operator
# rule 2026-06-11 — UNLESS off-grid (apocalyptic mode), where local is all we
# have. Scoped to EXPLICIT web phrasing so local DOM "search the page" stays
# local. _is_hard_task gates this on CLAF_MODE.
_WEBSEARCH_NEEDLES = re.compile(
    r"(?i)(search the web|web search|search online|search google|google (?:it|for)|"
    r"look (?:it )?up online|latest news|current (?:price|news|events)|"
    r"what'?s the latest|on the (?:web|internet))"
)


def _last_user_text(msgs) -> str:
    """Flatten the last message's text blocks (tool_result blocks yield '')."""
    if not msgs:
        return ""
    content = msgs[-1].get("content", "")
    if isinstance(content, list):
        return " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content)


def _is_action_turn(body: dict) -> bool:
    """Any user message in the conversation carries a browser/tool action intent.
    Checks last AND first user message — original intent gets buried in history
    as tool_results pile up, so last-only misses the request by turn 3+.
    """
    msgs = body.get("messages") or []
    if not msgs:
        return False
    # Last user message (immediate context)
    if _ACTION_NEEDLES.search(_last_user_text(msgs)):
        return True
    # First user message (original intent, often buried by turn 5+)
    for m in msgs:
        if m.get("role") == "user":
            c = m.get("content", "")
            text = (
                c
                if isinstance(c, str)
                else " ".join(
                    b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"
                )
            )
            if _ACTION_NEEDLES.search(text.lower()):
                return True
            break
    return False


# ----------------------------------------------------------------------------
# Toolbox-command routing. Minted toolbox tools (registry.json) are
# deterministic LOCAL scripts — e.g. `thunderbird_summary` reads cached mail on
# the box. A request that matches a toolbox trigger phrase MUST run local: the
# cloud can't run the local script, so escalating it only burns a cloud peer and
# fails on a 429 (observed 2026-06-14: "summarize my email" → cerebras 429 →
# giveup). This pins such requests to local before any escalation decision.
# ----------------------------------------------------------------------------
_TOOLBOX_REGISTRY = Path(__file__).resolve().parent / "toolbox" / "registry.json"
_toolbox_cmd_cache: dict = {"mtime": None, "variants": []}
_TOOLBOX_STOPWORDS = {
    "my",
    "the",
    "a",
    "an",
    "to",
    "this",
    "that",
    "please",
    "use",
    "lets",
    "for",
    "of",
    "on",
    "in",
    "go",
    "it",
    "me",
    "your",
    "all",
    "and",
}


def _toolbox_variants() -> list[tuple[str, str, set]]:
    """Load (tool_name, full_phrase, distinctive_tokens) from registry.json,
    cached by mtime. Returns [] if the registry is missing or has no phrases."""
    try:
        mtime = _TOOLBOX_REGISTRY.stat().st_mtime
    except OSError:
        return []
    if _toolbox_cmd_cache["mtime"] == mtime:
        return _toolbox_cmd_cache["variants"]
    variants: list[tuple[str, str, set]] = []
    try:
        data = json.loads(_TOOLBOX_REGISTRY.read_text(encoding="utf-8"))
    except Exception:
        return _toolbox_cmd_cache["variants"]
    for tool in data.get("tools", []):
        if not tool.get("enabled", True):
            continue
        name = tool.get("name", "")
        for raw in tool.get("commands") or []:
            phrase = re.sub(r"\[[^\]]*\]", " ", raw.lower())  # drop [url]/[website]
            phrase = " ".join(phrase.split())
            toks = {
                w
                for w in re.findall(r"[a-z]+", phrase)
                if w not in _TOOLBOX_STOPWORDS and len(w) > 2
            }
            variants.append((name, phrase, toks))
    _toolbox_cmd_cache["mtime"] = mtime
    _toolbox_cmd_cache["variants"] = variants
    return variants


def _matches_toolbox_command(body: dict) -> str | None:
    """Return the tool name if the last user message is a minted-toolbox command.

    Matches on a full trigger phrase appearing as substring, or >=2 distinctive
    tokens of a variant overlapping the message. Conservative by design — a
    false positive only pins a request local (no escalation), never the reverse.
    """
    text = _last_user_text(body.get("messages") or []).lower()
    if not text:
        return None
    text_tokens = set(re.findall(r"[a-z]+", text))
    for name, phrase, toks in _toolbox_variants():
        if len(phrase) >= 6 and phrase in text:
            return name
        if len(toks) >= 2 and len(toks & text_tokens) >= 2:
            return name
    return None


def _is_hard_task(body: dict) -> bool:
    """Should this request escalate above local in hybrid mode?

    Two-tier detection:
      1. EXPLICIT signals (always escalate):
         - metadata.escalate = True
         - `[CLOUD]` or `[ESCALATE]` marker in last message
         - system prompt > 12k chars
         - message count > 25
      2. CONTENT signals (escalate if matched):
         - complex reasoning, debugging, math, creative writing,
           multi-step architecture requests

    Routine file reads, simple edits, and pattern-matched tasks stay local.
    """
    # Toolbox-mapped commands are deterministic local tools — never hard.
    if _matches_toolbox_command(body):
        return False
    meta = body.get("metadata") or {}
    if meta.get("escalate") is True:
        return True
    if meta.get("force_cloud") is True:
        return True

    system = body.get("system") or ""
    if isinstance(system, list):
        system_text = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in system)
    else:
        system_text = str(system)
    # Claude Code's normal system prompt (tool definitions + agent instructions)
    # is ~33k chars. The old 12k threshold caused every request to escalate.
    # 100k catches genuinely massive custom prompts while letting normal
    # Claude Code traffic stay local unless other signals trigger escalation.
    if len(system_text) > 100_000:
        return True

    msgs = body.get("messages") or []
    # Detect mid-tool-loop requests: if the history already contains tool_use
    # or tool_result blocks, the agent is executing a multi-step local task.
    # These should stay local even as message count grows, otherwise long
    # bounded loops (e.g. create 60 files) escalate to cloud and die.
    _mid_tool_loop = any(
        isinstance(msg.get("content"), list)
        and any(
            isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result")
            for b in msg["content"]
        )
        for msg in msgs
    )
    # Claude Code conversations routinely hit 25+ messages; the local model
    # already trims to the last 10, so context size isn't the issue. Escalate
    # only when conversations are genuinely massive (150+ turns) AND not a
    # mid-tool-loop that the local model is already driving.
    if len(msgs) > 150 and not _mid_tool_loop:
        return True

    # Scan last user message for explicit markers + content signals
    if msgs:
        # LOCAL-FIRST: agent loops (tool_use/tool_result in history) stay
        # LOCAL — the local model holds tools and continues its own loops.
        # Only when local is explicitly tool-less (CLAF_LOCAL_MAX_TOOLS=0)
        # must acting sessions escalate, since a tool-less local would kill
        # the loop with a chat summary. Cloud is backup, not the default.
        if int(os.environ.get("CLAF_LOCAL_MAX_TOOLS", "6") or "6") == 0:
            for msg in msgs:
                c = msg.get("content", [])
                if isinstance(c, list) and any(
                    isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result") for b in c
                ):
                    return True
            # Tool-less local also can't handle fresh action commands.
            if _ACTION_NEEDLES.search(_last_user_text(msgs)):
                return True
        text = _last_user_text(msgs)
        # Background autocomplete / suggestion-mode turns are noise — never flash.
        # "No tools needed for suggestion" = error tool_result from suggestion loop.
        _LOCAL_EXEMPT = (
            "[suggestion mode]",
            "no tools needed for suggestion",
            "suggest what the user might naturally type",
        )
        if any(ex in text.lower() for ex in _LOCAL_EXEMPT):
            return False
        # Strip hook-injected blocks before needle scan — same issue as tool
        # selection: STANDING ORDERS / SESSION SNAPSHOT contain hard-needle words
        # (architecture, algorithm, analyze) that false-trigger flash.
        import re as _re_ht

        for _hdr in (
            r"\[standing orders\][^\[]*",
            r"\[task_seed_required[^\]]*\][^\[]*",
            r"\[session snapshot\][^\[]*",
            r"\[heartbeat[^\]]*\][^\[]*",
            r"\[non-negotiables\][^\[]*",
            r"\[topology\][^\[]*",
            r"\[retry_schema[^\]]*\][^\[]*",
            r"\[open tasks[^\]]*\][^\[]*",
            r"<system-reminder>.*?</system-reminder>",
        ):
            text = _re_ht.sub(_hdr, " ", text, flags=_re_ht.DOTALL)
        text = " ".join(text.split())
        # Web search → cloud for accuracy/currency, unless off-grid. Operator
        # rule 2026-06-11. (Apocalyptic mode keeps it local — cloud unreachable.)
        if os.environ.get("CLAF_MODE", "hybrid") not in (
            "off_grid",
            "local",
        ) and _WEBSEARCH_NEEDLES.search(text):
            return True
        if "[CLOUD]" in text or "[ESCALATE]" in text:
            return True
        if _HARD_NEEDLES.search(text):
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
    "debug": (
        "explain this error",
        "stack trace",
        "traceback",
        "why is this failing",
        "root cause",
    ),
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

    Routing waterfall (2026-06-08 fix: local first, cloud only on signal):
      PRIMARY  → local Ollama (fast, free, reliable)
      ESCALATE → flash/tap only on hard-task signal or explicit metadata
      FALLBACK → local Ollama (when cloud is down or rate-limited)

    Off-grid is the architecture. Cloud is convenience, not load-bearing.
    Triggers:
      - metadata.force_cloud=True  → flash (full cloud handoff, any tier)
      - metadata.escalate=True     → flash (operator-requested escalation)
      - metadata.emergency=True    → flash (crisis routing; also draws from emergency throttle pool)
      - hard task (_is_hard_task)  → flash (auto-escalation)
      - anything else              → local (default, fast, free)
    """
    # Toolbox-mapped commands run the local deterministic tool — pin local
    # ahead of every escalation trigger (including force_cloud/escalate), since
    # the cloud cannot run the local script.
    _tb = _matches_toolbox_command(body)
    if _tb:
        return "local", {"reason": "toolbox_command_local", "tool": _tb}
    meta = body.get("metadata") or {}
    if meta.get("force_cloud") is True:
        return "flash", {"reason": "force_cloud_metadata"}
    if meta.get("escalate") is True:
        return "flash", {"reason": "escalate_metadata"}
    if meta.get("emergency") is True:
        return "flash", {"reason": "emergency_metadata"}
    if _is_hard_task(body):
        return "flash", {"reason": "hard_task_auto_escalate"}

    # TOOL-INTENT routing — the other half of "cloud owns tools, local owns
    # reasoning". When CLAF_LOCAL_MAX_TOOLS=0 (gaming-PC profile), the local
    # model receives ZERO tool schemas: it literally cannot click, open a tab,
    # or run anything — it can only chat ABOUT the task. So any turn that
    # NEEDS a tool call must go flash:
    #   (a) mid tool-loop: last message carries a tool_result → the agent is
    #       executing a multi-step task and the next turn continues it
    #       (another tool call, or a wrap-up that needs the full context).
    #       Without this, turn 1 escalates + calls TaskList, then turn 2
    #       routes local and the loop dies as a chat summary.
    #   (b) action-intent: the operator's text is a command (open/click/
    #       screenshot/run/...). Routing it local = a model with no hands.
    # Budget safety: flash still goes through throttle.reserve(); when the
    # hourly cap is gone it degrades tap→local exactly like before.
    # LOCAL-FIRST: agent loops and action turns run LOCAL — the local model
    # holds tools (CLAF_LOCAL_MAX_TOOLS>0) and drives its own loops. Cloud
    # only takes tool turns when local is explicitly tool-less (MAX_TOOLS=0),
    # and even then it's the same off-grid-degrades-gracefully posture: no
    # cloud peer available → local still answers, just without hands.
    local_max_tools = int(os.environ.get("CLAF_LOCAL_MAX_TOOLS", "6") or "6")
    if local_max_tools == 0 and body.get("tools"):
        msgs = body.get("messages") or []
        last = msgs[-1] if msgs else {}
        content = last.get("content", "")
        if isinstance(content, list):
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                return "flash", {"reason": "tool_loop_continuation"}
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        if _ACTION_NEEDLES.search(str(content)):
            return "flash", {"reason": "action_intent_needs_tools"}

    # PLANNER-EXECUTOR (overseer) split — the architecture the operator wants:
    #   cloud = overseer (plans, drives the tool loop, decides when done),
    #   local = worker (cheap standalone turns: chat, one-shot lookups, coding).
    # The local 3B is ~100s/turn and stalls on clarifying questions mid-loop,
    # so it CANNOT be the executor inside a live agentic task. Once a task
    # needs tools, the overseer runs the WHOLE loop on cloud (fast, coherent):
    #   - action-intent turn (operator command)       → flash
    #   - tool_result continuation (mid-loop step)     → flash
    # Everything else (plain chat, no tools) stays local. Disable: CLAF_OVERSEER=0.
    if os.environ.get("CLAF_OVERSEER", "1") != "0" and body.get("tools"):
        _pe_msgs = body.get("messages") or []
        _pe_last = _pe_msgs[-1] if _pe_msgs else {}
        _pe_content = _pe_last.get("content", [])
        _pe_is_tool_result = (
            _pe_last.get("role") == "user"
            and isinstance(_pe_content, list)
            and any(isinstance(_b, dict) and _b.get("type") == "tool_result" for _b in _pe_content)
        )
        if _pe_is_tool_result:
            return "flash", {"reason": "overseer_loop_continuation"}
        if _is_action_turn(body):
            return "flash", {"reason": "overseer_plan_turn"}

    return "local", {"reason": "default_local_first"}


# ----------------------------------------------------------------------------
# Routing decision
# ----------------------------------------------------------------------------


def _pick_cloud_peer() -> Provider:
    """Pick the enabled cloud peer.

    If CLAF_PREFERRED_CLOUD is set (e.g. "anthropic"), that provider is
    tried first; otherwise normal tier ordering (lowest tier wins)."""
    cloud = [p for p in PROVIDERS if p.pool == "cloud" and p.enabled]
    if not cloud:
        raise RuntimeError(
            f"mode={MODE} requires at least one cloud peer; none enabled. "
            "Set one of: GROQ_API_KEY, GEMINI_API_KEY, CEREBRAS_API_KEY, "
            "FIREWORKS_API_KEY, OPENROUTER_API_KEY, ANTHROPIC_API_KEY."
        )
    _pref_name = os.environ.get("CLAF_PREFERRED_CLOUD", "").strip().lower()
    if _pref_name:
        for p in cloud:
            if p.name.lower() == _pref_name:
                return p
    return min(cloud, key=lambda p: p.tier)


def next_cloud_peer(failed_names: set[str], max_tier: int = 999) -> "Provider | None":
    """Return the next enabled cloud peer not in `failed_names`, sorted by tier.

    `max_tier` bounds the fallback walk to prevent flash/tap requests from
    leaking into paid tiers. Flash = max_tier=1, tap = max_tier=3, explicit
    escalation = 999 (unbounded). Without this, a Groq 429 cascades all the
    way to Anthropic (tier 4) even for routine flash tasks.

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
    `failed_names` or above `max_tier`). The caller should surface a 429 or
    fall back to local.
    """
    cloud = sorted(
        [
            p
            for p in PROVIDERS
            if p.pool == "cloud" and p.enabled and p.name not in failed_names and p.tier <= max_tier
        ],
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
        assert all(
            p.pool == "local" for p in PROVIDERS
        ), "local mode must contain only local-pool providers"
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
        "cloud_peers_enabled": [p.name for p in PROVIDERS if p.pool == "cloud" and p.enabled],
    }


# ---------------------------------------------------------------------------
# Dynamic local tool selection
# ---------------------------------------------------------------------------

TOOL_GROUPS: dict[str, list[str]] = {
    "browser": [
        # Ordered by loop frequency — when MAX_TOOLS caps the list, the
        # tail gets cut first. search = web search (Google via sensei).
        "mcp__sensei__tab_create",
        "mcp__sensei__read_full",
        "mcp__sensei__click",
        "mcp__sensei__fill",
        "mcp__sensei__screenshot",
        "mcp__sensei__browse",
        "mcp__sensei__search",
        "mcp__sensei__scroll",
        "mcp__sensei__key_press",
    ],
    "filesystem": [
        "Read",
        "Bash",
        "Glob",
        "Grep",
        "Edit",
        "Write",
    ],
    "tasks": [
        "TaskList",
        "TaskCreate",
        "TaskUpdate",
        "TaskGet",
    ],
    "email": [
        # Email-bridge MCP lives on Mary; include all its tools so local can
        # scan inboxes/trash and read/search messages without escalating.
        "mcp__email-bridge__check_inbox",
        "mcp__email-bridge__read_email",
        "mcp__email-bridge__search_inbox",
        "mcp__email-bridge__list_accounts",
        "mcp__email-bridge__list_folders",
    ],
    "core": [
        "TaskList",
        "Read",
        "Bash",
    ],
}

_BROWSER_SIGNALS = {
    "click",
    "screenshot",
    "navigate",
    "browse",
    "tab",
    "page",
    "url",
    "open",
    "website",
    "browser",
    "scroll",
    "fill",
    # web-search intents → browser group (mcp__sensei__search lives there)
    "google",
    "search the web",
    "web search",
    "look up",
}
_FILE_SIGNALS = {
    "read",
    "write",
    "edit",
    "file",
    "grep",
    "glob",
    "bash",
    "run",
    "code",
    "script",
    "directory",
    "path",
    "find",
    "locate",
    "search locally",
    "local search",
    "locally",
    "on my computer",
    "app",
    "application",
    "program",
    "executable",
}
_TASK_SIGNALS = {
    "task",
    "todo",
    "list",
    "create task",
    "update task",
}
_EMAIL_SIGNALS = {
    "email",
    "emails",
    "inbox",
    "trash",
    "folder",
    "folders",
    "aol",
    "gmail",
    "outlook",
    "mail",
    "message",
    "messages",
    "check mail",
    "scan mail",
    "check inbox",
    "scan inbox",
    "job related",
    "indeed",
    "ziprecruiter",
    "linkedin",
    "thunderbird",
}


def select_local_tools(body: dict, all_tools: list[dict]) -> "list[dict] | None":
    """Pick the right tool group for this request.

    If the caller already sent a small set (len ≤ max_tools), pass all of
    them through unchanged — covers agent_runner's custom tools (bash/read_file/
    write_file/done) that should never be stripped.

    For large sets (Claude Code's 30+ tools), scan the prompt and return the
    matching group + core tools capped at max_tools (~1000 tokens vs 6400+).
    Falls back to first max_tools if group lookup finds nothing.

    Returns None when CLAF_LOCAL_MAX_TOOLS=0 (explicit strip-tools mode)."""
    import os

    max_tools = int(os.environ.get("CLAF_LOCAL_MAX_TOOLS", "6"))
    if max_tools == 0:
        return None
    if not all_tools:
        return None

    # Small sets already within budget — pass through untouched.
    if len(all_tools) <= max_tools:
        return all_tools

    tool_map = {t.get("name", ""): t for t in all_tools}

    msgs = body.get("messages") or []

    # LOOP CONTINUITY: only activates when we're genuinely mid-loop — the last
    # message must be a tool_result (continuation), not a fresh user question.
    # Without this guard, any session with prior browser tool_use would force
    # browser tools on every subsequent request, even "read MD files" tasks.
    _last_msg = msgs[-1] if msgs else {}
    _last_content = _last_msg.get("content", [])
    _is_continuation = isinstance(_last_content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in _last_content
    )
    _NAME_TO_GROUP = {n: g for g, names in TOOL_GROUPS.items() if g != "core" for n in names}
    for msg in (reversed(msgs) if _is_continuation else []):
        c = msg.get("content", [])
        if not isinstance(c, list):
            continue
        _hist_group = next(
            (
                _NAME_TO_GROUP[b["name"]]
                for b in c
                if isinstance(b, dict)
                and b.get("type") == "tool_use"
                and b.get("name") in _NAME_TO_GROUP
            ),
            None,
        )
        if _hist_group:
            selected_names: list[str] = []
            for name in TOOL_GROUPS[_hist_group] + TOOL_GROUPS["core"]:
                if name in tool_map and name not in selected_names:
                    selected_names.append(name)
            if selected_names:
                return [tool_map[n] for n in selected_names[:max_tools]]
            break

    # Score last AND first user message. Last = immediate context. First =
    # original intent, which gets buried under tool_results by turn 3+.
    # Example: "check emails" at turn 1 → by turn 5 last msg is inbox JSON,
    # signal scoring misses email keywords, drops email tools on continuation.
    def _extract_user_text(msg):
        c = msg.get("content", "")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(
                b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"
            )
        return ""

    last_user = ""
    first_user = ""
    for m in reversed(msgs):
        if m.get("role") == "user":
            if not last_user:
                last_user = _extract_user_text(m)
            first_user = _extract_user_text(m)  # keeps updating to earliest
    prompt = (last_user + " " + first_user).lower()

    # Strip hook-injected context blocks before scoring. The UserPromptSubmit
    # hook prepends [STANDING ORDERS], [TASK_SEED_REQUIRED], [SESSION SNAPSHOT],
    # etc. — each containing browser/file keywords that corrupt group selection.
    # Example: hook contains "tab_create", "screenshot", "scroll" → browser
    # signals fire on every email or file request, producing multi-group and
    # giving the local model browser tools it then misuses.
    import re as _re

    for _hdr in (
        # Claude Code injects a <system-reminder> block with CLAUDE.md / memory
        # sections; it is packed with browser keywords and must be ignored.
        r"<system-reminder>.*?</system-reminder>",
        r"\[standing orders\][^\[]*",
        r"\[task_seed_required[^\]]*\][^\[]*",
        r"\[session snapshot\][^\[]*",
        r"\[heartbeat[^\]]*\][^\[]*",
        r"\[non-negotiables\][^\[]*",
        r"\[topology\][^\[]*",
        r"\[retry_schema[^\]]*\][^\[]*",
        r"\[open tasks[^\]]*\][^\[]*",
    ):
        prompt = _re.sub(_hdr, " ", prompt, flags=_re.DOTALL)
    prompt = " ".join(prompt.split())  # collapse whitespace

    scores = {
        "browser": sum(1 for s in _BROWSER_SIGNALS if s in prompt),
        "filesystem": sum(1 for s in _FILE_SIGNALS if s in prompt),
        "tasks": sum(1 for s in _TASK_SIGNALS if s in prompt),
        "email": sum(1 for s in _EMAIL_SIGNALS if s in prompt),
    }

    # Multi-group selection: if multiple signal groups are present, include them
    # all (e.g. email + filesystem, browser + filesystem). Otherwise pick the
    # highest-scoring group. Core tools are always appended last so they don't
    # steal budget from specialized groups.
    selected_names: list[str] = []
    active_groups = [g for g in scores if scores[g] > 0]
    if len(active_groups) > 1:
        for group in active_groups:
            for name in TOOL_GROUPS.get(group, []):
                if name in tool_map and name not in selected_names:
                    selected_names.append(name)
    else:
        # Single dominant group (or no signals -> core)
        best_group = active_groups[0] if active_groups else "core"
        for name in TOOL_GROUPS.get(best_group, []):
            if name in tool_map and name not in selected_names:
                selected_names.append(name)
    for name in TOOL_GROUPS["core"]:
        if name in tool_map and name not in selected_names:
            selected_names.append(name)

    if not selected_names:
        return all_tools[:max_tools]

    selected = [tool_map[n] for n in selected_names[:max_tools]]
    return selected if selected else all_tools[:max_tools]

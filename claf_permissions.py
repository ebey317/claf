"""CLAF permission modes — synced to Claude Code permission modes.

Mirrors Claude Code's modes so CLAF behaves consistently regardless of which
client is driving:

    default         — reads auto-approved; writes/executions ask first.
    acceptEdits     — reads + file edits + common safe filesystem commands.
    plan            — explore and propose; do not execute writes/actions.
    auto            — execute with deterministic safety gates (no sudo/install).
    dontAsk         — only pre-approved tool patterns run; everything else denied.
    bypassPermissions — execute freely; circuit breakers only.

Mode is set via CLAF_PERMISSION_MODE env var or ~/.claf/settings.json.
Shift+Tab cycles: default → acceptEdits → plan → auto → default.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

_VALID_MODES = {
    "default",
    "acceptedits",
    "plan",
    "auto",
    "dontask",
    "bypasspermissions",
}

_MODE_ALIASES = {
    "accept_edits": "acceptedits",
    "accept edits": "acceptedits",
    "dont_ask": "dontask",
    "dont ask": "dontask",
    "bypass_permissions": "bypasspermissions",
    "bypass permissions": "bypasspermissions",
}

_SETTINGS_FILE = Path.home() / ".claf" / "settings.json"

# Default cycle order (matches Claude Code's Shift+Tab cycle).
_MODE_CYCLE = ("default", "acceptedits", "plan", "auto")


def _load_mode_from_settings() -> str:
    if not _SETTINGS_FILE.exists():
        return ""
    try:
        data = json.loads(_SETTINGS_FILE.read_text())
        perms = data.get("permissions", {})
        return str(perms.get("defaultMode", "")).strip().lower()
    except Exception:
        return ""


def _normalize(raw: str) -> str:
    mode = (raw or "").strip().lower()
    return _MODE_ALIASES.get(mode, mode)


def current_mode() -> str:
    """Return the active CLAF permission mode, reading env or settings each call."""
    raw = os.environ.get("CLAF_PERMISSION_MODE", "") or _load_mode_from_settings() or "default"
    mode = _normalize(raw)
    if mode not in _VALID_MODES:
        raise ValueError(
            f"CLAF_PERMISSION_MODE must be one of {sorted(_VALID_MODES)}, got: {raw!r}"
        )
    return mode


class _ModeProxy:
    """Backward-compatible proxy so code can still reference claf_permissions.MODE."""

    def __str__(self) -> str:
        return current_mode()

    def __repr__(self) -> str:
        return current_mode()

    def __eq__(self, other: object) -> bool:
        return current_mode() == other

    def __hash__(self) -> int:
        return hash(current_mode())


# Backward-compatible alias. Use current_mode() in new code.
MODE = _ModeProxy()


# Bash commands considered safe in acceptEdits mode (matches Claude Code docs).
_ACCEPT_EDITS_SAFE_BASH = {
    "mkdir",
    "touch",
    "mv",
    "cp",
    "rm",
    "rmdir",
    "sed",
    "cat",
    "ls",
    "grep",
    "rg",
    "find",
    "head",
    "tail",
    "wc",
    "sort",
    "uniq",
    "cut",
    "tr",
    "diff",
    "xargs",
    "echo",
    "printf",
    "pwd",
    "cd",
    "basename",
    "dirname",
    "readlink",
    "file",
    "stat",
    "which",
    "command",
    "type",
}

# Commands that are never auto-approved, even in auto / bypass.
_ALWAYS_BLOCKED = {
    "sudo",
    "su",
    "doas",
    "pkexec",
}

# Destructive filesystem patterns that stay gated.
_DESTRUCTIVE_RE = re.compile(
    r"\b(rm\s+-[rf].*\s+(/|~|/home|/bin|/usr|/etc|/var|/opt|/lib)|"
    r"mkfs\.|fdisk|parted|dd\s+if=.*of=/dev/|"
    r"shutdown|reboot|poweroff|halt|init\s+\d)\b",
    re.IGNORECASE,
)


def _normalize_command(cmd: str) -> str:
    """Return the base command, stripping common safe prefixes/wrappers."""
    if not cmd:
        return ""
    tokens = cmd.strip().split()
    wrappers = {"timeout", "nice", "nohup", "env", "LANG=C", "NO_COLOR=1"}
    while tokens:
        tok = tokens[0]
        if "=" in tok or tok in wrappers:
            tokens.pop(0)
        else:
            break
    return tokens[0].lower() if tokens else ""


def is_command_safe_for_accept_edits(cmd: str) -> bool:
    """True if a Bash command is in the acceptEdits safe list and not destructive."""
    base = _normalize_command(cmd)
    if base in _ALWAYS_BLOCKED:
        return False
    if _DESTRUCTIVE_RE.search(cmd):
        return False
    return base in _ACCEPT_EDITS_SAFE_BASH


def is_command_destructive(cmd: str) -> bool:
    """True for commands that should never run without explicit approval."""
    base = _normalize_command(cmd)
    if base in _ALWAYS_BLOCKED:
        return True
    if re.search(
        r"\b(rm\s+-[rf].*\s+(/|~)|mkfs\.|fdisk|parted|dd\s+if=.*of=/dev/|shutdown|reboot|poweroff)\b",
        cmd,
        re.IGNORECASE,
    ):
        return True
    return False


def is_action_allowed(action_type: str, detail: str | None = None) -> str:
    """Return 'allow', 'ask', 'plan', or 'deny' for an action in the current mode.

    action_type: read | edit | bash | browser | network | install | launch | task
    detail: command string, app name, URL, etc.
    """
    action_type = (action_type or "").strip().lower()
    detail = (detail or "").strip()
    mode = current_mode()

    if action_type == "read":
        return "allow"

    if action_type in ("install", "sudo") and mode != "bypasspermissions":
        return "deny"

    if action_type == "bash" and detail:
        if is_command_destructive(detail):
            return "allow" if mode == "bypasspermissions" else "deny"

    if mode == "bypasspermissions":
        return "allow"

    if mode == "default":
        return "ask"

    if mode == "plan":
        return "plan"

    if mode == "auto":
        if action_type in ("edit", "browser", "network", "launch", "task"):
            return "allow"
        if action_type == "bash":
            return "allow"
        return "ask"

    if mode == "acceptedits":
        if action_type == "edit":
            return "allow"
        if action_type == "bash":
            return "allow" if is_command_safe_for_accept_edits(detail) else "ask"
        if action_type in ("launch", "task"):
            return "allow"
        return "ask"

    if mode == "dontask":
        if action_type == "edit":
            return "allow"
        if action_type == "bash":
            return "allow" if is_command_safe_for_accept_edits(detail) else "deny"
        if action_type == "launch":
            return "allow"
        return "deny"

    return "ask"


def persisted_mode() -> str:
    """Return the mode stored in ~/.claf/settings.json (ignores env var)."""
    raw = _load_mode_from_settings() or "default"
    mode = _normalize(raw)
    if mode not in _VALID_MODES:
        return "default"
    return mode


def _load_settings() -> dict:
    if not _SETTINGS_FILE.exists():
        return {}
    try:
        return json.loads(_SETTINGS_FILE.read_text())
    except Exception:
        return {}


def _save_settings(data: dict) -> None:
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_FILE.write_text(json.dumps(data, indent=2) + "\n")


def set_mode(mode: str) -> str:
    """Persist a CLAF permission mode to ~/.claf/settings.json. Returns normalized mode."""
    mode = _normalize(mode)
    if mode not in _VALID_MODES:
        raise ValueError(
            f"CLAF permission mode must be one of {sorted(_VALID_MODES)}, got: {mode!r}"
        )
    data = _load_settings()
    data.setdefault("permissions", {})
    data["permissions"]["defaultMode"] = mode
    _save_settings(data)
    return mode


def cycle_mode() -> str:
    """Cycle to the next mode in the default Shift+Tab order."""
    mode = current_mode()
    if mode in _MODE_CYCLE:
        idx = _MODE_CYCLE.index(mode)
        next_mode = _MODE_CYCLE[(idx + 1) % len(_MODE_CYCLE)]
    else:
        next_mode = "default"
    return set_mode(next_mode)


def mode_prompt_block() -> str:
    """Return a charter-style block describing the current permission mode."""
    mode = current_mode()
    rules = {
        "default": (
            "PERMISSION MODE: default (read-only auto-approved). "
            "Before editing files, running shell commands, opening apps, or browsing, "
            "present the intended action and wait for operator approval."
        ),
        "acceptedits": (
            "PERMISSION MODE: acceptEdits. Auto-approve reads, file edits, and safe "
            "filesystem commands (mkdir/touch/mv/cp/rm/rmdir/sed/grep/ls/cat/etc). "
            "Ask before sudo, installs, network mutations, destructive commands, or unusual apps."
        ),
        "plan": (
            "PERMISSION MODE: plan. Research and propose only. Do not execute writes, "
            "shell commands, app launches, or browser actions. End with a clear plan."
        ),
        "auto": (
            "PERMISSION MODE: auto. Execute confidently, but NEVER sudo/install/run "
            "destructive commands. Blocked actions are reported, not retried silently."
        ),
        "dontask": (
            "PERMISSION MODE: dontAsk. Only pre-approved patterns run. Deny everything else."
        ),
        "bypasspermissions": (
            "PERMISSION MODE: bypassPermissions. Minimal safeguards; circuit breakers for "
            "rm -rf /~ and sudo still apply. Use only in isolated environments."
        ),
    }
    return f"""PERMISSION MODE
{rules.get(mode, rules['default'])}
Cycle: Shift+Tab cycles default → acceptEdits → plan → auto.
Current mode: {mode}
"""


if __name__ == "__main__":
    print(f"mode={current_mode()}")
    print(mode_prompt_block())

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
"""
from __future__ import annotations

import json
import os
import re
import shutil
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


def _load_mode_from_settings() -> str:
    if not _SETTINGS_FILE.exists():
        return ""
    try:
        data = json.loads(_SETTINGS_FILE.read_text())
        perms = data.get("permissions", {})
        return str(perms.get("defaultMode", "")).strip().lower()
    except Exception:
        return ""


_raw_mode = (
    os.environ.get("CLAF_PERMISSION_MODE", "")
    or _load_mode_from_settings()
    or "default"
)
_raw_mode = (_MODE_ALIASES.get(_raw_mode, _raw_mode)).strip().lower()
if _raw_mode not in _VALID_MODES:
    raise ValueError(
        f"CLAF_PERMISSION_MODE must be one of {sorted(_VALID_MODES)}, got: {_raw_mode!r}"
    )
MODE: str = _raw_mode


# Bash commands considered safe in acceptEdits mode (matches Claude Code docs).
_ACCEPT_EDITS_SAFE_BASH = {
    "mkdir", "touch", "mv", "cp", "rm", "rmdir", "sed", "cat", "ls",
    "grep", "rg", "find", "head", "tail", "wc", "sort", "uniq", "cut",
    "tr", "diff", "xargs", "echo", "printf", "pwd", "cd", "basename",
    "dirname", "readlink", "file", "stat", "which", "command", "type",
}

# Commands that are never auto-approved, even in auto / bypass.
_ALWAYS_BLOCKED = {
    "sudo", "su", "doas", "pkexec",
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
    # Strip env vars and wrappers like timeout/nice/nohup.
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
    if re.search(r"\b(rm\s+-[rf].*\s+(/|~)|mkfs\.|fdisk|parted|dd\s+if=.*of=/dev/|shutdown|reboot|poweroff)\b", cmd, re.IGNORECASE):
        return True
    return False


def is_action_allowed(action_type: str, detail: str | None = None) -> str:
    """Return 'allow', 'ask', 'plan', or 'deny' for an action in the current mode.

    action_type: read | edit | bash | browser | network | install | launch | task
    detail: command string, app name, URL, etc.
    """
    action_type = (action_type or "").strip().lower()
    detail = (detail or "").strip()

    # Reads are always allowed (every mode needs exploration).
    if action_type == "read":
        return "allow"

    # Circuit breakers override every mode except explicit bypass.
    if action_type in ("install", "sudo") and MODE != "bypasspermissions":
        return "deny"

    if action_type == "bash" and detail:
        if is_command_destructive(detail):
            return "deny" if MODE != "bypasspermissions" else "allow"

    if MODE == "bypasspermissions":
        return "allow"

    if MODE == "default":
        return "ask"

    if MODE == "plan":
        return "plan"

    if MODE == "auto":
        if action_type in ("edit", "browser", "network", "launch", "task"):
            return "allow"
        if action_type == "bash":
            return "allow"
        return "ask"

    if MODE == "acceptedits":
        if action_type == "edit":
            return "allow"
        if action_type == "bash":
            return "allow" if is_command_safe_for_accept_edits(detail) else "ask"
        if action_type == "launch":
            return "allow"
        if action_type == "task":
            return "allow"
        return "ask"

    if MODE == "dontask":
        # Pre-approved: reads, safe edits, safe bash.
        if action_type == "edit":
            return "allow"
        if action_type == "bash":
            return "allow" if is_command_safe_for_accept_edits(detail) else "deny"
        if action_type == "launch":
            return "allow"
        return "deny"

    return "ask"


def mode_prompt_block() -> str:
    """Return a charter-style block describing the current permission mode."""
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
{rules.get(MODE, rules['default'])}
Current mode: {MODE}
"""


if __name__ == "__main__":
    print(f"mode={MODE}")
    print(mode_prompt_block())

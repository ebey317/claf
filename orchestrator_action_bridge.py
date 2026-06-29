#!/usr/bin/env python3
"""
CLAF Orchestrator → Action Bridge

Enhances orchestrator.py to detect action directives in LLM responses and
automatically execute them via the action_mcp module, then feed results back
into the conversation context.

This is optional middleware — if action_mcp is available, directives get
executed automatically. If not present, directives pass through as text.

Usage in orchestrator.py:

    # At the top, after the response from ollama_chat() / openai_compat_chat():
    assistant_text, usage = ollama_chat(provider, messages)

    # NEW: Execute any action directives in the response
    assistant_text = execute_actions_in_text(assistant_text)

    # Then continue with directive parsing
    content_blocks, tool_use = parse_directives_to_content(assistant_text, ...)
"""

import re
import json
from typing import Any, Optional

# Try to import action_mcp; graceful degradation if not available
try:
    from action_mcp import parse_and_execute_directives

    HAS_ACTION_MCP = True
except ImportError:
    HAS_ACTION_MCP = False
    parse_and_execute_directives = None


# Match a toolbox runner command preceded by Bash: (plain text outside code blocks).
_TOOLBOX_RUN_RE = re.compile(
    r"(?:^|\s)Bash:\s*(python3\s+~/projects/claf/toolbox/run_tool\.py\s+\S+.*)",
    re.IGNORECASE | re.MULTILINE,
)

# Match a bare toolbox runner command (no Bash:/SHELL: prefix).
_BARE_TOOLBOX_RUN_RE = re.compile(
    r"(?:^|\s)(python3\s+~/projects/claf/toolbox/run_tool\.py\s+\S+.*)",
    re.IGNORECASE | re.MULTILINE,
)

# Match fenced code blocks (``` ... ```) with optional language tag.
_CODE_BLOCK_RE = re.compile(
    r"```(?:\w+)?\n(.*?)\n```",
    re.DOTALL,
)

# Shell-like directive prefixes the model may put inside code blocks.
_DIRECTIVE_PREFIX_RE = re.compile(
    r"^(?:Bash:|SHELL:)\s*",
    re.IGNORECASE,
)

# Browser-open fallback commands the small local model reaches for from its
# training data instead of the open_website toolbox mapping (xdg-open, start,
# firefox, google-chrome ...). The 3B has too strong a prior for these to be
# overridden by charter text alone (verified on Mary 2026-06-14), so the bridge
# catches them deterministically and reroutes to the toolbox tool. This also
# enforces the no-bare-Chrome-tab rule: every open goes through Sensei/MCP.
_BROWSER_FALLBACK_RE = re.compile(
    r"\b(?:xdg-open|google-chrome|chromium-browser|chromium|sensible-browser|"
    r"x-www-browser|firefox|start|open)\s+"
    r"""["']?(?P<url>(?:https?://)?[\w-]+(?:\.[\w-]+)+[^\s"'`)]*)["']?""",
    re.IGNORECASE,
)
_OPEN_WEBSITE_CMD = (
    "python3 ~/projects/claf/toolbox/run_tool.py open_website " '\'{{"url": "{url}"}}\''
)


def _rewrite_browser_fallbacks(text: str) -> str:
    """Rewrite browser-open fallback commands into open_website toolbox calls.

    Converts `xdg-open example.com`, `firefox https://x.com`, etc. into a clean
    `SHELL: python3 ...run_tool.py open_website '{"url": "..."}'` directive so
    the deterministic tool runs instead of a bare browser launch. Bounded by
    newlines so an inline match never swallows surrounding prose.
    """

    def repl(m: re.Match) -> str:
        cmd = _OPEN_WEBSITE_CMD.format(url=m.group("url"))
        return f"\nSHELL: {cmd}\n"

    return _BROWSER_FALLBACK_RE.sub(repl, text)


def _extract_toolbox_commands(text: str) -> list[str]:
    """Find toolbox commands in plain text and inside markdown code blocks.

    The 3B local model on Mary wraps commands in explanatory prose and fenced
    code blocks, and sometimes drops the SHELL:/Bash: prefix entirely. The
    plain-text SHELL: regex also fails inside code blocks because it greedily
    absorbs the closing backticks and surrounding text. This extracts the
    runnable command lines wherever they appear.
    """
    commands = []

    # 1. Plain-text bare or Bash-prefixed toolbox commands.
    for m in _BARE_TOOLBOX_RUN_RE.finditer(text):
        commands.append(m.group(1).strip())

    # 2. Commands inside fenced code blocks.
    for block in _CODE_BLOCK_RE.finditer(text):
        for line in block.group(1).splitlines():
            line = line.strip()
            line = _DIRECTIVE_PREFIX_RE.sub("", line)
            m = _BARE_TOOLBOX_RUN_RE.match(line)
            if m:
                commands.append(m.group(1).strip())

    return commands


def _normalize_toolbox_directives(text: str) -> str:
    """Convert Bash-prefixed toolbox commands in plain text to SHELL: directives.

    The charter emits `Bash: python3 ~/projects/claf/toolbox/run_tool.py ...`
    but the action bridge only recognizes `SHELL:` directives. This normalization
    makes toolbox execution robust for Bash-prefixed commands that appear
    outside of code blocks.
    """

    def repl(m: re.Match) -> str:
        return f" SHELL: {m.group(1)}"

    return _TOOLBOX_RUN_RE.sub(repl, text)


def extract_directives_from_text(text: str) -> list[dict[str, Any]]:
    """
    Find all action directives in text (BROWSE:, SHELL:, FILE:, etc.)
    and return their raw match info.

    Returns a list of (start, end, directive_type, value) tuples.
    """
    patterns = [
        (r"BROWSE\s*:\s*open_url\s*=\s*([^\s]+)", "browse_open_url"),
        (r"BROWSE\s*:\s*search\s*=\s*(.+?)(?=\s[A-Z]+:|$)", "browse_search"),
        (r"SHELL\s*:\s*(.+?)(?=\s[A-Z]+:|$)", "shell_run"),
        (r"FILE\s*:\s*read\s*=\s*([^\s,]+)", "file_read"),
        (r"FILE\s*:\s*write\s*=\s*([^\s,]+)(?:,\s*content\s*=\s*(.+))?", "file_write"),
    ]

    directives = []
    for pattern, directive_type in patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            directives.append(
                {
                    "type": directive_type,
                    "start": m.start(),
                    "end": m.end(),
                    "match": m.group(0),
                    "groups": m.groups(),
                }
            )

    return directives


def execute_actions_in_text(text: str) -> str:
    """
    Scan text for action directives, execute them if action_mcp is available,
    and append results as a [ACTION RESULTS] block at the end.

    If action_mcp is not available, returns text unchanged.

    If directives are found and executed, the response is augmented:

        [Original response text]

        [ACTION RESULTS]
        - BROWSE:open_url=https://google.com → ✓ Opened
        - SHELL:uname -a → Success (Linux ...)
        [/ACTION RESULTS]
    """
    if not HAS_ACTION_MCP or parse_and_execute_directives is None:
        return text  # No action MCP available; pass through

    # 0. Reroute browser-open fallbacks (xdg-open/firefox/start <url>) the small
    #    model emits instead of the open_website mapping. Done before anything
    #    else so the deterministic toolbox tool runs in their place.
    text = _rewrite_browser_fallbacks(text)

    # 1. Normalize Bash-prefixed toolbox commands to SHELL: directives so the
    #    existing action_mcp parser handles them.
    normalized_text = _normalize_toolbox_directives(text)

    # 2. Execute plain-text directives (BROWSE:, SHELL:, FILE:, Bash-prefixed
    #    toolbox commands that were just normalized).
    results = parse_and_execute_directives(normalized_text)

    # 3. The 3B local model on Mary often wraps toolbox commands in ```bash
    #    blocks or emits them without any SHELL:/Bash: prefix. Extract those
    #    explicitly and execute them.
    toolbox_commands = _extract_toolbox_commands(text)
    if toolbox_commands:
        directive_text = "\n".join(f"SHELL: {cmd}" for cmd in toolbox_commands)
        results.extend(parse_and_execute_directives(directive_text))

    # 4. Deduplicate by command to avoid running the same command twice.
    seen = set()
    deduped = []
    for r in results:
        key = r.get("command") or r.get("url") or r.get("path") or json.dumps(r, sort_keys=True)
        if key and key not in seen:
            seen.add(key)
            deduped.append(r)
    results = deduped

    if not results:
        return text  # No directives found

    # Build results summary
    summary_lines = ["[ACTION RESULTS]"]
    for result in results:
        action = result.get("action", "unknown")
        success = result.get("success", False)
        status = "✓" if success else "✗"

        if action == "browse_open_url":
            summary_lines.append(f"{status} Opened {result.get('url')}")
        elif action == "browse_search":
            summary_lines.append(
                f"{status} Searched '{result.get('query')}' on {result.get('engine')}"
            )
        elif action == "shell_run":
            cmd = result.get("command", "")[:40]
            code = result.get("return_code", -1)
            summary_lines.append(f"{status} Ran: {cmd} (exit code {code})")
        elif action == "file_read":
            path = result.get("path", "")
            size = result.get("size", 0)
            summary_lines.append(f"{status} Read {path} ({size} bytes)")
        elif action == "file_write":
            path = result.get("path", "")
            bytes_written = result.get("bytes_written", 0)
            summary_lines.append(f"{status} Wrote {path} ({bytes_written} bytes)")

    summary_lines.append("[/ACTION RESULTS]")
    summary = "\n".join(summary_lines)

    # Append results to original text
    return f"{text}\n\n{summary}"


def action_execution_context(results: list[dict[str, Any]]) -> str:
    """
    Format action execution results for inclusion in the next system message.
    Useful for feeding back to the model what actually happened.

    Example output:
        [ACTION EXECUTION LOG]
        1. BROWSE:open_url=https://google.com → SUCCESS
           Message: ✓ Opened https://google.com in browser

        2. SHELL:uname -a → SUCCESS
           stdout: Linux debian 6.1.0-28-generic #28-Ubuntu ...
        [/ACTION EXECUTION LOG]
    """
    if not results:
        return ""

    lines = ["[ACTION EXECUTION LOG]"]
    for i, result in enumerate(results, 1):
        action = result.get("action", "unknown")
        success = result.get("success", False)
        status = "SUCCESS" if success else "FAILED"

        lines.append(f"{i}. {action} → {status}")

        if result.get("message"):
            lines.append(f"   Message: {result['message']}")
        if result.get("stdout"):
            lines.append(f"   stdout: {result['stdout'][:100]}")
        if result.get("error"):
            lines.append(f"   Error: {result['error']}")

    lines.append("[/ACTION EXECUTION LOG]")
    return "\n".join(lines)


# ============================================================================
# Integration point: patch orchestrator.py
# ============================================================================


def patch_orchestrator_response_handler():
    """
    Monkey-patch instructions for orchestrator.py.

    In orchestrator.py, after getting the LLM response, add:

        from orchestrator_action_bridge import execute_actions_in_text

        # Around line 747, after ollama_chat() call:
        assistant_text, usage = ollama_chat(provider, messages)
        assistant_text = execute_actions_in_text(assistant_text)  # NEW

        # Then continue with directive parsing
        content_blocks, tool_use = parse_directives_to_content(...)
    """
    print("""
    ╔══════════════════════════════════════════════════════════════════════════╗
    ║ ORCHESTRATOR ACTION BRIDGE                                               ║
    ║ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ║
    ║                                                                          ║
    ║ To enable automated action execution in orchestrator.py:                ║
    ║                                                                          ║
    ║ 1. Add this import at the top:                                          ║
    ║    from orchestrator_action_bridge import execute_actions_in_text       ║
    ║                                                                          ║
    ║ 2. In the /v1/messages handler (line ~747), after ollama_chat():        ║
    ║    assistant_text, usage = ollama_chat(provider, messages)              ║
    ║    assistant_text = execute_actions_in_text(assistant_text)  # ADD      ║
    ║                                                                          ║
    ║ 3. Restart orchestrator.py                                              ║
    ║                                                                          ║
    ║ Then directives like BROWSE:open_url=google.com will auto-execute       ║
    ║ and results will be appended to the response.                           ║
    ║                                                                          ║
    ╚══════════════════════════════════════════════════════════════════════════╝
    """)


if __name__ == "__main__":
    patch_orchestrator_response_handler()

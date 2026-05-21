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
            directives.append({
                "type": directive_type,
                "start": m.start(),
                "end": m.end(),
                "match": m.group(0),
                "groups": m.groups(),
            })
    
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
    
    # Execute directives
    results = parse_and_execute_directives(text)
    
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
            summary_lines.append(f"{status} Searched '{result.get('query')}' on {result.get('engine')}")
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

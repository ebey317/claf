#!/usr/bin/env python3
"""Minted tool: read the full DOM of the current Chrome tab or a given URL.

Wraps ~/projects/claf/tools/daddy_dom.py and returns a clean summary.
"""

import json
import subprocess
import sys
from pathlib import Path

DEFAULT_OUTPUT = Path.home() / "projects" / "claf" / "tools" / "daddy_dom_output.json"
TOOL_PATH = Path.home() / "projects" / "claf" / "tools" / "daddy_dom.py"
DEFAULT_URL = "https://kimi.com"


def run(args: dict | None = None) -> str:
    args = args or {}
    url = args.get("url")
    # Treat placeholder or empty value as the default site.
    if url in (None, "", "<url>"):
        url = DEFAULT_URL

    if not TOOL_PATH.exists():
        return f"[tool error] Daddy DOM tool not found at {TOOL_PATH}"

    cmd = [sys.executable, str(TOOL_PATH)]
    if url:
        cmd.extend(["--url", str(url)])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return "[tool error] Daddy DOM scan timed out after 60s"
    except Exception as e:
        return f"[tool error] {e}"

    if result.returncode != 0:
        err = (result.stderr or "").strip() or "unknown error"
        return f"[tool error] daddy_dom.py failed: {err}"

    if not DEFAULT_OUTPUT.exists():
        return "[tool result] Daddy DOM completed but no output file was written."

    try:
        data = json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))
    except Exception as e:
        return f"[tool error] Could not parse Daddy DOM output: {e}"

    url_out = data.get("url", url or "current tab")
    title = data.get("title", "")
    element_count = data.get("elements_captured", 0)
    viewport = data.get("viewport", {})

    summary = (
        f"[tool result] Captured DOM for {url_out}\n"
        f"Title: {title}\n"
        f"Viewport: {viewport}\n"
        f"Elements captured: {element_count}\n"
        f"Full output saved to: {DEFAULT_OUTPUT}"
    )
    return summary


if __name__ == "__main__":
    raw_args = {}
    if len(sys.argv) > 1:
        try:
            raw_args = json.loads(sys.argv[1])
        except Exception:
            raw_args = {}
    print(run(raw_args))

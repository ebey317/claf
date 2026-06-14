#!/usr/bin/env python3
"""Minted tool: open a website in Chrome via the Sensei bridge.

Deterministic replacement for the LLM-driven "go to [url]" path.
Uses action_mcp.py to push a BROWSER_NAV action to the local bridge.
"""
import json
import sys
from pathlib import Path

# Add project root to path so we can import action_mcp
sys.path.insert(0, str(Path.home() / "projects" / "claf"))
import action_mcp  # noqa: E402


DEFAULT_URL = "https://kimi.com"


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url or url == "<url>":
        return DEFAULT_URL
    # Strip quotes if passed as a JSON string literal
    if (url.startswith('"') and url.endswith('"')) or (url.startswith("'") and url.endswith("'")):
        url = url[1:-1]
    if "//" not in url and not url.startswith("http"):
        url = "https://" + url
    return url


def run(args: dict | None = None) -> str:
    args = args or {}
    raw_url = args.get("url") or args.get("website") or ""
    try:
        url = _normalize_url(raw_url)
    except ValueError as e:
        return f"[tool error] {e}"

    results = action_mcp.parse_and_execute_directives(f"BROWSE: open_url={url}")
    if not results:
        return "[tool error] No result from bridge."

    result = results[0]
    if result.get("success"):
        return f"Opened {url} in Chrome."
    message = result.get("message", "unknown error")
    return f"[tool error] Failed to open {url}: {message}"


if __name__ == "__main__":
    raw_args = {}
    if len(sys.argv) > 1:
        try:
            raw_args = json.loads(sys.argv[1])
        except Exception:
            raw_args = {"url": sys.argv[1]}
    print(run(raw_args))

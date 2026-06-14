#!/usr/bin/env python3
"""Daddy DOM Tool — full-page DOM reader for the CURRENT tab.

Reads the tab that is already open in Chrome. If you want to read a new URL,
pass --url and it will open ONE tab first, then read it.

Usage:
    python3 tools/daddy_dom.py                    # read current tab
    python3 tools/daddy_dom.py --url <url>        # open one tab, then read it
    python3 tools/daddy_dom.py --url <url> --output <json_path>

Output JSON:
    {
      "url": "...",
      "title": "...",
      "viewport": {"w": 1412, "h": 911},
      "elements": [
        {"ref": "ref_5", "role": "a", "name": "Superstars",
         "rect": {"x": 255, "y": 0, "w": 106, "h": 60}, ...},
        ...
      ]
    }
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SENSEI_MCP = Path.home() / "projects/master-ai/sensei_mcp_server.py"
DEFAULT_OUT = Path.home() / "projects/claf/tools/daddy_dom_output.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Daddy DOM Tool")
    parser.add_argument("--url", default=None, help="Optional: open this URL in one new tab first")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    proc = subprocess.Popen(
        [sys.executable, str(SENSEI_MCP)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "SENSEI_HEADLESS": "0", "SENSEI_READ_FULL_MAX_CHARS": "100000"},
    )

    def call(method, params=None, req_id=None):
        msg = {
            "jsonrpc": "2.0",
            "id": req_id or int(time.time() * 1000) % 100000,
            "method": method,
        }
        if params:
            msg["params"] = params
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline().strip())

    call(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "daddy-dom", "version": "1.0"},
        },
        1,
    )

    if args.url:
        print(f"[DaddyDOM] Opening one tab: {args.url}")
        r = call("tools/call", {"name": "tab_create", "arguments": {"url": args.url}}, 2)
        print(r["result"]["content"][0]["text"][:200])
        time.sleep(4)
    else:
        print("[DaddyDOM] Reading the current tab...")

    print("[DaddyDOM] Reading full DOM...")
    r = call("tools/call", {"name": "read_full"}, 3)
    text = r["result"]["content"][0]["text"]
    print(f"[DaddyDOM] Raw response length: {len(text)} chars")

    data = json.loads(text)
    result = data["result"]

    url = result.get("url", args.url or "")
    title = result.get("title", "")
    viewport = result.get("viewport", {})
    elements = result.get("elements", [])

    output = {
        "url": url,
        "title": title,
        "viewport": viewport,
        "elements_captured": len(elements),
        "elements": elements,
    }

    args.output.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"[DaddyDOM] Saved {len(elements)} elements to {args.output}")

    proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

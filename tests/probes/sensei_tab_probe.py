#!/usr/bin/env python3
"""Probe: open a URL via Sensei and dump the accessibility/coordinate map.

Proves the local-first browser pipeline:
  1. tab_create opens a fresh tab.
  2. read_full returns labeled elements with (x, y, w, h) rects.
  3. Local can click/fill by ref_N or coordinates without cloud tokens.

Usage:
    python3 tests/probes/sensei_tab_probe.py https://wwe.com
"""
import json, subprocess, sys, os, time

URL = sys.argv[1] if len(sys.argv) > 1 else "https://wwe.com"

proc = subprocess.Popen(
    [sys.executable, os.path.expanduser("~/projects/master-ai/sensei_mcp_server.py")],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, env={**os.environ, "SENSEI_HEADLESS": "0"},
)


def call(method, params=None, req_id=None):
    msg = {"jsonrpc": "2.0", "id": req_id or int(time.time() * 1000) % 100000, "method": method}
    if params:
        msg["params"] = params
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline().strip())


call("initialize", {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "sensei-probe", "version": "1.0"},
}, 1)

print(f"=== TAB_CREATE {URL} ===")
r = call("tools/call", {"name": "tab_create", "arguments": {"url": URL}}, 2)
print(json.dumps(r, indent=2)[:1500])
time.sleep(5)

print("\n=== READ_FULL ===")
r = call("tools/call", {"name": "read_full"}, 3)
text = r["result"]["content"][0]["text"]
print(text[:2000])
if "[...truncated]" in text:
    print("\n[Output truncated by Sensei MCP server; full tree is larger.]")

proc.terminate()

#!/usr/bin/env python3
"""Robust form filler for the current Sensei tab.

The currently-loaded Sensei extension ignores the separate `text`/`value`
parameter on BROWSER_FILL, so this tool bakes the value into the target with
the `::` delimiter (e.g. `#firstName :: Elijah`) and then verifies the field
values with read_full.

Usage:
    python3 tools/form_fill.py                     # fill example values
    python3 tools/form_fill.py --first Jane --last Doe --email jane@example.com
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SENSEI_MCP = Path.home() / "projects/master-ai/sensei_mcp_server.py"

EXAMPLE = {
    "first": "Elijah",
    "last": "Test",
    "email": "elijah.test@example.com",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Robust form filler")
    parser.add_argument("--first", default=EXAMPLE["first"])
    parser.add_argument("--last", default=EXAMPLE["last"])
    parser.add_argument("--email", default=EXAMPLE["email"])
    parser.add_argument("--submit", action="store_true", help="Also click the submit button")
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
        msg = {"jsonrpc": "2.0", "id": req_id or int(time.time() * 1000) % 100000, "method": method}
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
            "clientInfo": {"name": "form-fill", "version": "1.0"},
        },
        1,
    )

    # X-ray the page first.
    print("[FormFill] Reading current page...")
    r = call("tools/call", {"name": "read_full"}, 2)
    data = json.loads(r["result"]["content"][0]["text"])
    elements = data["result"].get("elements", [])

    # Map likely fields by selector id/name.
    selectors = {}
    for el in elements:
        if el.get("role") != "input":
            continue
        sel = el.get("selector", "")
        name = (el.get("name") or "").lower()
        if "first" in name or "first" in sel.lower():
            selectors["first"] = sel
        elif "last" in name or "last" in sel.lower():
            selectors["last"] = sel
        elif "email" in name or "email" in sel.lower():
            selectors["email"] = sel

    if not selectors:
        print("[FormFill] No first/last/email inputs found on the current page.")
        proc.terminate()
        return 1

    print(f"[FormFill] Matched fields: {selectors}")

    # Fill using the :: delimiter workaround.
    values = {"first": args.first, "last": args.last, "email": args.email}
    req = 3
    for key in ("first", "last", "email"):
        sel = selectors.get(key)
        if not sel:
            continue
        target = f"{sel} :: {values[key]}"
        print(f"[FormFill] Filling {key} -> {target}")
        r = call("tools/call", {"name": "fill", "arguments": {"where": target}}, req)
        req += 1
        txt = r["result"]["content"][0]["text"]
        ok = '"ok": true' in txt
        print(f"  {'OK' if ok else 'FAIL'}: {txt[:120]}")

    if args.submit:
        # Find submit button by role or text.
        submit_sel = None
        for el in elements:
            if el.get("role") == "button" and "submit" in (el.get("name") or "").lower():
                submit_sel = el.get("selector") or el.get("name")
                break
        if submit_sel:
            print(f"[FormFill] Clicking submit -> {submit_sel}")
            r = call(
                "tools/call",
                {"name": "click", "arguments": {"what": submit_sel, "intercept_popup": True}},
                req,
            )
            print(r["result"]["content"][0]["text"][:200])
            req += 1

    # Verify by re-reading.
    print("[FormFill] Verifying...")
    r = call("tools/call", {"name": "read_full"}, req)
    data = json.loads(r["result"]["content"][0]["text"])
    for el in data["result"].get("elements", []):
        if el.get("role") == "input":
            print(f"  {el['selector']}: {el.get('value', '')!r}")

    proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

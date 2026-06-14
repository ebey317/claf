#!/usr/bin/env python3
"""Minted tool: summarize all Thunderbird email accounts.

Deterministic replacement for the LLM-driven email-summary path.
Runs scan_thunderbird.py --summary --all and returns clean text.
"""
import json
import subprocess
import sys
from pathlib import Path


def run(args: dict | None = None) -> str:
    args = args or {}
    scanner = Path.home() / "scripts" / "scan_thunderbird.py"
    if not scanner.exists():
        return f"[tool error] Thunderbird scanner not found at {scanner}"

    cmd = [sys.executable, str(scanner), "--summary", "--all"]
    days = args.get("days")
    if days is not None:
        cmd.extend(["--days", str(days)])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return "[tool error] Thunderbird scan timed out after 60s"
    except Exception as e:
        return f"[tool error] {e}"

    if result.returncode != 0:
        err = (result.stderr or "").strip() or "unknown error"
        return f"[tool error] scan_thunderbird.py failed: {err}"

    output = (result.stdout or "").strip()
    if not output:
        return "[tool result] No email data returned from Thunderbird scan."
    return output


if __name__ == "__main__":
    # Accept optional JSON args from CLI or stdin
    raw_args = {}
    if len(sys.argv) > 1:
        try:
            raw_args = json.loads(sys.argv[1])
        except Exception:
            raw_args = {}
    elif not sys.stdin.isatty():
        try:
            raw_args = json.load(sys.stdin)
        except Exception:
            raw_args = {}
    print(run(raw_args))

#!/usr/bin/env python3
"""claf watch — live view of orchestrator routing decisions.

Tails ~/projects/claf/orchestrator.log (JSONL) and renders each event as a
single readable line. Lets the operator see in real time which tier got
picked, how long the model took, whether it spent the whole budget on
thinking, whether an off_grid_lock fired, etc.

Usage:
    python3 ~/projects/claf/watch.py
    CLAF_LOG_FILE=/path/to/log python3 ~/projects/claf/watch.py
"""

import json
import os
import sys
import time
from pathlib import Path

LOG_FILE = Path(
    os.environ.get("CLAF_LOG_FILE", str(Path.home() / "projects/claf/orchestrator.log"))
)


def render(event: dict) -> str:
    ts = event.get("ts", "-")
    e = event.get("event", "?")

    if e == "request_in":
        return (
            f"[{ts}]  →  REQ          model={event.get('model','-')}"
            f"   msgs={event.get('message_count','-')}   sys={'y' if event.get('has_system') else 'n'}"
        )

    if e == "route_decision":
        tier = event.get("picked_tier", "-")
        name = event.get("picked_name", "-")
        model = event.get("picked_model", "-")
        mode = event.get("mode", "-")
        trickle = event.get("trickle_mode", "-")
        env_key = event.get("env_key", "—")
        display = event.get("selected_display", f"{name} -> {model}")
        marker = "▶ local " if tier == 0 else f"▲ tier{tier}"
        return (
            f"[{ts}]    {marker:8} ROUTE   {display:<42}"
            f" key={env_key:<28} mode={mode:<8} lane={trickle}"
        )

    if e == "response_out":
        return (
            f"[{ts}]  ←  OUT          tier={event.get('tier','-')}"
            f"   chars={event.get('out_chars','-'):<6}"
            f"   tokens={event.get('input_tokens',0)}/{event.get('output_tokens',0)}"
        )

    if e == "thinking_only_response":
        return (
            f"[{ts}]  ⚠  THINK-ONLY   model={event.get('model','-')}"
            f"   thinking_chars={event.get('thinking_chars','-')}"
            "  (model burned budget on chain-of-thought, no answer text)"
        )

    if e == "provider_error":
        return (
            f"[{ts}]  ✗  ERR          tier={event.get('tier','-')}"
            f"   name={event.get('name','-')}   {str(event.get('error',''))[:80]}"
        )

    if e == "off_grid_lock":
        return (
            f"[{ts}]  🔒 OFF-GRID-LOCK   refused {event.get('attempted','-')}"
            f"  ({event.get('kind','-')})  — off_grid mode does not call out"
        )

    if e == "ollama_error":
        return f"[{ts}]  ✗  OLLAMA-ERR  {str(event.get('error',''))[:120]}"

    if e == "escalation_not_yet_wired":
        return f"[{ts}]  ⨯  STUB        {event.get('target','-')}  (escalation not wired)"

    return f"[{ts}]  ·  {e:<14} {event}"


def tail_jsonl(path: Path):
    if not path.exists():
        print(f"waiting for log file to appear: {path}", file=sys.stderr)
        while not path.exists():
            time.sleep(1)

    with path.open("r") as f:
        f.seek(0, 2)  # jump to end
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.3)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                print(f"[parse err] {line[:200]}")
                continue
            print(render(event), flush=True)


def live_state():
    """Mirror Claude Code's status bar. statusLine writes /tmp/claude_runtime
    on every refresh; we read it back so both displays show the same two
    boxes: claude=<model> | style=<style>."""
    try:
        with open("/tmp/claude_runtime", "r") as f:
            return [f.read().strip() or "claude=? | style=?"]
    except Exception:
        return ["claude=? | style=?  (status not yet written)"]


def banner():
    print("=" * 78)
    print(f" CLAF WATCH   log={LOG_FILE}")
    for line in live_state():
        print(f" {line}")
    print(f" tailing routing decisions; Ctrl+C to exit")
    print("=" * 78)


if __name__ == "__main__":
    banner()
    try:
        tail_jsonl(LOG_FILE)
    except KeyboardInterrupt:
        print("\nbye.")

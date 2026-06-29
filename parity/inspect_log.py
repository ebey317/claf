#!/usr/bin/env python3
"""Diagnostic: does orchestrator.log contain trainable (prompt -> tool_use) pairs?

Step 1 of the hermes3:3b fine-tune (Path B). Before committing to a train run we
need to know whether the log actually carries the input context AND the emitted
tool call, or just routing telemetry. This reports the honest answer.
"""

import json, os, sys, collections, pathlib

LOG = pathlib.Path(
    os.environ.get("CLAF_LOG", str(pathlib.Path.home() / "projects/claf/orchestrator.log"))
)

events = collections.Counter()
keys_by_event: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
long_string_fields: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
sample_by_event: dict[str, dict] = {}
total = bad = 0
# Heuristics for "carries content we could train on"
carries_prompt = collections.Counter()  # events with user-prompt-ish text
carries_toolcall = collections.Counter()  # events naming an emitted tool

PROMPTY = ("prompt", "messages", "last_user", "user", "text", "input", "query", "snippet")
TOOLY = ("tool", "tool_use", "tools", "name", "emitted", "completion", "response", "content")

with LOG.open("r", errors="ignore") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            bad += 1
            continue
        total += 1
        name = ev.get("event", "<no-event>")
        events[name] += 1
        for k, v in ev.items():
            keys_by_event[name][k] += 1
            if isinstance(v, str) and len(v) > 40:
                long_string_fields[name][k] += 1
        if any(k in ev and isinstance(ev[k], str) and len(str(ev[k])) > 15 for k in PROMPTY):
            carries_prompt[name] += 1
        if any(k in ev for k in TOOLY):
            carries_toolcall[name] += 1
        sample_by_event.setdefault(name, ev)

print(f"LOG: {LOG}  ({LOG.stat().st_size/1e6:.1f} MB)")
print(f"parsed JSON lines: {total}   unparseable: {bad}\n")

print("EVENT HISTOGRAM")
for name, n in events.most_common():
    p = "P" if carries_prompt.get(name) else "-"
    t = "T" if carries_toolcall.get(name) else "-"
    print(f"  {n:6}  [{p}{t}]  {name}")

print("\nLONG-STRING FIELDS PER EVENT (>40 chars — candidate content)")
for name, _ in events.most_common():
    lf = long_string_fields.get(name)
    if lf:
        print(f"  {name}: {dict(lf)}")

# The pairing question: is there an event that holds BOTH the inbound prompt
# context and the emitted tool call? Print the richest samples.
print("\nRICHEST SAMPLES (events that look like they carry content)")
for name in list(events):
    if carries_prompt.get(name) or carries_toolcall.get(name):
        s = sample_by_event[name]
        compact = {k: (str(v)[:160] + ("…" if len(str(v)) > 160 else "")) for k, v in s.items()}
        print(f"\n  --- {name} ---")
        for k, v in compact.items():
            print(f"     {k}: {v}")

print("\nVERDICT HINTS")
print(
    f"  events that carry prompt-ish text:  {sum(carries_prompt.values())} rows across {len(carries_prompt)} event types"
)
print(
    f"  events that name a tool/completion: {sum(carries_toolcall.values())} rows across {len(carries_toolcall)} event types"
)
print("  -> if NO single event holds BOTH the request messages AND the emitted")
print("     tool_use, the log is telemetry-only and we must add request/response")
print("     capture (a logging hook) before harvesting a real dataset.")

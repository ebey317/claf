# CLAF — Closed-Loop Agent Framework

**Off-grid is the architecture. Everything else is convenience.**

Claude Code CLI and the Chrome MCP extension stay exactly as Anthropic ships them. The LLM calls get redirected to a local FastAPI proxy that translates Anthropic's `/v1/messages` into Ollama. By default the proxy cannot reach the internet — even if a stray API key is sitting in your environment. If the grid drops, this still runs.

## What it does

Claude Code thinks it is talking to Anthropic. It is actually talking to CLAF. CLAF:

1. **Routes locally first.** Routine work — file reads, task management, browser automation — stays on-device. No tokens, no latency, no cloud dependency.
2. **Escalates selectively.** Hard tasks (open-ended analysis, web search, multi-step reasoning) get promoted to a cloud peer. The promotion decision is a function of the request, not a user toggle.
3. **Injects surgical context.** Charter slices (identity, browser rules, task patterns, debug hints) are selected per-request and prepended before any trim — so the local model always knows who it is and what the rules are, regardless of context window pressure.
4. **Persists task state.** `~/.claf/current_task.json` is written at task start and injected at the top of every turn. When a loop hits the context cap and restarts, the agent reads the file and picks up exactly where it stopped — no re-derivation, no hallucination about completed work.

## Two-machine topology

```
Madam-Mary (HP Pro, i7-6700T, CPU-only)          Gaming PC (AMD 12-core, GTX 1660 Ti)
────────────────────────────────────────          ─────────────────────────────────────
qwen3.5:2b  — primary local model                qwen3.5:9b — primary workhorse
             vision + tools + thinking                         vision + tools + thinking
glm-ocr:q8_0 — 1.1B OCR/vision test              qwen3-vl:8b — visual automation agent
             topology test under CPU                            GUI interaction, screenshots
                                                  command-r7b, hermes3:3b — topology test
```

Both boxes run the same orchestrator. Models are scoped per machine because the hardware is different — the gaming PC's GPU changes what runs well locally.

## Layout

```
claf/
├── orchestrator.py       # FastAPI proxy — request entry, routing, charter injection
├── claf_config.py        # Provider registry + select_local_tools() + routing signals
├── task_state.py         # Persistent task file: load / format / inject
├── launch.sh             # Sets env + runs `claude --strict-mcp-config`
├── charter/
│   ├── charter_core.md   # Identity + act-first rules (always injected)
│   ├── charter_browser.md
│   ├── charter_tasks.md  # Includes task file lifecycle instructions
│   └── charter_debug.md
├── parity/
│   └── test_parity.py    # 5-layer scorecard: ROUTING / CONTEXT / CAPABILITY / BEHAVIOR / TERMINATION
├── profiles/
│   └── gaming-pc.env     # Per-machine env overrides
└── watch.py              # Live routing log viewer
```

## Routing logic

| Signal | Decision |
|--------|----------|
| `search the web`, `look up`, `current events` | → cloud (accuracy requires live data) |
| `analyze trade-offs`, `explain why`, system prompt > 10K chars | → cloud |
| `read`, `write`, `edit`, `file`, `bash` | → local / filesystem group |
| `click`, `screenshot`, `browse`, `tab` | → local / browser group |
| `task`, `todo`, `list` | → local / tasks group |
| mid-loop `tool_result` continuation | → same group as active loop |
| fresh user message | → re-score from keywords (no history bleed) |

Tool group selection caps at `CLAF_LOCAL_MAX_TOOLS=10` — local models get the right 4–6 tools for the job, not the whole surface.

## Persistent task state

Write `~/.claf/current_task.json` to give any session a durable to-do list:

```json
{
  "goal": "set up Railway deploy for fairchance bot",
  "items": [
    {"id": 1, "task": "write Procfile", "status": "done"},
    {"id": 2, "task": "set WEBHOOK_URL env var", "status": "pending"},
    {"id": 3, "task": "push to Railway", "status": "pending"}
  ]
}
```

The orchestrator injects this at the top of every local turn — before charter, before everything else. The agent updates items by rewriting the file. When all items are done, it deletes the file. Loop cap + restart no longer means starting over.

## Modes

| `CLAF_MODE` | Behavior |
|---|---|
| `off_grid` (default) | Tier 0 only — local Ollama. Cloud-tier provider objects are NOT constructed at import time. There is no live code path to a non-local model. A stray API key in your shell does not change behavior. |
| `hybrid` | Local first, cloud escalation on hard-task signal. Both tiers are live. This is what runs in daily use. |
| `with_convenience` | All tiers active: local → Groq → Gemini → OpenRouter → paid Anthropic. Routing picks per request. |

Defense in depth: even in `hybrid` / `with_convenience`, the orchestrator has an `off_grid_lock` check that 423s any non-local provider when `MODE == off_grid`.

### Permission modes (synced to Claude Code)

CLAF mirrors Claude Code's permission modes so `launch.sh --permission-mode <mode>` matches what Claude Code expects:

| Mode | Auto-approves |
|---|---|
| `default` | Reads only |
| `acceptEdits` | Reads + file edits + common safe filesystem commands |
| `plan` | Reads only; propose without executing |
| `auto` | Most actions, with deterministic safety gates (no sudo/install/destructive) |
| `dontAsk` | Only pre-approved patterns |
| `bypassPermissions` | Everything; circuit breakers only |

Set/cycle from the terminal:

```bash
# Check current mode
python3 ~/projects/claf/toolbox/run_tool.py claf_mode

# Cycle default → acceptEdits → plan → auto
python3 ~/projects/claf/toolbox/run_tool.py claf_mode '{"cycle": true}'

# Set explicitly
python3 ~/projects/claf/toolbox/run_tool.py claf_mode '{"set": "auto"}'
```

Bind **Shift+Tab** in your terminal to cycle CLAF mode (matches Claude Code's shortcut):

```bash
# Add to ~/.bashrc
bind '"\e[Z": "python3 ~/projects/claf/toolbox/run_tool.py claf_mode '"'"'{"cycle": true}'"'"'\n"'
```

The current mode is persisted in `~/.claf/settings.json` and injected into the system prompt on every turn.

## Parity

`parity/test_parity.py` is a 5-layer scorecard that measures whether the local model produces cloud-equivalent outcomes — not prose similarity, but:

| Layer | Question |
|-------|----------|
| ROUTING | Did hard tasks escalate? Did easy tasks stay local? |
| CONTEXT | Did identity + operator's actual words survive the trim? |
| CAPABILITY | Did tools reach local, and did tool calls parse back? |
| BEHAVIOR | Act-first, show evidence, never fake "done"? |
| TERMINATION | Stop when done, ask when stuck, no infinite loops? |

Current scores on Madam-Mary (qwen3.5:2b): ROUTING 9/9, TERMINATION 1/1, PARSER 4/4. The one BEHAVIOR miss is on a cloud turn where Sonnet 4.6 itself failed the PLAN_PREAMBLE check.

## Run

```bash
# Proxy
cd ~/projects/claf && source .venv/bin/activate && python3 orchestrator.py

# Claude Code wired to it
bash ~/projects/claf/launch.sh

# Live routing log (optional)
python3 ~/projects/claf/watch.py
```

## Sanity check

```bash
curl -s http://localhost:8000/healthz | python3 -m json.tool

curl -s http://localhost:8000/v1/messages \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"say hello in 5 words"}]}' \
  | python3 -m json.tool
```

The last call returns an Anthropic-shaped JSON envelope with the local model's text in `content[0].text`.

## Security constraints

- `ANTHROPIC_API_KEY` is never committed. Cloud keys load from `~/.master_ai_keys` at runtime.
- `.env` is gitignored.
- `ANTHROPIC_CONSOLE_KEY` (billing/Console) and the API key are separate and must never be mixed.

## Why off-grid is the default

- Anthropic's runtime (CLI, Chrome MCP, hooks, skills, side panel) is hard to build and gets better every month. It lives on disk and runs offline once installed. Reuse it.
- The LLM call is the part that costs per token AND requires the network. Replace it with local Ollama and you're free of both.
- `$100/mo Claude Code Max` subscription pays for the runtime — not for tokens. The brain runs locally.
- When the subscription ends, the wiring and the extension are still yours. The product is the stack, not the subscription.

## Related

- `~/scripts/sensei_extension/` — Chrome MCP extension, the browser limb. CLAF is the brain swap; sensei is the hands.
- `~/projects/3b-mcp-application/` — job-apply executor, different scope, same off-grid spirit.

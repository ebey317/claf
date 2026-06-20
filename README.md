# CLAF — Closed-Loop Agent Framework

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> **TL;DR:** Run Claude Code without paying per token. Local Ollama handles 90% of tasks; cloud escalates selectively. Off-grid is the architecture, not a toggle.

Claude Code CLI and the Chrome MCP extension stay exactly as Anthropic ships them. The LLM calls get silently redirected to a local FastAPI proxy that translates Anthropic's `/v1/messages` format into Ollama's chat API. In `local` mode the proxy cannot reach the internet — even if a stray API key sits in your shell. If the grid drops, this still runs.

---

## How it works

Claude Code thinks it is talking to Anthropic. It is actually talking to CLAF. CLAF:

1. **Routes locally first.** Routine work — file reads, task management, browser automation — stays on-device. No tokens, no latency, no cloud dependency.
2. **Escalates selectively.** Hard tasks (open-ended analysis, web search, multi-step reasoning) get promoted to a cloud peer. The promotion decision is a function of the request, not a user toggle.
3. **Injects surgical context.** Charter slices (identity, browser rules, task patterns, debug hints) are selected per-request and prepended before any trim — so the local model always knows who it is and what the rules are, regardless of context window pressure.
4. **Persists task state.** `~/.claf/current_task.json` is written at task start and injected at the top of every turn. When a loop hits the context cap and restarts, the agent reads the file and picks up exactly where it stopped.

---

## Repository layout

```
claf/
├── orchestrator.py            # FastAPI proxy — /v1/messages entry, routing, charter injection
├── claf_config.py             # Provider registry, select_provider(), routing signals
├── claf_throttle.py           # Rate-limiting and request metering
├── claf_permissions.py        # Permission-mode sync with Claude Code
├── task_state.py              # Persistent task file: load / save / inject
├── launch.sh                  # Sets ANTHROPIC_BASE_URL + runs claude --strict-mcp-config
├── watch.py                   # Live routing log viewer (tail orchestrator.log)
├── .env.example               # All configurable env vars with defaults and notes
├── charter/
│   ├── charter_core.md        # Identity + act-first rules (always injected)
│   ├── charter_browser.md     # Sensei browser tool rules
│   ├── charter_tasks.md       # Task file lifecycle instructions
│   └── charter_debug.md       # Debug and retry hints
├── parity/
│   └── test_parity.py         # 5-layer scorecard: ROUTING/CONTEXT/CAPABILITY/BEHAVIOR/TERMINATION
├── profiles/
│   └── *.env                  # Per-machine model and URL overrides
└── systemd/                   # User-scope systemd service units
```

---

## Setup

### 1. Prerequisites

- Python 3.10+
- [Ollama](https://ollama.ai) running locally (`ollama serve`)
- At least one model pulled: `ollama pull qwen2.5-coder:latest`
- Claude Code CLI: `npm install -g @anthropic-ai/claude-code`

### 2. Install

```bash
git clone https://github.com/ebey317/claf.git
cd claf
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env — set CLAF_LOCAL_MODEL to your pulled Ollama model
# Cloud keys go in ~/.master_ai_keys (never in .env)
```

### 4. Run

```bash
# Terminal 1 — start the proxy
source .venv/bin/activate
python3 orchestrator.py

# Terminal 2 — start Claude Code wired to the proxy
bash launch.sh

# Terminal 3 — optional live routing log
python3 watch.py
```

### 5. Verify

```bash
curl -s http://localhost:8000/healthz | python3 -m json.tool

curl -s http://localhost:8000/v1/messages \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"say hello in five words"}]}' \
  | python3 -m json.tool
```

The last call returns an Anthropic-shaped JSON envelope with the local model's text in `content[0].text`.

---

## Routing modes

Set via `CLAF_MODE` in `.env`:

| Mode | Behavior |
|---|---|
| `local` | Tier 0 only — Ollama. Cloud provider objects are **not** constructed at import time. A stray API key in your shell does nothing. |
| `hybrid` *(default)* | Local first; cloud escalation on hard-task signal. Both tiers are live. This is what runs in daily use. |
| `cloud` | Cloud peer pool only. Ollama is bypassed. |

Legacy aliases accepted for one upgrade cycle: `off_grid` → `local`, `with_convenience` → `hybrid`.

Defense in depth: even in `hybrid` mode the orchestrator has an `off_grid_lock` check that 423s any non-local provider when `MODE == local`. Paranoia for misconfig.

---

## Routing signals

| Incoming signal | Decision |
|---|---|
| `search the web`, `look up`, `current events` | → cloud (accuracy requires live data) |
| `analyze trade-offs`, `explain why`, system prompt > 10K chars | → cloud |
| `read`, `write`, `edit`, `file`, `bash` | → local / filesystem group |
| `click`, `screenshot`, `browse`, `tab` | → local / browser group |
| `task`, `todo`, `list` | → local / tasks group |
| mid-loop `tool_result` continuation | → same group as active loop |
| fresh user message | → re-score from keywords (no history bleed) |

Tool group selection caps at `CLAF_LOCAL_MAX_TOOLS=10` — local models get the right 4–6 tools for the job, not the whole 30-tool surface.

---

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

The orchestrator injects this at the top of every local turn — before charter, before everything else. The agent updates items by rewriting the file. When all items are done, it deletes the file. Context cap + restart no longer means starting over.

---

## Permission modes

CLAF mirrors Claude Code's permission modes. Set via `CLAF_PERMISSION_MODE` or cycle with **Shift+Tab** after sourcing `claf_shell_integration.sh`:

```bash
# Add to ~/.bashrc
source "$HOME/projects/claf/claf_shell_integration.sh"
```

| Mode | Auto-approves |
|---|---|
| `default` | Reads only |
| `acceptEdits` | Reads + file edits + common safe filesystem commands |
| `plan` | Reads only; propose without executing |
| `auto` | Most actions, with deterministic safety gates (no sudo / install / destructive) |
| `bypassPermissions` | Everything; circuit breakers only |

Current mode is persisted in `~/.claf/settings.json` and injected into the system prompt every turn.

---

## Two-machine topology

Both machines run the same orchestrator. Models are scoped per machine via `profiles/`:

```
Mary (HP Pro i7-6700T, CPU-only)        Elijah (AMD 12-core, GTX 1660 Ti)
─────────────────────────────────        ──────────────────────────────────
qwen3.5:2b  — primary                   qwen2.5-coder:64k — primary workhorse
glm-ocr:q8_0 — vision/OCR test          qwen3-vl:8b — visual automation
                                         hermes3:3b  — fast tool-call model
```

---

## Parity scorecard

`parity/test_parity.py` measures whether the local model produces cloud-equivalent outcomes across 5 layers:

| Layer | Question |
|---|---|
| ROUTING | Did hard tasks escalate? Did easy tasks stay local? |
| CONTEXT | Did identity + operator's actual words survive the trim? |
| CAPABILITY | Did tools reach local, and did tool calls parse back? |
| BEHAVIOR | Act-first, show evidence, never fake "done"? |
| TERMINATION | Stop when done, ask when stuck, no infinite loops? |

Current scores on Mary (qwen3.5:2b): ROUTING 9/9, TERMINATION 1/1, PARSER 4/4.

---

## Security

- **Never commit `.env`** — it is gitignored. Cloud keys load from `~/.master_ai_keys` at runtime.
- **Account separation enforced** — `ANTHROPIC_CONSOLE_KEY` (billing) and `ANTHROPIC_API_KEY` (API) must stay separate. A preflight hook blocks Bash/Edit/Write calls that would mix them.
- **Off-grid lock** — even with cloud keys present, `local` mode returns 423 on any non-Ollama call.

---

## Related

- **[master-ai](https://github.com/ebey317/master-ai)** — Local-first agent stack; Sensei terminal agent and Pupil browser UI
- **[AI Controller](https://github.com/ebey317/-AI-controller.)** — Xbox controller → voice → desktop control

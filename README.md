# CLAF — Closed-Loop Agent Framework

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Last Commit](https://img.shields.io/github/last-commit/ebey317/claf)](https://github.com/ebey317/claf/commits)
[![Language](https://img.shields.io/github/languages/top/ebey317/claf)](https://github.com/ebey317/claf)
[![Stars](https://img.shields.io/github/stars/ebey317/claf?style=social)](https://github.com/ebey317/claf/stargazers)

> Run Claude Code without paying per token. Local Ollama handles 90% of tasks; cloud escalates selectively. Off-grid is the architecture, not a toggle.

---

## What It Does

CLAF is a local proxy that wears Anthropic's skin. Claude Code CLI and the Chrome MCP extension stay exactly as Anthropic ships them — the LLM calls get silently redirected to a local FastAPI proxy that translates Anthropic's `/v1/messages` format into Ollama's chat API. In `local` mode the proxy cannot reach the internet, even if a stray API key sits in your shell. If the grid drops, this still runs.

---

## Why It Exists

Cloud-based LLM agents are powerful but fragile: they depend on network access, meter every token, and silently break when a provider throttles or a connection drops. CLAF inverts that dependency. The routine 90% of agent work — file reads, task management, browser automation, simple edits — runs on-device with zero token cost and zero latency. Cloud is reserved as a selective escalation tier for hard tasks, not a load-bearing path. The goal is local autonomy first, cloud convenience second.

---

## Features

- **Local-first routing** — Routine work stays on-device. No tokens, no latency, no cloud dependency.
- **Selective cloud escalation** — Hard tasks (open-ended analysis, web search, multi-step reasoning) get promoted to a cloud peer. The promotion decision is a function of the request, not a user toggle.
- **Surgical context injection** — Charter slices (identity, browser rules, task patterns, debug hints) are selected per-request and prepended before any trim — so the local model always knows who it is and what the rules are.
- **Persistent task state** — `~/.claf/current_task.json` is written at task start and injected at the top of every turn. Context-cap restarts pick up exactly where the agent stopped.
- **Off-grid lock** — Even with cloud keys present, `local` mode returns 423 on any non-Ollama call. Defense in depth against misconfiguration.
- **Permission-mode sync** — Mirrors Claude Code's permission modes (`default`, `acceptEdits`, `plan`, `auto`, `bypassPermissions`) with deterministic safety gates.
- **Multi-provider cloud tiers** — Supports Anthropic, OpenRouter, Groq, Cerebras, and Fireworks as opt-in escalation providers.
- **Live routing visibility** — `watch.py` tails `orchestrator.log` and pretty-prints every routing decision in real time.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Proxy server | FastAPI + Uvicorn |
| Local model runtime | Ollama (qwen2.5-coder:3b default) |
| Cloud escalation | Anthropic, OpenRouter, Groq, Cerebras, Fireworks |
| CLI client | Claude Code CLI (`@anthropic-ai/claude-code`) |
| Browser automation | Chrome MCP extension + Sensei tool directives |
| Language | Python 3.10+ |
| License | MIT |

---

## Quick Start

### Prerequisites

- Python 3.10+
- [Ollama](https://ollama.ai) running locally (`ollama serve`)
- At least one model pulled: `ollama pull qwen2.5-coder:3b`
- Claude Code CLI: `npm install -g @anthropic-ai/claude-code`

### Install

```bash
git clone https://github.com/ebey317/claf.git
cd claf
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
# Edit .env — set CLAF_LOCAL_MODEL to your pulled Ollama model
# Cloud keys go in ~/.master_ai_keys (never in .env)
```

### Run

```bash
# Terminal 1 — start the proxy
source .venv/bin/activate
python3 orchestrator.py

# Terminal 2 — start Claude Code wired to the proxy
bash launch.sh

# Terminal 3 — optional live routing log
python3 watch.py
```

### Verify

```bash
curl -s http://localhost:8000/healthz | python3 -m json.tool

curl -s http://localhost:8000/v1/messages \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"say hello in five words"}]}' \
  | python3 -m json.tool
```

The last call returns an Anthropic-shaped JSON envelope with the local model's text in `content[0].text`.

---

## Usage

### Routing Modes

Set via `CLAF_MODE` in `.env`:

| Mode | Behavior |
|---|---|
| `local` | Tier 0 only — Ollama. Cloud provider objects are **not** constructed at import time. A stray API key in your shell does nothing. |
| `hybrid` *(default)* | Local first; cloud escalation on hard-task signal. Both tiers are live. This is what runs in daily use. |
| `cloud` | Cloud peer pool only. Ollama is bypassed. |

Legacy aliases accepted for one upgrade cycle: `off_grid` → `local`, `with_convenience` → `hybrid`.

### Routing Signals

| Incoming signal | Decision |
|---|---|
| `search the web`, `look up`, `current events` | → cloud (accuracy requires live data) |
| `analyze trade-offs`, `explain why`, system prompt > 10K chars | → cloud |
| `read`, `write`, `edit`, `file`, `bash` | → local / filesystem group |
| `click`, `screenshot`, `browse`, `tab` | → local / browser group |
| `task`, `todo`, `list` | → local / tasks group |
| mid-loop `tool_result` continuation | → same group as active loop |
| fresh user message | → re-score from keywords (no history bleed) |

### Persistent Task State

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

### Permission Modes

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

---

## Architecture

```
                    ┌─────────────────────────────────────────────┐
                    │              Claude Code CLI                 │
                    │        (+ Chrome MCP extension)             │
                    └───────────────────┬─────────────────────────┘
                                        │  POST /v1/messages
                                        ▼
                    ┌─────────────────────────────────────────────┐
                    │            CLAF FastAPI Proxy               │
                    │          (orchestrator.py)                   │
                    │                                             │
                    │  ┌─────────────┐    ┌──────────────────┐    │
                    │  │  Router     │───►│  Charter Inject  │    │
                    │  │ (claf_)     │    │  (identity +     │    │
                    │  │  config.py) │    │   task state)    │    │
                    │  └──────┬──────┘    └──────────────────┘    │
                    │         │                                   │
                    │    ┌────┴────┐                                │
                    │    ▼         ▼                                │
                    │ LOCAL     CLOUD                               │
                    │ (Tier 0) (Tier 1+)                           │
                    └────┬─────────┬───────────────────────────────┘
                         │         │
                         ▼         ▼
                   ┌──────────┐  ┌──────────────────────────────┐
                   │  Ollama  │  │  Anthropic / OpenRouter /    │
                   │ (local)  │  │  Groq / Cerebras / Fireworks  │
                   └──────────┘  └──────────────────────────────┘

    Routing signals: keywords, tool groups, task state, context size
    Off-grid lock: local mode 423s any non-Ollama call (defense in depth)
```

---

## Contributing

This is a personal off-grid tool, but suggestions and issue reports are welcome.

1. Fork the repository.
2. Create a feature branch: `git checkout -b my-feature`.
3. Commit with clear messages: `git commit -m "feat(scope): description"`.
4. Push and open a Pull Request.

Please do not add cloud-tier code paths that bypass the `off_grid` guard. The whole point is that `local` mode is unreachable to the network. New providers go in `_convenience_tiers()` only. Never hard-code API keys — env vars only.

---

## License

MIT © Elijah Wilkins. See [LICENSE](LICENSE) for full text.

---

## Author / Contact

**Elijah Wilkins** — GitHub: [@ebey317](https://github.com/ebey317)

Related projects:

- **[master-ai](https://github.com/ebey317/master-ai)** — Local-first agent stack; Sensei terminal agent and Pupil browser UI
- **[AI Controller](https://github.com/ebey317/-AI-controller.)** — Xbox controller → voice → desktop control
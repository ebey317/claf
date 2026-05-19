# CLAF — Closed-Loop Agent Framework

**Off-grid is the architecture. Everything else is convenience.**

Claude Code CLI and the Chrome MCP extension stay exactly as Anthropic ships them. The LLM calls get redirected to a local FastAPI proxy that translates Anthropic's `/v1/messages` into a chat call against your local Ollama. By default the proxy literally cannot reach the internet — even if a stray API key is sitting in your environment.

If the grid drops, this still runs.

## Layout

```
claf/
├── orchestrator.py    # FastAPI proxy, port 8000 — request entry + tier dispatch
├── claf_config.py     # provider registry + routing decision (single source of truth)
├── launch.sh          # sets env vars + runs `claude --chrome --strict-mcp-config`
├── requirements.txt   # fastapi, uvicorn, httpx
└── README.md
```

## Modes

| `CLAF_MODE` | Behaviour |
|---|---|
| `off_grid` (default) | Tier 0 only — local Ollama. The cloud-tier provider entries are NOT constructed at import time. There is no live code path to a non-local model. A stray `GROQ_API_KEY` / `OPENROUTER_API_KEY` / etc. in your shell does not change behavior. |
| `with_convenience` | Opt-in. The cloud tiers (Groq / Gemini / OpenRouter / paid Anthropic) are added to the registry. Routing decides per request: routine → local, "hard task" signal → mid-tier free APIs, top-tier paid Anthropic for the heaviest lifts. |

Defense in depth: even in `with_convenience`, the orchestrator has an `off_grid_lock` check — if anything tries to route to a non-local provider while `MODE == off_grid`, the proxy returns 423 Locked, refusing the call. This is paranoia for the case where the registry pruning is bypassed somehow.

A request is "hard" (only meaningful in `with_convenience`) if `metadata.escalate=true`, system prompt > 10k chars, message count > 20, or the last assistant message contained `[ESCALATE]`.

## The off-grid spine (always loaded)

| Tier | Provider | Model | Notes |
|---|---|---|---|
| 0 | local-ollama | qwen3-vl:2b (configurable via `CLAF_LOCAL_MODEL`) | Tools + vision + thinking. ~1.9 GB. |

That's it. The off-grid mode is one row of the table because the off-grid scenario is one row of the table.

## One-time setup

```bash
cd ~/projects/claf
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

Two terminals.

```bash
# Terminal 1 — the proxy (off_grid mode by default)
cd ~/projects/claf
source .venv/bin/activate
python3 orchestrator.py
```

```bash
# Terminal 2 — Claude Code wired to it
bash ~/projects/claf/launch.sh
```

## Sanity check

```bash
curl -s http://localhost:8000/                                # name + version + mode
curl -s http://localhost:8000/healthz | python3 -m json.tool  # provider table + ollama reachability
curl -s http://localhost:8000/v1/models | python3 -m json.tool

curl -s http://localhost:8000/v1/messages \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"say hello in 5 words"}]}' \
  | python3 -m json.tool
```

The last call should return an Anthropic-shaped JSON envelope with the local model's text in `content[0].text`.

## What's still missing

- **Streaming.** No SSE. Proxy 400s on `stream:true`. Add streaming next.
- **Native tool calling.** Tool-use blocks are flattened to text on input; small local models can't emit Anthropic-shape `tool_use` blocks back. The Chrome MCP extension's text-encoded directives (`BROWSER_*`) work; native Anthropic tool_use does not.
- **Vision.** Image blocks are elided to a placeholder. Wire base64 forwarding when text smoke is green.
- **Bigger local brain option.** Default tier-0 is `qwen3-vl:2b` because of the "smallest viable" sizing exercise. For the off-grid scenario the operator actually wants the biggest local model that fits the hardware (~30-100B). Swap `CLAF_LOCAL_MODEL` env var.

## Logs + live watch

Append-only JSONL at `~/projects/claf/orchestrator.log`.

For a readable live view (one line per event — request in, route decision, response out, lock/error events):

```bash
python3 ~/projects/claf/watch.py
```

Output shape (sample lines):
```
[12:33:01]  →  REQ          model=claude-sonnet-4-6   msgs=3   sys=y
[12:33:01]    ▶ local   ROUTE   local-ollama       model=qwen3-vl:2b           mode=off_grid
[12:33:14]  ←  OUT          tier=0   chars=187      tokens=412/96
[12:33:14]  ⚠  THINK-ONLY   model=qwen3-vl:2b   thinking_chars=820  (model burned budget on chain-of-thought, no answer text)
```

So you can SEE the orchestra running — which tier got selected, whether the local model spent its budget thinking, when an off_grid_lock fires.

## Why off-grid is the default

- Operator's brand and identity is local-first. If the grid drops he still needs the agent to work.
- Anthropic's runtime (CLI, Chrome MCP, skills, connectors, side panel, hooks) is the part that's hard to build and gets better every month. That part is reusable shipped software — it lives on disk and runs offline once installed. Reuse it.
- The LLM call is the part that costs per token AND requires the network. Replace it with local Ollama and you're free of both.
- $100/mo Claude Code subscription stays for the runtime (the CLI, the extension, the tool surface) — not for tokens. The brain runs locally.

## If you ever go back online

The convenience tiers exist for the scenario where you're on a fat pipe with API keys and want speed/quality on hard tasks. Set `CLAF_MODE=with_convenience` and the registry expands. Export whichever subset of these you want enabled — any tier whose env key is missing stays disabled even in this mode:

| Tier | Provider | Env key |
|---|---|---|
| 1 | groq-free | `GROQ_API_KEY` |
| 2 | gemini-free | `GEMINI_API_KEY` |
| 3 | openrouter | `OPENROUTER_API_KEY` |
| 4 | anthropic-direct | `CLAF_ANTHROPIC_API_KEY` |

The orchestrator's behavior in `with_convenience` mode is documented in `claf_config.py` — routine work still goes local first, escalation only fires on the hard-task signal.

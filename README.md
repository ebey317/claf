# CLAF — Closed-Loop Agent Framework

Anthropic's skin. Your brain.

Claude Code CLI and the Chrome MCP extension stay exactly as Anthropic ships them. The LLM calls get redirected to a local FastAPI proxy that translates Anthropic's `/v1/messages` format into the right format for whichever provider the router picks — local Ollama by default, optional escalation to Groq / Gemini / OpenRouter / paid Anthropic.

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
| `local_only` (default) | Every request → local Ollama. No API keys touched. |
| `all` | Tier ladder. Routine work → local. "Hard task" signal → mid-tier free APIs. Top-tier paid Anthropic for the heaviest lifts. |

A request is "hard" if `metadata.escalate=true`, system prompt > 10k chars, message count > 20, or the last assistant message contained `[ESCALATE]`.

## Provider ladder (in `claf_config.py`)

| Tier | Provider | Default model | Enable via env var |
|---|---|---|---|
| 0 | local-ollama | qwen3-vl:2b | (always enabled) |
| 1 | groq-free | llama-3.3-70b-versatile | `GROQ_API_KEY` |
| 2 | gemini-free | gemini-2.5-flash | `GEMINI_API_KEY` |
| 3 | openrouter | anthropic/claude-sonnet-4.6 | `OPENROUTER_API_KEY` |
| 4 | anthropic-direct | claude-sonnet-4-6 | `CLAF_ANTHROPIC_API_KEY` |

Tiers whose env key is missing auto-disable. Tier 0 is always on.

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
# Terminal 1 — the proxy
cd ~/projects/claf
source .venv/bin/activate
python3 orchestrator.py
```

```bash
# Terminal 2 — Claude Code wired to it
bash ~/projects/claf/launch.sh
```

To switch to all-mode for a session: `CLAF_MODE=all python3 orchestrator.py` (then export your API keys before starting).

## Sanity check

```bash
curl -s http://localhost:8000/                          # name + version + mode
curl -s http://localhost:8000/healthz | python3 -m json.tool   # provider table + ollama reachability
curl -s http://localhost:8000/v1/models | python3 -m json.tool

curl -s http://localhost:8000/v1/messages \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude-sonnet-4-6","messages":[{"role":"user","content":"say hello in 5 words"}]}' \
  | python3 -m json.tool
```

The last call should return an Anthropic-shaped JSON envelope with the picked provider's text in `content[0].text`.

## What's still missing

- **Streaming.** No SSE. Proxy 400s on `stream:true`. Add streaming next.
- **Native tool calling.** Tool-use blocks are flattened to text on input; small local models can't emit Anthropic-shape `tool_use` blocks back. The Chrome MCP extension's text-encoded directives (`BROWSER_*`) work; native Anthropic tool_use does not.
- **Vision.** Image blocks are elided to a placeholder. Wire base64 forwarding when text smoke is green.
- **Per-request mode override.** Mode is process-global. A `metadata.force_tier=N` knob would let the caller pin a tier per request.

## Logs

Append-only JSONL at `~/projects/claf/orchestrator.log`.

```bash
tail -f ~/projects/claf/orchestrator.log | jq -r '"[\(.ts)] \(.event)  tier=\(.picked_tier // .tier // "-")  name=\(.picked_name // .name // "-")  \(.error // "")"'
```

## Why this exists

- Anthropic's runtime (CLI, Chrome MCP, skills, connectors, side panel, hooks) is the part that's hard to build and gets better every month. Reuse it.
- The LLM call is the part that costs per token. Replace it with local Ollama for the 70-85% of work that's routine.
- $100/mo subscription stays for the hard 5-10% of work that needs Claude-grade reasoning. The middle 10-20% can route through free API tiers (Groq, Gemini, OpenRouter free).
- Operator owns the configuration — not a token dependency. Subscription is the runtime bill, not the brain bill.

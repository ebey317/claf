# CLAF — Notes for future Claude Code sessions in this repo

## ⚠️ Machine names — use these, nothing else

| Name | Hostname | IP | Local model |
|------|----------|----|-------------|
| **Mary** | Madam-Mary | localhost | hermes3:3b + qwen3-vl:2b |
| **Elijah** | elijah-ms-7b86 | 100.121.76.101 (Tailscale/SSH) | qwen2.5-coder:64k (tools) + qwen3-vl:64k (vision) |

Never say "gaming PC", "Madam-Mary", "the box", or raw IPs. Always **Mary** or **Elijah**.
Full handoff dock + open work: `~/MD/HANDOFF.md`

---

## What this is

A local proxy that wears Anthropic's skin: Claude Code CLI + Chrome MCP extension stay as Anthropic ships them; the LLM call gets redirected to a local Ollama. Off-grid is the architecture, not a toggle.

## Defaults are off-grid

- `CLAF_MODE` defaults to `off_grid`. In that mode, `PROVIDERS` contains exactly one entry (`local-ollama`, tier 0). The cloud-tier provider objects are NOT constructed at import time. There is no live code path to a non-local provider, regardless of which API keys are set in env.
- `with_convenience` is opt-in (`CLAF_MODE=with_convenience`). Adds Groq / Gemini / OpenRouter / Anthropic-direct as opt-in escalation tiers.
- Defense in depth: even in `with_convenience`, the orchestrator has an `off_grid_lock` check that 423s any non-ollama provider when `MODE == off_grid`. Paranoia for misconfig.

## Hot files

- `orchestrator.py` — FastAPI proxy. POST `/v1/messages` is the main entrypoint Claude Code hits. `/`, `/healthz`, `/v1/models`, `/stats` round it out.
- `claf_config.py` — provider registry + `select_provider()`. Single source of truth for routing.
- `watch.py` — live view of routing decisions, tails `orchestrator.log` and pretty-prints each event.

## Run

```bash
# T1 — proxy (off_grid by default)
cd ~/projects/claf && source .venv/bin/activate && python3 orchestrator.py

# T2 — Claude Code wired to it
bash ~/projects/claf/launch.sh

# T3 — live visibility (optional)
python3 ~/projects/claf/watch.py
```

## Do NOT

- Add cloud-tier code paths that bypass the `off_grid` guard. The whole point is that off_grid is unreachable to network. New providers go in `_convenience_tiers()` only.
- Hard-code API keys anywhere. Env vars only, read at import time when `MODE == with_convenience`.
- Remove the `off_grid_lock` check in `orchestrator.py:messages()`. Defense-in-depth stays.
- Commit `__pycache__`, `.venv`, `.env`, or logs. `.gitignore` covers them.

## Known caveats

- **Streaming**: proxy 400s on `stream:true`. Add SSE handling when a real consumer needs it.
- **Native tool_use**: tool blocks in Claude's payload are flattened to text. Local models can't emit native Anthropic-shape `tool_use` JSON back. Text-encoded `BROWSER_*` directives (from sensei_extension) still work.
- **Vision**: image blocks elided to placeholders. Wire base64 forwarding when text smoke is consistently green.
- **Thinking models**: handled — if content is empty and thinking is present, the proxy surfaces a tagged fallback so Claude Code doesn't see a silent blank. See commit 0beb628.

## Operator context

- Brand is off-grid / local-first. Architecture decisions favor local autonomy over cloud convenience.
- Cloud tiers exist as a knob for the "back online" scenario, not as load-bearing path.
- `$100/mo Claude Code Max` subscription pays for the runtime (CLI + extension), not for tokens. Local does the routine 90%. Cloud is selective.
- Operator's GitHub handle is `ebey317`. Remote is configured to `https://github.com/ebey317/claf.git` but the repo itself is created/owned by Saun (collaborator) before first push.

## Related repo

- `~/projects/3b-mcp-application/` — sibling project, NOT CLAF. Holds the job-apply master plan for a 3B-model executor walking through Indeed smart-apply. Different scope, same off-grid spirit.

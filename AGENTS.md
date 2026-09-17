# AGENTS.md

This file gives AI coding agents (Claude Code, Cursor, Codex, Hermes, etc.) the
context they need to work productively in this repo. Read it before touching
anything.

## What this project is

> Run Claude Code without paying per token. Local Ollama handles 90% of tasks; cloud escalates selectively. Off-grid is the architecture, not a toggle.

## Repository layout

Top-level layout:

Directories:
- `charter/`
- `config/`
- `docs/`
- `parity/`
- `profiles/`
- `schemas/`
- `scripts/`
- `systemd/`

Key files:
- `AGENTS.md`
- `C23_LATENCY_VERDICT.md`
- `C24_LATENCY_VERDICT.md`
- `C25_HYBRID_FIX_VERDICT.md`
- `CLAUDE.md`
- `CODE_OF_CONDUCT.md`
- `CONTRIBUTING.md`
- `IMPLEMENTATION_PLAN.md`
- `Modelfile`
- `Modelfile.hermes3`

## Hard rules for agents

- `CLAUDE.md` is the source of truth for agent instructions. Don't duplicate guidance here.
- `charter/` and `cloud_charter.md` define the project principles. Changes to those need explicit maintainer sign-off.
- Modelfiles (`Modelfile`, `Modelfile.hermes3`) are version-pinned. New model versions need a new file, not a rewrite.

## Making a safe change

1. Read `README.md` first.
2. Match the project's existing style — read two adjacent files before writing yours.
3. If there is a `CLAUDE.md`, it overrides this file for Claude-specific guidance.
4. Run the test suite (`make test`, `pytest`, or repo-equivalent). If there is no test suite, say so in the PR.
5. Keep `git status` clean — no stray `.bak`, log, or debug files staged.

## Common failure modes

- Throttle config (`claf_throttle.py`) silently reverts on conflict — check git history before changing defaults.

## Project status

This is a public repository. I welcome issues and pull requests — see `CONTRIBUTING.md`
for guidelines and `SECURITY.md` for reporting vulnerabilities.

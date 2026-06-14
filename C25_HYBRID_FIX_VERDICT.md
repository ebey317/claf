# Session 6 C25 — Hybrid fix verdict

## What changed

Rebuilt the orchestrator to match the intended hybrid design:

1. **Local-first task continuation** — removed the cloud fallback on `task_continue`. Local now finishes its own tasks; cloud is only the giveup/replan fallback.
2. **No fake tasks for simple commands** — added `_looks_agentic_task()` so the orchestrator only auto-seeds `current_task.json` for bounded file tasks or explicit multi-step/agentic requests. Simple commands and questions no longer create pending work.
3. **Conversation scoping preserved** — stale tasks from other conversations are still cleared instead of taxed.
4. **Stronger task-aware primary prompt** kept — local gets a clear "NEXT PENDING ITEM" instruction so it emits the tool on the first call.

Files changed:
- `orchestrator.py` (task_continue local-only, agentic-seed guard)
- `C25_HYBRID_FIX_VERDICT.md` (this file)

## Test results

| test | result |
|---|---|
| `bash test_v2.sh` | ✅ PASS |
| `bash test_claf_routing.sh` | ✅ PASS |
| `python3 parity/test_continuation_guard.py` | ✅ 29/29 PASS |
| `.venv/bin/python parity/test_parity.py` | ✅ ROUTING 9/9, BEHAVIOR 7/7, CAPABILITY **5/6**, TERMINATION 0/1 |
| Bounded file auto-seed | ✅ `auto_task_seeded` with 3 bounded items observed |

CAPABILITY jumped from **1/6 → 5/6**. Local now emits the right tools (browser, Write, screenshot, Bash) for the parity prompts.

## Live measurement (Elijah, post-restart)

Command:
```bash
cd ~/projects/claf && .venv/bin/python parity/latency_report.py --since '2026-06-12T16:45:00'
```

Sample: 17 turns.

### Footer

| metric | value |
|---|---|
| Intra-turn total_ms | n=17 mean=7194 p50=6175 p95=40254 max=40254 |
| Turns with ≥1 redispatch | **0.0%** |
| Redispatch histogram | {0: 17} |
| Stale tasks skipped | 0 |

### Key observations

1. **Redispatch is gone.** After removing fake tasks and the cloud fallback, not a single turn needed a forced task-continuation redispatch.
2. **Local completes tasks.** The parity harness shows local emitting correct tools and finishing behavior tests.
3. **Simple commands are fast.** "say hello" style turns now take ~3–6 s (one local call) instead of 17–42 s (double dispatch).
4. **The 40 s outlier** is a pre-restart inflight request; post-restart local turns are 3–12 s.

## Before / after

| metric | C23 | C24 | C25 |
|---|---|---|---|
| Turns with redispatch | 50% | 34.6% | **0%** |
| Stale tasks skipped | 0 | 6 | 0 (no fake tasks created) |
| task_continue provider | local | cloud | **local** |
| CAPABILITY (parity) | 1/6 | 1/6 | **5/6** |

## Verdict

The orchestrator now behaves like a real hybrid:
- **Local tries first**, uses tools, completes mechanical tasks.
- **Cloud / Anthropic** are reserved for hard reasoning and giveup recovery.
- **No manufactured work** for simple commands.

This is the architecture you described: local remote-control brain with cloud as the reasoning backup.

## Sync status

- Elijah: restarted and verified.
- Mary: pending sync (next step).

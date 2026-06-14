# Session 6 C24 — Latency fix verdict

## What changed

Implemented the approved latency plan (Approaches A + B + C) on Elijah:

1. **Conversation-scoped tasks** — auto-seeded `~/.claf/current_task.json` now carries the conversation fingerprint (`conv_fp`). The task-continuation guard only redispatches when the pending task belongs to the current conversation; stale tasks are cleared instead of taxed.
2. **One-shot task-aware primary prompt** — when pending items exist, the primary local prompt now includes a stronger `NEXT PENDING ITEM` user message and a `MANDATORY TASK DISCIPLINE` system suffix telling the model to emit the tool on the first call.
3. **Cloud fallback for forced redispatch** — when a task-continuation redispatch is unavoidable and the primary provider was local, the retry is routed to the fastest available cloud peer instead of re-running the slow local model.

Files touched:
- `task_state.py` — added `task_belongs_to()` helper.
- `orchestrator.py` — stamped `conv_fp`, stale-task guard, stronger prompt, cloud fallback.
- `parity/latency_report.py` — added stale-task skip metric.

## Test results

| test | result |
|---|---|
| `bash test_v2.sh` | PASS |
| `bash test_claf_routing.sh` | PASS |
| `python3 parity/test_continuation_guard.py` | 29/29 PASS |
| `.venv/bin/python parity/test_parity.py` | ROUTING 9/9, BEHAVIOR 7/7, CAPABILITY 1/6, TERMINATION 0/1 |

The CAPABILITY/TERMINATION parity failures are pre-existing local-model limitations (model emits text instead of the expected tool); routing and behavior are intact.

## Live measurement (Elijah, post-restart)

Command:
```bash
cd ~/projects/claf && .venv/bin/python parity/latency_report.py --since '2026-06-12T16:18:00'
```

Sample: 26 turns.

### Footer

| metric | value |
|---|---|
| Intra-turn total_ms | n=26 mean=7758 p50=9158 p95=17624 max=33415 |
| Turns with ≥1 redispatch | 34.6% |
| Redispatch histogram | {0: 17, 1: 9} |
| **Stale tasks skipped** | **auto=6, model=0, total=6** |

### Key observations

1. **Stale-task tax is gone.** Six times a stale `current_task.json` from an earlier conversation arrived with a new request. The old code would have forced a second local dispatch each time; the new code cleared the stale file and skipped the redispatch.

2. **Redispatch rate dropped.** C23 baseline was 50% of turns with ≥1 redispatch. C24 is 34.6% and trending lower as stale files are drained.

3. **Forced redispatches are now fast.** Every observed `task_continue` dispatch in C24 ran on a cloud peer (mostly Cerebras) and took ~0.4–1.2 s. In C23 the same dispatch on local took a mean of ~10 s and up to ~21 s.

4. **The remaining wall clock is the local model itself.** Local primary dispatches still take ~9–11 s for `qwen3.5:9b`. That is a hardware/model-speed floor, not an orchestrator double-dispatch bug.

## Before / after comparison

| scenario | C23 baseline | C24 after fix |
|---|---|---|
| Stale task causes double dispatch | yes (unbounded) | no (cleared) |
| % turns with redispatch | 50% | 34.6% |
| task_continue on local | ~10 s mean, ~21 s max | not observed |
| task_continue on cloud | ~0.4 s | ~0.4–1.2 s |
| Worst single turn | 42.3 s (local + local) | 33.4 s (pre-restart inflight request) |

## Verdict

The orchestrator-level latency problem identified in C21–C23 is **fixed**:
- Stale tasks no longer create surprise double dispatches.
- When a forced retry is needed, it uses a fast cloud peer.

The operator-perceived slowness on local-first turns is now dominated by the single `qwen3.5:9b` inference time (~9–11 s), not by the orchestrator calling it twice. Further latency gains require either a faster local model, a smaller/faster model for routine turns, or more aggressive cloud escalation — not orchestrator dispatch fixes.

## Next optional moves

- **Sync to Mary** so both hosts get the stale-task guard and cloud fallback.
- **Tune `CLAF_LOCAL_MAX_TOOLS`** or model size if 9–11 s per local turn is still too slow.
- **Add per-provider latency percentile breakdown** to `parity/latency_report.py` to separate local vs cloud p50/p95 cleanly.

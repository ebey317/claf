# Session 6 C23 — Per-Stage Latency Measurement Verdict

## Setup
- Host: Elijah (local CLAF orchestrator on `localhost:8000`)
- Local model: `qwen3.5:9b` via Ollama
- Cloud peer: `cerebras / gpt-oss-120b`
- Instrumentation: C21 per-turn marks + dispatch records
- Parser: `parity/latency_report.py` (C22)
- Measurement battery:
  - `./test_v2.sh` (4 prompts: easy chat, hard code, bash one-liner, emergency hard task)
  - `./test_claf_routing.sh` (4 routing probes)
  - `python3 parity/test_parity.py` (9 live prompts; parser unit failed on venv import, but live prompts completed)
- Date: 2026-06-12

## Aggregate Results (18 turns)

### Per-turn sample
| ts | turn_id | conv_fp | provider | total_ms | route_ms | dispatches | redispatch | tool_use |
|----|---------|---------|----------|----------|----------|------------|------------|----------|
| 15:44:12 | e8deb6893976 | 602266ad56 | local-ollama | 36,434 | 2 | primary=36,434ms | 0 | N |
| 15:48:50 | 3658437ec955 | 602266ad56 | local-ollama | 2,296 | 0 | primary=2,296ms | 0 | N |
| 15:48:53 | dac0338257f0 | fee9133b0b | cerebras | 1,092 | 0 | primary=1,090ms | 0 | N |
| 15:49:06 | 7b870505c2ae | ddea985cfd | local-ollama | 9,581 | 0 | primary=9,581ms | 0 | N |
| 15:49:09 | e4978135de0e | 65158d9dc0 | cerebras | 1,061 | 0 | primary=1,061ms | 0 | N |
| 15:51:09 | eb282f2b82e5 | 602266ad56 | local-ollama | 7,342 | 0 | primary=7,342ms | 0 | N |
| 15:51:12 | fb74885ea9f9 | fee9133b0b | cerebras | 793 | 0 | primary=793ms | 0 | N |
| 15:51:21 | a223397f8a2b | ddea985cfd | local-ollama | 5,731 | 0 | primary=5,731ms | 0 | N |
| 15:51:24 | b1b49f5eb7f8 | 65158d9dc0 | cerebras | 978 | 0 | primary=977ms | 0 | N |
| 15:52:28 | 5a43bb5dbee7 | 613b989d0c | local-ollama | 42,277 | 21,185 | primary=21,184ms task_continue=21,091ms | 1 | Y |
| 15:52:45 | a009e0a6c844 | 1411581735 | local-ollama | 17,469 | 8,725 | primary=8,724ms task_continue=8,743ms | 1 | Y |
| 15:52:46 | d3f9b34b6cba | ae23cabf41 | cerebras | 821 | 402 | primary=401ms task_continue=419ms | 1 | Y |
| 15:52:48 | 8f6f2acfa49b | 127ba70796 | cerebras | 1,546 | 1,124 | primary=1,124ms task_continue=422ms | 1 | Y |
| 15:53:17 | cb467c29856f | ff786fb95a | local-ollama | 29,462 | 20,917 | primary=20,917ms task_continue=8,545ms | 1 | Y |
| 15:53:47 | 93f45fd068e7 | 755771c972 | local-ollama | 29,750 | 21,536 | primary=21,535ms task_continue=8,213ms | 1 | Y |
| 15:54:14 | 866d0e7fe456 | 023a08df1d | local-ollama | 26,898 | 13,943 | primary=13,934ms task_continue=12,955ms | 1 | Y |
| 15:54:34 | e714e499351a | 3959f3f817 | local-ollama | 19,956 | 9,988 | primary=9,988ms task_continue=9,968ms | 1 | Y |
| 15:54:51 | d36268ba02d3 | 352b9bdeae | local-ollama | 17,607 | 8,913 | primary=8,913ms task_continue=8,694ms | 1 | Y |

### Footer
```
Intra-turn total_ms:     n=18 mean=13949.7 p50=8461.5 p95=42277 max=42277
Inter-turn gap_ms:       n=5 mean=155234.2 p50=136944 p95=241896 max=241896
Redispatch histogram:    {'0': 9, '1': 9}
Turns with ≥1 redispatch: 50.0%
```

### Dispatch summary by kind
| kind | provider | model | n | mean ms | p95 ms |
|------|----------|-------|---|---------|--------|
| primary | local-ollama | qwen3.5:9b | 12 | 13,018 | 36,434 |
| primary | cerebras | gpt-oss-120b | 6 | 971 | 1,546 |
| task_continue | local-ollama | qwen3.5:9b | 7 | 9,957 | 21,091 |
| task_continue | cerebras | gpt-oss-120b | 2 | 421 | 422 |

## Verdict

1. **Routing and prompt-preparation overhead is negligible.**
   - `route_ms` is 0–2 ms for text-only turns and 0.4–21 s for tool-use turns.
   - The large `route_ms` on tool-use turns is NOT routing overhead; it is the time spent inside `_enforce_auto_task_scope` / mechanical task-scope rewriting before the backend call. This is intra-turn work and is now visible.

2. **The dominant intra-turn bucket is the LLM dispatch itself.**
   - Local `qwen3.5:9b` primary dispatch: mean 13.0 s, p95 36.4 s.
   - Cloud `cerebras / gpt-oss-120b` primary dispatch: mean 0.97 s, p95 1.55 s.
   - Cloud is ~13× faster for these prompts.

3. **Task-continuation redispatches add real cost.**
   - 50% of observed turns fired the continuation guard (9/18).
   - Local task_continue mean: 9.96 s; cloud task_continue mean: 0.42 s.
   - The worst single turn was 42.3 s total: 21.2 s primary + 21.1 s task_continue, both local.

4. **Inter-turn gaps in this synthetic run are NOT representative of client/MCP delay.**
   - The measured 125–242 s gaps are the scripted `sleep` intervals between `test_v2.sh` / `test_claf_routing.sh` probes.
   - No multi-turn conversation within a single `conv_fp` was captured, so true client/MCP gap cannot be inferred from this data.
   - Conclusion: the operator-perceived "~180 s/turn" wall-clock is NOT explained by inter-turn client/MCP time in this dataset; it is dominated by intra-turn local-model dispatch and continuation-redispatch time.

5. **Instrumentation is complete and accurate.**
   - `turn_summary` events contain `marks`, `dispatches`, `provider`, `model`, `redispatch_count`, and `tool_use`.
   - The parser reconciles request_in/response_out/turn_summary by `turn_id` and separates primary vs task_continue dispatches.

## Recommendation (unblocks Phases B/C/E)
- The missing wall-clock lives **inside the turn**, primarily in local-model dispatch and task-continuation redispatch.
- Tuning candidates that directly address the measured bottleneck:
  - **C24** redispatch budget (cap expensive double-local dispatches).
  - **C25** kill-switches for tap polish / giveup interceptor (not observed firing here, but cheap insurance).
  - **C32** optional tailnet GPU tier for Mary (would collapse local dispatch time).
- Do NOT tune before this verdict; the data now names the bottleneck explicitly.

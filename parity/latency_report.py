#!/usr/bin/env python3
"""CLAF latency report (Session 6 C22).

Parses orchestrator.log JSONL, ties request_in / response_out / turn_summary
events together by turn_id, and prints:
  - per-turn table with stage + dispatch breakdown
  - inter-turn gap analysis per conversation fingerprint
  - footer with intra-turn and inter-turn percentiles + redispatch histogram

Usage:
    python3 parity/latency_report.py
    python3 parity/latency_report.py --since 2026-06-12T00:00:00
    python3 parity/latency_report.py --json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG = Path.home() / "projects/claf/orchestrator.log"


def _parse_iso(ts: str) -> datetime:
    # Accept "2026-06-12T15:43:35" or with Z / offset.
    ts = ts.replace("Z", "+00:00")
    if "+" not in ts and "-" not in ts[10:]:
        ts += "+00:00"
    return datetime.fromisoformat(ts)


def _pctiles(values: list[float | int]) -> dict[str, float | int]:
    if not values:
        return {"n": 0, "mean": 0, "min": 0, "p50": 0, "p95": 0, "max": 0}
    s = sorted(values)
    n = len(s)
    mean = sum(s) / n
    p50 = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    p95 = s[int(n * 0.95)] if n > 1 else s[0]
    return {
        "n": n,
        "mean": round(mean, 1),
        "min": s[0],
        "p50": round(p50, 1),
        "p95": round(p95, 1),
        "max": s[-1],
    }


def _parse_log(
    path: Path, since: datetime | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    requests: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    stale_tasks: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since is not None:
                try:
                    ev_dt = _parse_iso(ev.get("ts", "1970-01-01T00:00:00"))
                except Exception:
                    continue
                if ev_dt < since:
                    continue
            ev_type = ev.get("event")
            if ev_type == "request_in":
                requests.append(ev)
            elif ev_type == "response_out":
                responses.append(ev)
            elif ev_type == "turn_summary":
                summaries.append(ev)
            elif ev_type in (
                "stale_auto_task_cleared_skipping_redispatch",
                "stale_model_task_skipping_redispatch",
            ):
                stale_tasks.append(ev)
    return requests, responses, summaries, stale_tasks


def _build_turns(requests, responses, summaries) -> list[dict[str, Any]]:
    req_by_turn = {r["turn_id"]: r for r in requests if "turn_id" in r}
    resp_by_turn = {r["turn_id"]: r for r in responses if "turn_id" in r}

    turns: list[dict[str, Any]] = []
    for summary in summaries:
        tid = summary.get("turn_id")
        req = req_by_turn.get(tid)
        resp = resp_by_turn.get(tid)
        marks = summary.get("marks") or {}
        dispatches = summary.get("dispatches") or []
        dispatch_by_kind: dict[str, list[int]] = defaultdict(list)
        for d in dispatches:
            dur = d.get("end_ms", 0) - d.get("start_ms", 0)
            dispatch_by_kind[d.get("kind", "unknown")].append(dur)
        dispatch_summary = {k: _pctiles(v) for k, v in dispatch_by_kind.items()}
        total_ms = summary.get("total_ms", 0)
        route_ms = marks.get("t_prompt_ready", 0) - marks.get("t_request_in", 0)
        turns.append(
            {
                "ts": summary.get("ts", ""),
                "ts_ms": summary.get("ts_ms"),
                "turn_id": tid,
                "conv_fp": summary.get("conv_fp", "0000000000"),
                "message_count": summary.get("message_count", 0),
                "total_ms": total_ms,
                "route_ms": route_ms,
                "dispatch_summary": dispatch_summary,
                "redispatch_count": summary.get("redispatch_count", 0),
                "provider": summary.get("provider", "unknown"),
                "model": summary.get("model", "unknown"),
                "pool": summary.get("provider_pool", "unknown"),
                "tool_use": summary.get("tool_use", False),
                "stream": summary.get("stream", False),
                "status": summary.get("status", "ok"),
                "request_in": req,
                "response_out": resp,
            }
        )
    turns.sort(key=lambda t: t.get("ts_ms") or 0)
    return turns


def _inter_turn_gaps(turns: list[dict[str, Any]]) -> dict[str, list[int]]:
    """For each conv_fp, compute gaps between consecutive response_out / request_in pairs."""
    by_conv: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in turns:
        by_conv[t["conv_fp"]].append(t)
    gaps: dict[str, list[int]] = defaultdict(list)
    for conv_fp, conv_turns in by_conv.items():
        conv_turns.sort(key=lambda t: t.get("ts_ms") or 0)
        for prev, cur in zip(conv_turns, conv_turns[1:]):
            prev_resp_ms = prev.get("ts_ms")
            cur_req_ms = cur.get("ts_ms")
            if prev_resp_ms is None or cur_req_ms is None:
                continue
            # Approximate: request_in ts_ms is close to turn start; use total_ms to
            # estimate response_out ts_ms.
            gap = cur_req_ms - (prev_resp_ms + prev["total_ms"])
            if gap >= 0:
                gaps[conv_fp].append(gap)
    return gaps


def _print_table(turns: list[dict[str, Any]]) -> None:
    headers = [
        "ts",
        "turn_id",
        "conv_fp",
        "provider",
        "total",
        "route",
        "dispatches",
        "redispatch",
        "tool_use",
        "status",
    ]
    print(
        " ".join(
            f"{h:<18}" if h in ("ts", "turn_id") else f"{h:<12}" if h == "conv_fp" else f"{h:<10}"
            for h in headers
        )
    )
    print("-" * 130)
    for t in turns:
        disp_str = " ".join(f"{k}={v['mean']}ms" for k, v in t["dispatch_summary"].items())
        print(
            f"{t['ts']:<18} "
            f"{t['turn_id']:<18} "
            f"{t['conv_fp']:<12} "
            f"{t['provider']:<10} "
            f"{t['total_ms']:<10} "
            f"{t['route_ms']:<10} "
            f"{disp_str:<30} "
            f"{t['redispatch_count']:<10} "
            f"{'Y' if t['tool_use'] else 'N':<10} "
            f"{t['status']:<10}"
        )


def _print_footer(
    turns: list[dict[str, Any]], gaps: dict[str, list[int]], stale_tasks: list[dict[str, Any]]
) -> dict[str, Any]:
    intra = _pctiles([t["total_ms"] for t in turns])
    all_gaps = [g for gg in gaps.values() for g in gg]
    gap_stats = _pctiles(all_gaps)
    redispatch_hist: dict[str, int] = defaultdict(int)
    for t in turns:
        redispatch_hist[str(t["redispatch_count"])] += 1
    pct_redispatched = (
        round(100 * sum(1 for t in turns if t["redispatch_count"] > 0) / len(turns), 1)
        if turns
        else 0
    )

    stale_auto = sum(
        1 for s in stale_tasks if s.get("event") == "stale_auto_task_cleared_skipping_redispatch"
    )
    stale_model = sum(
        1 for s in stale_tasks if s.get("event") == "stale_model_task_skipping_redispatch"
    )

    print("\n=== Footer ===")
    print(
        f"Intra-turn total_ms:     n={intra['n']} mean={intra['mean']} p50={intra['p50']} p95={intra['p95']} max={intra['max']}"
    )
    print(
        f"Inter-turn gap_ms:       n={gap_stats['n']} mean={gap_stats['mean']} p50={gap_stats['p50']} p95={gap_stats['p95']} max={gap_stats['max']}"
    )
    print(f"Redispatch histogram:    {dict(redispatch_hist)}")
    print(f"Turns with ≥1 redispatch: {pct_redispatched}%")
    print(
        f"Stale tasks skipped:     auto={stale_auto} model={stale_model} total={stale_auto + stale_model}"
    )

    return {
        "intra_turn_ms": intra,
        "inter_turn_gap_ms": gap_stats,
        "redispatch_histogram": dict(redispatch_hist),
        "pct_turns_with_redispatch": pct_redispatched,
        "stale_tasks": {
            "auto": stale_auto,
            "model": stale_model,
            "total": stale_auto + stale_model,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="CLAF latency report")
    parser.add_argument("--log", type=Path, default=LOG, help="Path to orchestrator.log")
    parser.add_argument("--since", type=str, default=None, help="ISO timestamp lower bound")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of table")
    args = parser.parse_args()

    if not args.log.exists():
        print(f"Log file not found: {args.log}", file=sys.stderr)
        return 1

    since_dt = _parse_iso(args.since) if args.since else None
    requests, responses, summaries, stale_tasks = _parse_log(args.log, since_dt)
    turns = _build_turns(requests, responses, summaries)
    gaps = _inter_turn_gaps(turns)

    if args.json:
        footer = {
            "intra_turn_ms": _pctiles([t["total_ms"] for t in turns]),
            "inter_turn_gap_ms": _pctiles([g for gg in gaps.values() for g in gg]),
            "redispatch_histogram": dict(
                sorted(
                    (str(k), v) for k, v in {str(t["redispatch_count"]): 0 for t in turns}.items()
                )
            ),
        }
        # Proper histogram.
        hist: dict[str, int] = defaultdict(int)
        for t in turns:
            hist[str(t["redispatch_count"])] += 1
        footer["redispatch_histogram"] = dict(hist)
        footer["pct_turns_with_redispatch"] = (
            round(100 * sum(1 for t in turns if t["redispatch_count"] > 0) / len(turns), 1)
            if turns
            else 0
        )
        stale_auto = sum(
            1
            for s in stale_tasks
            if s.get("event") == "stale_auto_task_cleared_skipping_redispatch"
        )
        stale_model = sum(
            1 for s in stale_tasks if s.get("event") == "stale_model_task_skipping_redispatch"
        )
        footer["stale_tasks"] = {
            "auto": stale_auto,
            "model": stale_model,
            "total": stale_auto + stale_model,
        }
        print(json.dumps({"turns": turns, "gaps": gaps, "footer": footer}, indent=2, default=str))
        return 0

    if not turns:
        print("No turn_summary events found.")
        return 0

    _print_table(turns)
    footer = _print_footer(turns, gaps, stale_tasks)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""CLAF throttle — Flash / Tap / Local budget enforcement.

Two windows:
- Hourly: caps how many Flash (full cloud handoff) and Tap (cloud snippet
  polish) escalations can fire per hour. Resets every 3600s.
- Daily : caps total tokens reserved across both modes. Resets every 86400s.

Reserve / commit / refund pattern: every escalation reserves budget at intake,
commits if the call returned successfully, refunds if the call raised. The
lock around the counters keeps concurrent requests from racing.

Emergency budget: a small daily pool of Flash slots that do NOT count against
the main hourly cap. Use when the regular budget is exhausted and the operator
explicitly forces cloud. Three per day by default.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal


Need = Literal["tap", "flash"]


@dataclass
class ThrottleState:
    # 2026-05-24: raised for 24/7 free cloud primary routing.
    # qwen3-coder:480b-cloud (Tier 1) is SSH-signed / no per-token billing —
    # high hourly/daily caps don't cost money; they just prevent CLAF from
    # self-throttling the free tier into local-only. Paid tiers are explicit
    # escalation only, so a high cap here doesn't increase spend.
    flash_budget_hourly: int = int(os.environ.get("CLAF_FLASH_BUDGET_HOURLY", "200"))    # was 5 — free cloud tier, no billing
    tap_budget_hourly: int = int(os.environ.get("CLAF_TAP_BUDGET_HOURLY", "200"))      # was 15
    token_budget_daily: int = int(os.environ.get("CLAF_TOKEN_BUDGET_DAILY", "500000")) # was 25_000 — count informational only
    emergency_flash_daily: int = int(os.environ.get("CLAF_EMERGENCY_FLASH_DAILY", "10"))   # was 3

    _flash_used: int = 0
    _tap_used: int = 0
    _tokens_reserved: int = 0
    _emergency_used: int = 0

    _hour_start: float = field(default_factory=time.time)
    _day_start: float = field(default_factory=time.time)

    _reservations: dict[str, dict] = field(default_factory=dict)


THROTTLE = ThrottleState()
_LOCK = threading.Lock()


def _roll_windows_locked(now: float) -> None:
    """Reset counters whose window has expired. Must be called under _LOCK."""
    if now - THROTTLE._hour_start >= 3600:
        THROTTLE._flash_used = 0
        THROTTLE._tap_used = 0
        THROTTLE._hour_start = now
    if now - THROTTLE._day_start >= 86_400:
        THROTTLE._tokens_reserved = 0
        THROTTLE._emergency_used = 0
        THROTTLE._day_start = now


def reserve(tokens: int, need: Need, *, emergency: bool = False) -> str | None:
    """Reserve budget for an escalation. Returns reservation_id if approved.

    Caller MUST call commit(id) on success or refund(id) on failure.
    Returns None if no budget remains in the relevant window.
    """
    now = time.time()
    with _LOCK:
        _roll_windows_locked(now)

        if THROTTLE._tokens_reserved + tokens > THROTTLE.token_budget_daily and not emergency:
            return None

        if emergency:
            if need != "flash":
                return None
            if THROTTLE._emergency_used >= THROTTLE.emergency_flash_daily:
                return None
            THROTTLE._emergency_used += 1
        elif need == "flash":
            if THROTTLE._flash_used >= THROTTLE.flash_budget_hourly:
                return None
            THROTTLE._flash_used += 1
        elif need == "tap":
            if THROTTLE._tap_used >= THROTTLE.tap_budget_hourly:
                return None
            THROTTLE._tap_used += 1
        else:
            return None

        THROTTLE._tokens_reserved += tokens
        rid = f"r_{uuid.uuid4().hex[:12]}"
        THROTTLE._reservations[rid] = {
            "need": need,
            "tokens": tokens,
            "emergency": emergency,
            "ts": now,
        }
        return rid


def commit(reservation_id: str) -> None:
    """Mark a reservation as successfully consumed. Counters stay incremented."""
    with _LOCK:
        THROTTLE._reservations.pop(reservation_id, None)


def refund(reservation_id: str) -> None:
    """Return reserved budget on failed call. Reverses the counter bumps."""
    with _LOCK:
        res = THROTTLE._reservations.pop(reservation_id, None)
        if not res:
            return
        THROTTLE._tokens_reserved = max(0, THROTTLE._tokens_reserved - res["tokens"])
        if res["emergency"]:
            THROTTLE._emergency_used = max(0, THROTTLE._emergency_used - 1)
        elif res["need"] == "flash":
            THROTTLE._flash_used = max(0, THROTTLE._flash_used - 1)
        elif res["need"] == "tap":
            THROTTLE._tap_used = max(0, THROTTLE._tap_used - 1)


def snapshot() -> dict:
    """Read-only view of current budget state. Safe to expose at /stats."""
    now = time.time()
    with _LOCK:
        _roll_windows_locked(now)
        return {
            "flash": {
                "used": THROTTLE._flash_used,
                "cap": THROTTLE.flash_budget_hourly,
                "remaining": max(0, THROTTLE.flash_budget_hourly - THROTTLE._flash_used),
            },
            "tap": {
                "used": THROTTLE._tap_used,
                "cap": THROTTLE.tap_budget_hourly,
                "remaining": max(0, THROTTLE.tap_budget_hourly - THROTTLE._tap_used),
            },
            "tokens_daily": {
                "used": THROTTLE._tokens_reserved,
                "cap": THROTTLE.token_budget_daily,
                "remaining": max(0, THROTTLE.token_budget_daily - THROTTLE._tokens_reserved),
            },
            "emergency_flash": {
                "used": THROTTLE._emergency_used,
                "cap": THROTTLE.emergency_flash_daily,
                "remaining": max(0, THROTTLE.emergency_flash_daily - THROTTLE._emergency_used),
            },
            "hour_window_resets_in_s": max(0, int(3600 - (now - THROTTLE._hour_start))),
            "day_window_resets_in_s": max(0, int(86_400 - (now - THROTTLE._day_start))),
            "open_reservations": len(THROTTLE._reservations),
        }


def degrade_message(need: Need) -> str:
    """Operator-facing string when budget is exhausted, suitable for inclusion
    in a response body when local can't credibly handle the request."""
    snap = snapshot()
    reset_s = snap["hour_window_resets_in_s"] if need in ("flash", "tap") else snap["day_window_resets_in_s"]
    mins = max(1, reset_s // 60)
    cap_key = need
    used = snap[cap_key]["used"]
    cap = snap[cap_key]["cap"]
    emergency_left = snap["emergency_flash"]["remaining"] if need == "flash" else 0
    extra = (
        f" Emergency flash override available ({emergency_left} left today) via metadata.force_cloud + metadata.emergency=true."
        if emergency_left and need == "flash"
        else ""
    )
    return (
        f"[CLAF degrade] {need} budget exhausted ({used}/{cap} in this hour). "
        f"Resets in ~{mins} min.{extra} Falling back to local."
    )

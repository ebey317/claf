"""Tests for claf_throttle.py.

These tests import but do not modify the core routing files.
They only exercise the public API of the throttle module.
"""

import claf_throttle as throttle


def test_reserve_returns_id_for_valid_request():
    rid = throttle.reserve(10, "flash")
    assert rid is not None
    assert rid.startswith("r_")
    throttle.refund(rid)  # clean up


def test_refund_releases_budget():
    before = throttle.snapshot()
    rid = throttle.reserve(100, "tap")
    assert rid is not None
    throttle.refund(rid)
    after = throttle.snapshot()
    assert after["tokens_daily"]["used"] == before["tokens_daily"]["used"]
    assert after["tap"]["used"] == before["tap"]["used"]


def test_snapshot_reports_counters():
    snap = throttle.snapshot()
    assert "flash" in snap
    assert "tap" in snap
    assert "tokens_daily" in snap
    assert "emergency_flash" in snap
    assert snap["flash"]["used"] <= snap["flash"]["cap"]
    assert snap["tap"]["used"] <= snap["tap"]["cap"]


def test_degrade_message_returns_string():
    msg = throttle.degrade_message("flash")
    assert isinstance(msg, str)
    assert msg

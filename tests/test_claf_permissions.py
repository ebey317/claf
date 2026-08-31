"""Tests for claf_permissions mode mapping."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import claf_permissions as cp


def test_default_allows_read():
    assert cp.is_action_allowed("read") == "allow"


def test_default_asks_for_edit():
    os.environ["CLAF_PERMISSION_MODE"] = "default"
    import importlib

    importlib.reload(cp)
    assert cp.is_action_allowed("edit") == "ask"


def test_default_asks_for_bash():
    os.environ["CLAF_PERMISSION_MODE"] = "default"
    import importlib

    importlib.reload(cp)
    assert cp.is_action_allowed("bash", "ls") == "ask"


def test_auto_allows_bash():
    os.environ["CLAF_PERMISSION_MODE"] = "auto"
    # reload module to pick up env change
    import importlib

    importlib.reload(cp)
    assert cp.is_action_allowed("bash", "ls") == "allow"
    assert cp.is_action_allowed("edit") == "allow"


def test_auto_denies_sudo():
    assert cp.is_action_allowed("bash", "sudo apt update") == "deny"


def test_plan_returns_plan():
    os.environ["CLAF_PERMISSION_MODE"] = "plan"
    import importlib

    importlib.reload(cp)
    assert cp.is_action_allowed("bash", "ls") == "plan"


def test_accept_edits_allows_safe_bash():
    os.environ["CLAF_PERMISSION_MODE"] = "acceptEdits"
    import importlib

    importlib.reload(cp)
    assert cp.is_action_allowed("bash", "mkdir foo") == "allow"
    assert cp.is_action_allowed("bash", "sudo apt update") == "deny"


if __name__ == "__main__":
    test_default_allows_read()
    test_default_asks_for_edit()
    test_default_asks_for_bash()
    test_auto_allows_bash()
    test_auto_denies_sudo()
    test_plan_returns_plan()
    test_accept_edits_allows_safe_bash()
    print("ok")

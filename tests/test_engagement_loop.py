"""Tests for engagement_loop checkpoint helpers."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "toolbox"))

import engagement_loop as el


def test_checkpoint_needed_every_five():
    assert el._checkpoint_needed(5, 5) is True
    assert el._checkpoint_needed(10, 5) is True
    assert el._checkpoint_needed(4, 5) is False


def test_checkpoint_needed_zero_disabled():
    assert el._checkpoint_needed(5, 0) is False


def test_build_checkpoint_text():
    qa = [("Step 1: run ls", "file.txt")]
    remaining = ["Step 2: run pwd"]
    text = el._build_checkpoint_text("Test Task", 1, 2, qa, remaining)
    assert "Checkpoint — Test Task" in text
    assert "1/2 steps completed" in text
    assert "Step 1: run ls" in text
    assert "Step 2: run pwd" in text
    assert "PAUSED" in text


def test_mark_task_paused():
    block = "### ⏳ KIMI — Demo\n**Do this exactly:**\n1. run ls\n"
    text = el._mark_task(block, 0, len(block), "paused", "1/2 paused")
    assert "### ⏸️ KIMI — Demo" in text
    assert "PAUSED" in text
    assert "1/2 paused" in text


def test_mark_task_done():
    block = "### 🔄 KIMI — Demo\n**Do this exactly:**\n1. run ls\n"
    text = el._mark_task(block, 0, len(block), "done", "completed")
    assert "### ✅ KIMI — Demo" in text
    assert "DONE" in text


if __name__ == "__main__":
    test_checkpoint_needed_every_five()
    test_checkpoint_needed_zero_disabled()
    test_build_checkpoint_text()
    test_mark_task_paused()
    test_mark_task_done()
    print("ok")

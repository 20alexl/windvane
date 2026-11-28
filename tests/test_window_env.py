"""CLAUDE_CODE_AUTO_COMPACT_WINDOW takes a k or M suffix (Claude Code 2.1.304):
the env var is read the way the setting and the launch flag already are."""

import pytest

from windvane import pressure


@pytest.mark.parametrize("value, point", [("200k", 200_000), ("500K", 500_000), ("1M", 1_000_000), ("750000", 750_000)])
def test_the_window_variable_takes_a_suffix(monkeypatch, value, point):
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", value)
    detail = pressure.compaction_point_detail(1_000_000)
    assert detail["source"] == "env" and detail["point"] == point


def test_a_window_below_the_floor_is_not_the_env_s(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "50k")
    assert pressure.compaction_point_detail(1_000_000)["source"] != "env"

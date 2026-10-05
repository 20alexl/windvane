"""Test-wide isolation from the machine the tests run on."""

import pytest


@pytest.fixture(autouse=True)
def _semantic_tier_off(monkeypatch):
    """The semantic tier is off in every test unless the test turns it on:
    the engine reads the plugin's `semantic` row from this machine's Claude
    Code settings, and a machine with the row on made the keyword-only
    tests fail (2026-10-05). `WINDVANE_SEMANTIC=0` wins over the row, in
    this process and in any daemon a test spawns."""
    monkeypatch.setenv("WINDVANE_SEMANTIC", "0")

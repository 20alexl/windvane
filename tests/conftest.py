"""Test-wide isolation from the machine the tests run on."""

import pytest


@pytest.fixture(autouse=True)
def _machine_rows_pinned(monkeypatch):
    """The plugin's settings rows on this machine stay out of the tests:
    the engine reads them from Claude Code's settings, and a machine with
    the `semantic` row on made the keyword-only tests fail, one with the
    strict pack on (`/windvane-strict`) seeded 27 rules where a test counts
    16 (both 2026-10-05). The environment wins over a row, in this process
    and in any daemon a test spawns; a test that wants a row on sets its
    variable after this, and a file that clears these variables (the rules
    tests) replaces the rows reader instead."""
    monkeypatch.setenv("WINDVANE_SEMANTIC", "0")
    monkeypatch.setenv("WINDVANE_STRICT_PACK", "0")

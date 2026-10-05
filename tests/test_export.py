"""The export: the store's record of a project as Markdown, nothing deleted."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    store = tmp_path / "store"
    monkeypatch.setenv("WINDVANE_DIR", str(store))
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return store


def _seed(tmp_path):
    from windvane.checkpoints import ContextGuard
    from windvane.rules import DESTRUCTIVE_DETECTOR
    from windvane.store import MemoryStore

    ws = tmp_path / "ws"
    proj = ws / "proj"
    proj.mkdir(parents=True)
    s = MemoryStore()
    s.add_rule(str(ws), "Never write a file with platform line endings")
    s.add_rule(str(proj), "Do not run a destructive command without asking", detector=DESTRUCTIVE_DETECTOR)
    s.remember_discovery(str(proj), "MISTAKE: dropped the index in db.py", category="mistake", relevance=9, auto_embed=False)
    s.remember_discovery(str(proj), "DECISION: sqlite for the alias store", category="decision", relevance=7, auto_embed=False)
    s.remember_discovery(str(proj), "DECISION: an old choice nobody reads", category="decision", relevance=4, auto_embed=False)
    p = s.get_project(str(proj))
    for e in p.entries:
        if "old choice" in e.content:
            e.last_accessed = time.time() - 40 * 86400
    s._dirty_projects.add(p.project_path)
    s._save()
    s.archive_old_memories(str(proj), dry_run=False)
    ContextGuard().save_checkpoint(
        "Port the export", "the runs folder", ["rules.md"], ["runs/"], ["export.py"],
        project_path=str(proj), handoff_summary="export half done", handoff_warnings=["nothing is deleted"],
    )
    runs = proj / ".windvane" / "runs"
    runs.mkdir(parents=True)
    (runs / "2026-10-04-abc.md").write_bytes(b"# Run report\n")
    (runs / "2026-10-04-abc.json").write_bytes(b"{}\n")
    return s, ws, proj


def test_export_writes_every_file_and_deletes_nothing(tmp_path):
    from windvane.export import export_project

    s, ws, proj = _seed(tmp_path)
    before = (s.get_archive_stats(str(proj))["hot_total"], s.get_archive_stats(str(proj))["archive_total"])
    result = export_project(str(proj))
    out = proj / ".windvane" / "export"
    assert result["out"] == str(out)
    names = sorted(Path(p).relative_to(out).as_posix() for p in result["written"])
    assert names == ["checkpoints.md", "decisions.md", "mistakes.md", "rules.md",
                     "runs/2026-10-04-abc.json", "runs/2026-10-04-abc.md"]

    rules = (out / "rules.md").read_text(encoding="utf-8")
    assert rules.startswith("# Rules\n\nProject: `") and "Exported: " in rules
    assert "## This project" in rules and "## Inherited" in rules
    assert "_(watched: destructive shell" in rules and "platform line endings" in rules
    assert "`[" in rules and time.strftime("%Y-") in rules  # ids and dates

    mistakes = (out / "mistakes.md").read_text(encoding="utf-8")
    assert "dropped the index" in mistakes and "_(files: db.py)_" in mistakes
    decisions = (out / "decisions.md").read_text(encoding="utf-8")
    assert "## Active" in decisions and "## Archived" in decisions and "an old choice" in decisions
    ck = (out / "checkpoints.md").read_text(encoding="utf-8")
    assert "**Task:** Port the export" in ck and "**Handoff note:** export half done" in ck
    assert "- nothing is deleted" in ck and "(manual)" in ck and "`[task_" in ck
    assert (out / "runs" / "2026-10-04-abc.md").read_bytes() == b"# Run report\n"
    assert all(b"\r\n" not in Path(p).read_bytes() for p in result["written"])

    from windvane.store import MemoryStore

    fresh = MemoryStore()
    after = (fresh.get_archive_stats(str(proj))["hot_total"], fresh.get_archive_stats(str(proj))["archive_total"])
    assert after == before


def test_an_empty_project_exports_empty_files(tmp_path):
    from windvane.export import export_project

    empty = tmp_path / "empty"
    empty.mkdir()
    result = export_project(str(empty), str(tmp_path / "out"))
    assert len(result["written"]) == 4
    assert (tmp_path / "out" / "mistakes.md").read_text(encoding="utf-8").rstrip().endswith("No mistakes.")


def test_the_export_cli_prints_one_json_line(tmp_path, _store):
    proj = tmp_path / "p"
    proj.mkdir()
    env = dict(os.environ, WINDVANE_DIR=str(_store), WINDVANE_NO_DAEMON="1", PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    run = subprocess.run(
        [sys.executable, "-m", "windvane.export", "--project", str(proj), "--out", str(tmp_path / "o")],
        capture_output=True, env=env, timeout=120,
    )
    lines = run.stdout.decode().strip().splitlines()
    assert run.returncode == 0 and len(lines) == 1, run.stderr.decode(errors="replace")
    assert len(json.loads(lines[0])["written"]) == 4

"""Importing an engram store (windvane.migrate): a copy that never touches
the source, leaves the runtime files and the sessions behind, refuses an
existing store unless merging, and merges only what is missing."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from windvane import migrate

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "dst"))


def _engram_store(root: Path, projects: dict) -> Path:
    """An engram-shaped store: projects {path: hash}."""
    src = root / "engram"
    manifest = {
        "version": 3,
        "engram_version": "0.8.61",
        "migrations_applied": ["0.8.58:retire_gone_projects"],
        "projects": {p: {"hash": h, "name": Path(p).name} for p, h in projects.items()},
    }
    (src / "projects").mkdir(parents=True)
    (src / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for h in projects.values():
        d = src / "projects" / h
        (d / "extractions").mkdir(parents=True)
        (d / "memory.json").write_text(json.dumps({"entries": [{"id": h, "content": f"rule of {h}"}]}), encoding="utf-8")
        (d / "handoff_history.json").write_text("[]", encoding="utf-8")
        (d / "extractions" / "s1.json").write_text("{}", encoding="utf-8")
        (d / "memory.json.tmp").write_text("half", encoding="utf-8")
    (src / "checkpoints").mkdir()
    (src / "checkpoints" / "task_1.json").write_text("{}", encoding="utf-8")
    (src / "sessions").mkdir()
    (src / "sessions" / "abc.json").write_text("{}", encoding="utf-8")
    (src / "archive.json").write_text("{}", encoding="utf-8")
    (src / "config.json").write_text(json.dumps({"lessons_globs": [".learnings/*.md"]}), encoding="utf-8")
    for runtime in ("scorer_port", "scorer_pid", "scorer.lock", "mining.lock", "mining_status.json", "session_active"):
        (src / runtime).write_text("1", encoding="utf-8")
    return src


def _digest(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def test_a_full_import_copies_the_store_and_leaves_the_source_alone(tmp_path: Path):
    src = _engram_store(tmp_path, {"e:/w/claude-engram": "h1", "e:/w/other": "h2"})
    before = _digest(src)
    dst = tmp_path / "dst"
    out = migrate.import_engram_store(src, dst)
    assert _digest(src) == before  # never written, moved or deleted
    assert out["dst"] == str(dst) and "error" not in out
    for rel in ("projects/h1/memory.json", "projects/h2/extractions/s1.json", "checkpoints/task_1.json",
                "archive.json", "config.json", "manifest.json"):
        assert (dst / rel).is_file(), rel
    for rel in ("sessions/abc.json", "scorer_port", "scorer.lock", "mining.lock", "mining_status.json",
                "session_active", "projects/h1/memory.json.tmp"):
        assert not (dst / rel).exists(), rel
    copied = len(_digest(dst))
    assert out["copied"] == copied
    assert out["copied"] + out["skipped"] == len(before)
    assert (dst / "projects" / "h1" / "memory.json").read_bytes() == (src / "projects" / "h1" / "memory.json").read_bytes()
    manifest = json.loads((dst / "manifest.json").read_text(encoding="utf-8"))
    # The spelling moves; the values (paths, ids) never do.
    assert "engram_version" not in manifest and manifest["windvane_version"] == "0.8.61"
    assert "e:/w/claude-engram" in manifest["projects"]
    assert manifest["migrations_applied"] == ["0.8.58:retire_gone_projects"]


def test_an_existing_store_is_refused_without_merge(tmp_path: Path):
    src = _engram_store(tmp_path, {"e:/w/a": "h1"})
    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "manifest.json").write_text(json.dumps({"projects": {}}), encoding="utf-8")
    before = _digest(dst)
    out = migrate.import_engram_store(src, dst)
    assert "error" in out and "--merge" in out["error"]
    assert _digest(dst) == before


def test_a_merge_adds_only_the_projects_the_store_lacks(tmp_path: Path):
    src = _engram_store(tmp_path, {"e:/w/a": "h1", "e:/w/b": "h2"})
    dst = tmp_path / "dst"
    (dst / "projects" / "h1").mkdir(parents=True)
    (dst / "projects" / "h1" / "memory.json").write_text('{"entries": ["mine"]}', encoding="utf-8")
    (dst / "manifest.json").write_text(json.dumps({"version": 3, "projects": {"e:/w/a": {"hash": "h1"}}}), encoding="utf-8")
    out = migrate.import_engram_store(src, dst, merge=True)
    assert "error" not in out
    assert (dst / "projects" / "h1" / "memory.json").read_text(encoding="utf-8") == '{"entries": ["mine"]}'
    assert (dst / "projects" / "h2" / "memory.json").is_file()
    assert not (dst / "projects" / "h2" / "memory.json.tmp").exists()
    assert not (dst / "checkpoints").exists() and not (dst / "sessions").exists()
    manifest = json.loads((dst / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["projects"]) == {"e:/w/a", "e:/w/b"} and manifest["projects"]["e:/w/a"] == {"hash": "h1"}
    assert out["copied"] == 3  # memory.json, handoff_history.json, extractions/s1.json of h2
    again = migrate.import_engram_store(src, dst, merge=True)
    assert again["copied"] == 0  # nothing left to add


def test_a_dry_run_counts_and_writes_nothing(tmp_path: Path):
    src = _engram_store(tmp_path, {"e:/w/a": "h1"})
    dst = tmp_path / "dst"
    out = migrate.import_engram_store(src, dst, dry_run=True)
    assert out["dry_run"] is True and out["copied"] > 0
    assert not dst.exists()
    real = migrate.import_engram_store(src, dst)
    assert real["copied"] == out["copied"] and real["skipped"] == out["skipped"]


def test_a_missing_source_or_the_same_folder_is_an_error(tmp_path: Path):
    assert "error" in migrate.import_engram_store(tmp_path / "nowhere", tmp_path / "dst")
    src = _engram_store(tmp_path, {"e:/w/a": "h1"})
    assert "same folder" in migrate.import_engram_store(src, src)["error"]


def test_the_default_destination_is_the_configured_store(tmp_path: Path):
    src = _engram_store(tmp_path, {"e:/w/a": "h1"})
    out = migrate.import_engram_store(src)
    assert Path(out["dst"]) == tmp_path / "dst" and (tmp_path / "dst" / "manifest.json").is_file()


def test_the_cli_prints_one_json_line(tmp_path: Path):
    src = _engram_store(tmp_path, {"e:/w/a": "h1"})
    env = {**os.environ, "PYTHONPATH": str(ROOT), "WINDVANE_DIR": str(tmp_path / "dst"), "WINDVANE_NO_DAEMON": "1"}

    def run(*args):
        r = subprocess.run([sys.executable, "-m", "windvane.migrate", *args], capture_output=True, text=True,
                           env=env, stdin=subprocess.DEVNULL, timeout=60)
        lines = r.stdout.strip().splitlines()
        assert len(lines) == 1, r.stdout + r.stderr
        return r.returncode, json.loads(lines[0])

    rc, out = run("--import", "--from", str(src), "--dry-run")
    assert rc == 0 and out["dry_run"] is True
    rc, out = run("--import", "--from", str(src))
    assert rc == 0 and out["copied"] > 0
    rc, out = run("--import", "--from", str(src))
    assert rc == 1 and "error" in out
    rc, out = run("--import", "--from", str(src), "--merge")
    assert rc == 0 and out["copied"] == 0
    rc, out = run()
    assert rc == 1 and "--import" in out["error"]

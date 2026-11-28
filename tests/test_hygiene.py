"""The daily store hygiene reaches every project: memory cleanup for each one
whose folder exists, and gone projects with nothing in them forgotten."""

import json
import os
import time
from pathlib import Path

import pytest

from windvane import hygiene
from windvane.store import MemoryStore


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")


def _age(store: MemoryStore, path: str, days: float) -> None:
    folder = store._projects_dir / store._manifest["projects"][store._normalize_path(path)]["hash"]
    then = time.time() - days * 86400
    for f in folder.iterdir():
        os.utime(f, (then, then))


def test_every_project_with_a_folder_gets_its_broken_and_doubled_entries_cleaned(tmp_path):
    here, other = tmp_path / "here", tmp_path / "other"
    here.mkdir(), other.mkdir()
    store = MemoryStore()
    assert store.remember_discovery(str(other), "the build reads the map from config/map.json", auto_embed=False)[0]
    proj = store.get_project(str(other))
    twin = type(proj.entries[0])(**{**proj.entries[0].__dict__, "id": "twin0000001"})
    proj.entries.append(twin)
    store._dirty_projects.add(proj.project_path)
    store._save()
    report = hygiene.sweep_projects(MemoryStore(), str(here))
    assert report["cleaned"] >= 1
    assert len(MemoryStore().get_project(str(other)).entries) == 1


def test_a_dormant_project_keeps_its_old_memories(tmp_path):
    here, dormant = tmp_path / "here", tmp_path / "dormant"
    here.mkdir(), dormant.mkdir()
    store = MemoryStore()
    assert store.remember_discovery(str(dormant), "a fact nobody read for half a year", auto_embed=False)[0]
    proj = store.get_project(str(dormant))
    proj.entries[0].last_accessed = time.time() - 180 * 86400
    proj.entries[0].relevance = 4
    store._dirty_projects.add(proj.project_path)
    store._save()
    hygiene.sweep_projects(MemoryStore(), str(here))
    kept = MemoryStore().get_project(str(dormant)).entries
    assert len(kept) == 1 and kept[0].relevance == 4


def test_a_gone_empty_quiet_project_is_forgotten(tmp_path):
    here, gone = tmp_path / "here", tmp_path / "lanes" / "wt-old" / "ui"
    here.mkdir(), gone.mkdir(parents=True)
    store = MemoryStore()
    store.remember_project(str(gone))
    store._dirty_projects.add(store._normalize_path(str(gone)))
    store._save()
    _age(store, str(gone), 40)
    gone.rmdir(), gone.parent.rmdir()  # the worktree was removed
    report = hygiene.sweep_projects(MemoryStore(), str(here))
    assert report["forgot"] == [store._normalize_path(str(gone))]
    assert store._normalize_path(str(gone)) not in MemoryStore()._manifest["projects"]


def test_a_gone_project_with_memories_or_recent_writes_stays(tmp_path):
    here, full, fresh = tmp_path / "here", tmp_path / "full", tmp_path / "fresh"
    for d in (here, full, fresh):
        d.mkdir()
    store = MemoryStore()
    assert store.remember_discovery(str(full), "a memory that must not be lost", auto_embed=False)[0]
    store.remember_project(str(fresh))
    store._dirty_projects.add(store._normalize_path(str(fresh)))
    store._save()
    _age(store, str(full), 40)
    full.rmdir(), fresh.rmdir()
    report = hygiene.sweep_projects(MemoryStore(), str(here))
    assert report["forgot"] == []
    projects = MemoryStore()._manifest["projects"]
    assert store._normalize_path(str(full)) in projects and store._normalize_path(str(fresh)) in projects


def test_a_live_tick_sweeps_once_a_day_in_the_store_it_was_handed(tmp_path, monkeypatch):
    """A standing session rarely ends, so the session-end hygiene seldom ran:
    a live tick runs it when a day has passed, and stamps the miner's store."""
    pytest.importorskip("windvane.proc_lock")
    from windvane.mining import background as bg
    from windvane.mining import jsonl_reader
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    sid, at = "0f1e2d3c-0000-4000-8000-000000000001", "2026-11-28T10:00:00+00:00"
    lines = [
        {"type": "user", "message": {"role": "user", "content": "tidy the store"}, "sessionId": sid, "timestamp": at},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Tidying it."}]},
         "sessionId": sid, "timestamp": at},
    ]
    (transcripts / f"{sid}.jsonl").write_bytes(("\n".join(json.dumps(x) for x in lines) + "\n").encode("utf-8"))
    monkeypatch.setattr(jsonl_reader, "resolve_jsonl_dir", lambda _p: transcripts)
    here = tmp_path / "here"
    here.mkdir()
    store = tmp_path / "store"
    MemoryStore(storage_dir=str(store)).remember_project(str(here))
    gone = tmp_path / "lanes" / "wt-old"
    gone.mkdir(parents=True)
    s = MemoryStore(storage_dir=str(store))
    s.remember_project(str(gone))
    s._dirty_projects.add(s._normalize_path(str(gone)))
    s._save()
    _age(s, str(gone), 40)
    gone.rmdir()
    bg.run_mining(str(here), "live", str(store))
    assert (store / "hygiene_last").exists()
    assert s._normalize_path(str(gone)) not in MemoryStore(storage_dir=str(store))._manifest["projects"]


def test_the_daily_stamp_says_when_hygiene_is_due(tmp_path):
    assert hygiene.due()
    hygiene.stamp()
    assert not hygiene.due()
    old = time.time() - 25 * 3600
    os.utime(hygiene.stamp_file(), (old, old))
    assert hygiene.due()

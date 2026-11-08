"""Several writers on one store: nothing acknowledged is lost, a busy file is
retried, a corrupt file is kept, and a lock never holds a hook up for long."""

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
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    return tmp_path / "store"


WRITER = """
import sys
from windvane.store import MemoryStore
from windvane.events.common import _append_memory_entry
project, worker, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
acked = 0
for i in range(count):
    words = f"w{worker}a{i} w{worker}b{i} w{worker}c{i} w{worker}d{i}"  # shares nothing, so the dedupe stays out of it
    if i % 2:
        added, _ = MemoryStore().remember_discovery(project, words, auto_embed=False)
    else:
        added = _append_memory_entry(project, {"id": f"e{worker}x{i}", "content": words, "category": "discovery"})
    acked += bool(added)
print(acked)
"""


def test_processes_writing_one_project_lose_nothing(tmp_path, _store):
    """Four processes, 160 appends, measured on 1.0.19: 66 acknowledged, 94
    raised on the shared temp file, 2 left on disk."""
    project = str(tmp_path / "p")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    runs = [
        subprocess.Popen([sys.executable, "-c", WRITER, project, str(w), "12"], env=env, cwd=str(ROOT),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for w in range(3)
    ]
    acked = 0
    for run in runs:
        out, err = run.communicate(timeout=60)
        assert run.returncode == 0, err[-800:]
        acked += int(out.strip().splitlines()[-1])
    assert acked == 36
    files = list(_store.glob("projects/*/memory.json"))
    assert len(files) == 1
    contents = [e["content"] for e in json.loads(files[0].read_text(encoding="utf-8"))["entries"]]
    assert len(contents) == len(set(contents)) == 36


def test_a_write_landing_during_a_load_is_not_written_over(tmp_path, _store, monkeypatch):
    """The load read the file and then stamped it, so a write between the two
    looked already seen: 1 in about 80 runs of three processes lost an entry."""
    from windvane.events.common import _append_memory_entry
    from windvane.store import MemoryStore

    project = str(tmp_path / "p")
    assert MemoryStore().remember_discovery(project, "alpha beta gamma delta", auto_embed=False)[0]
    s = MemoryStore()
    real = s._load_project
    landed = []

    def load_then_another_writer(norm):
        proj = real(norm)
        if not landed:
            landed.append(_append_memory_entry(project, {"id": "other", "content": "epsilon zeta eta theta", "category": "discovery"}))
        return proj

    monkeypatch.setattr(s, "_load_project", load_then_another_writer)
    assert s.remember_discovery(project, "iota kappa lambda mu", auto_embed=False)[0]
    assert landed == [True]
    mem = next(_store.glob("projects/*/memory.json"))
    contents = {e["content"] for e in json.loads(mem.read_text(encoding="utf-8"))["entries"]}
    assert contents == {"alpha beta gamma delta", "epsilon zeta eta theta", "iota kappa lambda mu"}


def test_a_temp_file_is_never_shared(tmp_path):
    from windvane import atomic

    seen = []
    real = os.replace

    def spy(src, dst):
        seen.append(str(src))
        real(src, dst)

    target = tmp_path / "f.json"
    os.replace = spy
    try:
        atomic.write_text(target, "1")
        atomic.write_text(target, "2")
    finally:
        os.replace = real
    assert len(set(seen)) == 2 and target.read_text(encoding="utf-8") == "2"
    assert [p.name for p in tmp_path.iterdir()] == ["f.json"]


def test_a_busy_file_is_retried_and_a_lasting_failure_leaves_no_temp(tmp_path, monkeypatch):
    """Windows refuses the replace while any reader has the file open: 5.5% of
    attempts with four readers, measured."""
    from windvane import atomic

    real = os.replace
    fails = {"left": 2}

    def busy(src, dst):
        if fails["left"]:
            fails["left"] -= 1
            raise PermissionError(13, "in use")
        real(src, dst)

    target = tmp_path / "f.json"
    with monkeypatch.context() as m:
        m.setattr(os, "replace", busy)
        atomic.write_text(target, "kept")
    assert target.read_text(encoding="utf-8") == "kept"

    def always(src, dst):
        raise PermissionError(13, "in use")

    with monkeypatch.context() as m:
        m.setattr(os, "replace", always)
        m.setattr(atomic, "_REPLACE_WAITS", (0.0, 0.0))
        with pytest.raises(PermissionError):
            atomic.write_text(target, "lost")
    assert target.read_text(encoding="utf-8") == "kept"
    assert [p.name for p in tmp_path.iterdir()] == ["f.json"]


def test_a_held_lock_delays_a_writer_by_a_bounded_wait(tmp_path):
    from windvane import atomic, proc_lock

    target = tmp_path / "f.json"
    held = proc_lock.acquire(atomic.lock_path(target))
    assert held is not None
    began = time.monotonic()
    try:
        with atomic.locked(target, wait=0.1) as got:
            assert got is False
    finally:
        held.release()
    assert 0.08 <= time.monotonic() - began < 1.0
    with atomic.locked(target) as got:
        assert got is True


def _corpses(path: Path) -> list:
    return sorted(p for p in path.parent.iterdir() if p.name.startswith(path.name + ".corrupt"))


def test_reading_a_corrupt_file_moves_it_aside_and_a_busy_one_says_so(tmp_path, monkeypatch):
    from windvane import atomic

    f = tmp_path / "f.json"
    assert atomic.read_json(f, {"d": 1}) == {"d": 1}
    f.write_text("{ not json", encoding="utf-8")
    assert atomic.read_json(f, {"d": 1}) == {"d": 1}
    assert not f.exists() and _corpses(f)[0].read_text(encoding="utf-8") == "{ not json"

    f.write_text("{}", encoding="utf-8")
    with monkeypatch.context() as m:
        m.setattr(atomic, "_READ_WAITS", (0.0,))
        m.setattr(Path, "read_bytes", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "in use")))
        with pytest.raises(OSError):
            atomic.read_json(f, {})


def test_a_hook_append_keeps_a_corrupt_memory_file(tmp_path, _store):
    """The hook read a bad file as an empty project and wrote that back."""
    from windvane.events.common import _append_memory_entry
    from windvane.store import MemoryStore

    project = str(tmp_path / "p")
    assert MemoryStore().remember_discovery(project, "a fact from before the damage", auto_embed=False)[0]
    mem = next(_store.glob("projects/*/memory.json"))
    mem.write_text('{"entries": [{"content": "truncated', encoding="utf-8")
    assert _append_memory_entry(project, {"id": "e1", "content": "a fact from after", "category": "discovery"})
    assert _corpses(mem) and "truncated" in _corpses(mem)[0].read_text(encoding="utf-8")
    assert [e["content"] for e in json.loads(mem.read_text(encoding="utf-8"))["entries"]] == ["a fact from after"]


def test_a_hook_append_writes_nothing_when_the_file_cannot_be_read(tmp_path, _store, monkeypatch):
    """A file in use is not an empty project."""
    from windvane import atomic
    from windvane.events.common import _append_memory_entry
    from windvane.store import MemoryStore

    project = str(tmp_path / "p")
    assert MemoryStore().remember_discovery(project, "a fact worth keeping", auto_embed=False)[0]
    mem = next(_store.glob("projects/*/memory.json"))
    before = mem.read_bytes()

    def busy(*_a, **_k):
        raise PermissionError(13, "in use")

    monkeypatch.setattr(atomic, "read_json", busy)
    assert _append_memory_entry(project, {"id": "e1", "content": "another fact", "category": "discovery"}) is False
    assert mem.read_bytes() == before


def test_loading_a_corrupt_project_keeps_the_file(tmp_path, _store):
    from windvane.store import MemoryStore

    project = str(tmp_path / "p")
    assert MemoryStore().remember_discovery(project, "a fact from before", auto_embed=False)[0]
    mem = next(_store.glob("projects/*/memory.json"))
    mem.write_text("{ damaged", encoding="utf-8")
    s = MemoryStore()
    assert s.remember_discovery(project, "a fact from after", auto_embed=False)[0]
    assert _corpses(mem) and _corpses(mem)[0].read_text(encoding="utf-8") == "{ damaged"


def test_a_corrupt_ring_file_is_kept_and_does_not_stop_saves(tmp_path):
    from windvane import checkpoints as ck

    ring = tmp_path / "ring"
    ring.mkdir()
    history = ring / ck.HISTORY_FILENAME
    history.write_text("{ damaged", encoding="utf-8")
    report = ck.write_handoff({"kind": "manual", "task_description": "t", "pending_steps": ["a"]}, [ring])
    assert report["appended"] == [str(ring)]
    assert _corpses(history)
    assert len(json.loads(history.read_text(encoding="utf-8"))["handoffs"]) == 1


def test_rings_saved_together_keep_every_record(tmp_path):
    """The global ring is shared by every project: two saves at once read the
    same history and the later write dropped the other's record."""
    from windvane import checkpoints as ck

    ring = tmp_path / "ring"
    ring.mkdir()
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from windvane import checkpoints as ck\n"
        "for i in range(6):\n"
        "    ck.write_handoff({'kind': 'manual', 'task_id': f'{sys.argv[2]}-{i}', 'task_description': 't', 'pending_steps': ['a']}, [Path(sys.argv[1])])\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    runs = [
        subprocess.Popen([sys.executable, "-c", code, str(ring), str(w)], env=env, cwd=str(ROOT), stderr=subprocess.PIPE, text=True)
        for w in range(3)
    ]
    for run in runs:
        _, err = run.communicate(timeout=60)
        assert run.returncode == 0, err[-800:]
    ids = [h["task_id"] for h in json.loads((ring / ck.HISTORY_FILENAME).read_text(encoding="utf-8"))["handoffs"]]
    assert len(ids) == len(set(ids)) == 18


def test_an_archive_save_keeps_what_another_writer_archived(tmp_path):
    from windvane.store import MemoryStore

    a, b = MemoryStore(), MemoryStore()
    project = str(tmp_path / "p")
    assert a.remember_discovery(project, "a fact archived by the first store", auto_embed=False)[0]
    assert b.remember_discovery(project, "a fact archived by the second store", auto_embed=False)[0]
    a_id = next(e.id for e in a.get_project(project).entries if "first" in e.content)
    b._load_archive()  # loaded before the first store wrote
    assert a.archive_memory(project, a_id)[0]
    b_id = next(e.id for e in b.get_project(project).entries if "second" in e.content)
    assert b.archive_memory(project, b_id)[0]
    fresh = MemoryStore()
    fresh._load_archive()
    ids = {e.id for e in fresh._archive_projects[fresh._normalize_path(project)].entries}
    assert {a_id, b_id} <= ids


def test_a_corrupt_archive_is_kept_rather_than_written_over(tmp_path, _store):
    from windvane.store import MemoryStore

    s = MemoryStore()
    project = str(tmp_path / "p")
    assert s.remember_discovery(project, "a fact to archive", auto_embed=False)[0]
    s.archive_file.write_text("{ damaged", encoding="utf-8")
    assert s.archive_memory(project, s.get_project(project).entries[0].id)[0]
    assert _corpses(s.archive_file)[0].read_text(encoding="utf-8") == "{ damaged"

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

    def load_then_another_writer(norm, **kw):
        proj = real(norm, **kw)
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


def test_a_held_lock_refuses_a_writer_after_a_bounded_wait(tmp_path):
    """Past the wait a read-modify-write does not run: going ahead unlocked
    could lose what the holder acknowledged."""
    from windvane import atomic, proc_lock

    target = tmp_path / "f.json"
    held = proc_lock.acquire(atomic.lock_path(target))
    assert held is not None
    ran = []
    began = time.monotonic()
    try:
        with pytest.raises(atomic.LockBusy):
            with atomic.locked(target, wait=0.1):
                ran.append("refused")
        # A record that must land goes ahead, and knows it has no lock.
        with atomic.locked(target, wait=0.1, must_land=True) as got:
            ran.append(got)
    finally:
        held.release()
    assert ran == [False]
    assert 0.16 <= time.monotonic() - began < 1.5
    with atomic.locked(target) as got:
        assert got is True


@pytest.fixture
def _busy_lock():
    """Hold a file's lock as another process would, for writers with no wait."""
    from windvane import atomic, proc_lock

    held = []

    def hold(path):
        lock = proc_lock.acquire(atomic.lock_path(path))
        assert lock is not None
        held.append(lock)
        return lock

    yield hold
    for lock in held:
        lock.release()


def _project_file(store, project):
    return store._project_dir(store._normalize_path(project)) / "memory.json"


def test_an_appender_that_missed_the_lock_writes_nothing_and_says_so(tmp_path, monkeypatch, _store, _busy_lock):
    from windvane import atomic
    from windvane.events.common import _append_memory_entry
    from windvane.store import MemoryStore

    project = str(tmp_path / "p")
    s = MemoryStore()
    assert s.remember_discovery(project, "the holder's own acknowledged fact", auto_embed=False)[0]
    target = _project_file(s, project)
    before = target.read_text(encoding="utf-8")
    monkeypatch.setattr(atomic, "LOCK_WAIT_S", 0.05)
    lock = _busy_lock(target)
    assert _append_memory_entry(project, {"id": "eX", "content": "a late hook's entry", "category": "discovery"}) is False
    assert target.read_text(encoding="utf-8") == before
    lock.release()
    assert _append_memory_entry(project, {"id": "eX", "content": "a late hook's entry", "category": "discovery"}) is True


def test_a_project_that_missed_the_lock_stays_dirty_until_a_save_lands(tmp_path, monkeypatch, _store, _busy_lock):
    """_save cleared every dirty mark whatever each save did, so the entry a
    failed save held was never written by this process again."""
    from windvane import atomic
    from windvane.store import MemoryStore

    project = str(tmp_path / "p")
    s = MemoryStore()
    assert s.remember_discovery(project, "the first fact on the disk", auto_embed=False)[0]
    target = _project_file(s, project)
    monkeypatch.setattr(atomic, "LOCK_WAIT_S", 0.05)
    lock = _busy_lock(target)
    added, message = s.remember_discovery(project, "a second fact, written while the file is held", auto_embed=False)
    assert not added and "not saved" in message.lower()
    assert s._save_error and "lock" in s._save_error.lower()
    assert s._normalize_path(project) in s._dirty_projects
    assert "second" not in target.read_text(encoding="utf-8")
    # A reader in this process keeps the unwritten copy; a reload would drop it.
    other = MemoryStore()
    other.remember_project(project)
    assert any("second" in e.content for e in s.get_project(project).entries)
    lock.release()
    assert s.remember_discovery(project, "a third fact, once the holder is done", auto_embed=False)[0]
    assert s._save_error is None
    assert s._normalize_path(project) not in s._dirty_projects
    on_disk = target.read_text(encoding="utf-8")
    assert "second" in on_disk and "third" in on_disk and "first" in on_disk


def test_a_reload_does_not_drop_a_project_still_waiting_to_be_written(tmp_path, monkeypatch, _store, _busy_lock):
    from windvane import atomic
    from windvane.store import MemoryStore

    project = str(tmp_path / "p")
    s, other = MemoryStore(), MemoryStore()
    assert s.remember_discovery(project, "the fact both stores begin with", auto_embed=False)[0]
    assert other.remember_discovery(project, "a fact the other store wrote later", auto_embed=False)[0]
    target = _project_file(s, project)
    monkeypatch.setattr(atomic, "LOCK_WAIT_S", 0.05)
    lock = _busy_lock(target)
    s.get_project(project).entries[0].relevance = 9  # the disk moved on; this copy has a change of its own
    s._dirty_projects.add(s._normalize_path(project))
    s._save()
    assert s.get_project(project).entries[0].relevance == 9
    lock.release()
    assert s._save()
    on_disk = json.loads(target.read_text(encoding="utf-8"))["entries"]
    assert any("later" in e["content"] for e in on_disk)
    assert any(e["relevance"] == 9 for e in on_disk)


def test_merging_a_file_with_an_entry_lacking_an_id_does_not_wait_on_its_own_lock(tmp_path, _store):
    """The merge read the file under the lock, and repairing an entry saved
    from inside that read, on the same lock: a held lock is refused now, so the
    repair failed, reported an error and swapped the store's copy for the disk's."""
    from windvane.store import MemoryStore

    project = str(tmp_path / "p")
    s = MemoryStore()
    assert s.remember_discovery(project, "the fact this store holds", auto_embed=False)[0]
    ours = s.get_project(project)
    target = _project_file(s, project)
    data = json.loads(target.read_text(encoding="utf-8"))
    data["entries"].append({"id": "", "content": "written by something older, without an id", "category": "discovery"})
    target.write_text(json.dumps(data), encoding="utf-8")
    ours.entries[0].relevance = 9
    s._dirty_projects.add(s._normalize_path(project))
    began = time.monotonic()
    assert s._save()
    assert time.monotonic() - began < 0.5
    assert s._save_error is None
    assert s._projects[s._normalize_path(project)] is ours
    on_disk = json.loads(target.read_text(encoding="utf-8"))["entries"]
    assert [bool(e["id"]) for e in on_disk] == [True, True]
    assert len(ours.entries) == 2


def test_the_memory_tool_says_when_a_write_did_not_land(tmp_path, monkeypatch, _store, _busy_lock):
    from windvane import atomic, tools
    from windvane.store import MemoryStore

    project = str(tmp_path / "p")
    s = MemoryStore()
    assert s.remember_discovery(project, "the first fact on the disk", auto_embed=False)[0]
    monkeypatch.setattr(atomic, "LOCK_WAIT_S", 0.05)
    lock = _busy_lock(_project_file(s, project))
    warm = tools.Warm()
    request = {"tool": "memory", "arguments": {"operation": "remember", "project_path": project, "content": "a fact the tool was asked to keep"}}
    out = tools.run(request, warm)
    assert out["isError"] and "not saved" in out["text"].lower()
    lock.release()
    again = tools.run(request, warm)
    assert not again["isError"]
    assert "asked to keep" in _project_file(s, project).read_text(encoding="utf-8")


def test_an_archive_that_missed_the_lock_moves_nothing(tmp_path, monkeypatch, _store, _busy_lock):
    from windvane import atomic
    from windvane.store import MemoryStore

    project = str(tmp_path / "p")
    s = MemoryStore()
    assert s.remember_discovery(project, "a fact that wants the archive", auto_embed=False)[0]
    mid = s.get_project(project).entries[0].id
    monkeypatch.setattr(atomic, "LOCK_WAIT_S", 0.05)
    lock = _busy_lock(s.archive_file)
    ok, message = s.archive_memory(project, mid)
    assert not ok and "active" in message
    assert [e.id for e in s.get_project(project).entries] == [mid]
    lock.release()
    assert s.archive_memory(project, mid)[0]


def test_a_manifest_that_missed_the_lock_stays_dirty(tmp_path, monkeypatch, _store, _busy_lock):
    from windvane import atomic
    from windvane.store import MemoryStore

    s = MemoryStore()
    assert s.remember_discovery(str(tmp_path / "p"), "a fact that registers the project", auto_embed=False)[0]
    monkeypatch.setattr(atomic, "LOCK_WAIT_S", 0.05)
    lock = _busy_lock(s._manifest_file)
    s._manifest["projects"]["/somewhere/else"] = {"hash": "abcd1234", "name": "else"}
    s._manifest_dirty = True
    s._save_manifest()
    assert s._manifest_dirty and s._save_error
    assert "/somewhere/else" not in s._manifest_file.read_text(encoding="utf-8")
    lock.release()
    s._save_manifest()
    assert not s._manifest_dirty
    assert "/somewhere/else" in s._manifest_file.read_text(encoding="utf-8")


def test_a_checkpoint_lands_even_when_the_ring_lock_is_held(tmp_path, monkeypatch, _store, _busy_lock):
    """A bank is what a compaction keeps. A lost race costs one neighbour in
    the ring; a refused bank costs the resume state."""
    from windvane import atomic, checkpoints as ck

    ring = tmp_path / "ring"
    ring.mkdir()
    monkeypatch.setattr(atomic, "LOCK_WAIT_S", 0.05)
    lock = _busy_lock(ring / ck.HISTORY_FILENAME)
    report = ck.write_handoff({"kind": "manual", "task_id": "t1", "summary": "the state to keep"}, [ring])
    assert report["appended"] == [str(ring)]
    # A refresh is not owed, so it does not go past the lock.
    assert ck.replace_handoff({"task_id": "t1", "summary": "the state, amended"}, [ring])["unread"] == [str(ring)]
    lock.release()
    assert ck.replace_handoff({"task_id": "t1", "summary": "the state, amended"}, [ring])["replaced"] == [str(ring)]
    assert "amended" in (ring / ck.HISTORY_FILENAME).read_text(encoding="utf-8")


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


def test_a_failed_read_leaves_the_rest_of_a_project_alone(tmp_path, monkeypatch, _store):
    """Entries were merged after a failed read, and the summary and the other
    fields went: this copy began empty. And until the next write a warm process
    saw no entries even with the file readable again."""
    from windvane import atomic
    from windvane.store import MemoryStore

    project = str(tmp_path / "p")
    s = MemoryStore()
    assert s.remember_discovery(project, "the fact the file holds", auto_embed=False)[0]
    proj = s.get_project(project)
    proj.summary = "a summary worth keeping"
    s._dirty_projects.add(proj.project_path)
    assert s._save()

    def unreadable(*a, **k):
        raise PermissionError(13, "in use")

    warm = MemoryStore()
    with monkeypatch.context() as m:
        m.setattr(atomic, "read_json", unreadable)
        assert warm.get_project(project).entries == []
    # Readable again: a reader does not need a write to see it.
    assert [e.content for e in warm.get_project(project).entries] == ["the fact the file holds"]

    other = MemoryStore()
    with monkeypatch.context() as m:
        m.setattr(atomic, "read_json", unreadable)
        assert other.get_project(project).entries == []
        assert other.remember_discovery(project, "a fact added meanwhile", auto_embed=False)
        assert other._save_error and "not written" in other._save_error
    assert other._save()
    on_disk = json.loads(_project_file(s, project).read_text(encoding="utf-8"))
    assert on_disk["summary"] == "a summary worth keeping"
    assert len(on_disk["entries"]) == 2
    assert other.get_project(project).summary == "a summary worth keeping"


def test_a_failed_ring_read_does_not_replace_the_ring(tmp_path, monkeypatch, _store):
    """Five records and a read that failed: the save seeded a new ring from the
    latest pointer and wrote it over the five."""
    from windvane import atomic, checkpoints as ck

    ring, other = tmp_path / "ring", tmp_path / "other"
    ring.mkdir()
    other.mkdir()
    for n in range(5):
        ck.write_handoff({"kind": "manual", "task_id": f"t{n}", "summary": f"record {n}"}, [ring])
    real = atomic.read_json

    def busy_ring(path, default=None):
        if Path(path).parent == ring:
            raise PermissionError(13, "in use")
        return real(path, default)

    with monkeypatch.context() as m:
        m.setattr(atomic, "read_json", busy_ring)
        report = ck.write_handoff({"kind": "manual", "task_id": "new", "summary": "the new record"}, [ring, other])
    assert report["unread"] == [str(ring)] and report["appended"] == [str(other)]
    kept = json.loads((ring / ck.HISTORY_FILENAME).read_text(encoding="utf-8"))["handoffs"]
    assert [h["task_id"] for h in kept] == [f"t{n}" for n in range(5)]
    assert json.loads((ring / ck.LATEST_FILENAME).read_text(encoding="utf-8"))["task_id"] == "t4"


def test_a_failed_state_read_does_not_write_the_defaults_over_it(tmp_path, monkeypatch, _store):
    from windvane import atomic
    from windvane.events import common

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-unread")
    state = common.load_state()
    state["prompts_this_session"] = 7
    common.save_state(state)
    target = common.get_state_file()
    assert json.loads(target.read_text(encoding="utf-8"))["prompts_this_session"] == 7

    with monkeypatch.context() as m:
        m.setattr(atomic, "read_json", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "in use")))
        blank = common.load_state()
        assert blank["prompts_this_session"] == 0
    common.save_state(blank)
    assert json.loads(target.read_text(encoding="utf-8"))["prompts_this_session"] == 7
    again = common.load_state()
    assert again["prompts_this_session"] == 7
    again["prompts_this_session"] = 8
    common.save_state(again)
    assert json.loads(target.read_text(encoding="utf-8"))["prompts_this_session"] == 8

    target.write_text("{ damaged", encoding="utf-8")
    assert common.load_state()["prompts_this_session"] == 0
    assert _corpses(target)


def test_an_archive_of_the_wrong_shape_is_moved_aside(tmp_path, _store):
    """Valid JSON with a bad row raised on every save, and every move to the
    archive failed until someone edited the file."""
    from windvane.store import MemoryStore

    s = MemoryStore()
    project = str(tmp_path / "p")
    assert s.remember_discovery(project, "a fact that wants the archive", auto_embed=False)[0]
    s.archive_file.write_text('{"projects": {"/a": 7}}', encoding="utf-8")
    assert s.archive_memory(project, s.get_project(project).entries[0].id)[0]
    assert _corpses(s.archive_file)[0].read_text(encoding="utf-8") == '{"projects": {"/a": 7}}'


def test_a_damaged_memory_file_is_said_at_session_start(tmp_path, monkeypatch, _store):
    from windvane.events import session_start
    from windvane.store import MemoryStore

    project = str(tmp_path / "p")
    s = MemoryStore()
    assert s.remember_discovery(project, "a fact the damage takes with it", auto_embed=False)[0]
    assert session_start._damaged_memory_lines(project) == []
    target = _project_file(s, project)
    target.write_text("{ damaged", encoding="utf-8")
    fresh = MemoryStore()
    assert fresh.get_project(project).entries == []
    assert fresh._load_error and "moved aside" in fresh._load_error
    lines = session_start._damaged_memory_lines(project)
    assert len(lines) == 1 and "moved aside" in lines[0] and ".corrupt-" in lines[0]


def test_forgetting_a_project_stays_forgotten_past_a_stale_read(tmp_path, _store):
    """The person asked for a deletion, which may be of secret text, and the
    merges of the manifest and the archive put it back with success reported."""
    from windvane.store import MemoryStore

    a, warm = MemoryStore(), MemoryStore()
    project, keeper = str(tmp_path / "p"), str(tmp_path / "q")
    assert a.remember_discovery(project, "a fact to forget", auto_embed=False)[0]
    assert a.archive_memory(project, a.get_project(project).entries[0].id)[0]
    warm.get_project(project)
    warm._load_archive()
    # Both files move on behind the warm store.
    assert a.remember_discovery(keeper, "a fact from a project that stays", auto_embed=False)[0]
    assert a.archive_memory(keeper, a.get_project(keeper).entries[0].id)[0]
    warm.forget_project(project)
    fresh = MemoryStore()
    fresh._load_archive()
    norm = fresh._normalize_path(project)
    assert norm not in fresh._manifest["projects"] and norm not in fresh._archive_projects
    assert fresh._normalize_path(keeper) in fresh._manifest["projects"]
    assert fresh._normalize_path(keeper) in fresh._archive_projects
    assert "a fact to forget" not in fresh.archive_file.read_text(encoding="utf-8")


def test_a_restore_is_not_undone_by_the_other_writers_archive(tmp_path, _store):
    """The merge took a restored entry back from the disk, since the copy this
    process had just removed read as one it had never seen. A second restore
    then gave two active copies."""
    from windvane.store import MemoryStore

    a, b = MemoryStore(), MemoryStore()
    project = str(tmp_path / "p")
    assert a.remember_discovery(project, "a fact that goes and comes back", auto_embed=False)[0]
    assert a.remember_discovery(project, "another fact the first store archives later", auto_embed=False)[0]
    first, second = [e.id for e in a.get_project(project).entries]
    assert a.archive_memory(project, first)[0]
    assert b.get_project(project)
    b._load_archive()
    assert a.archive_memory(project, second)[0]  # the file moves on behind the second store
    assert b.restore_from_archive(project, first)[0]
    fresh = MemoryStore()
    fresh._load_archive()
    norm = fresh._normalize_path(project)
    assert {e.id for e in fresh._archive_projects[norm].entries} == {second}
    assert [e.id for e in fresh.get_project(project).entries].count(first) == 1
    assert not b._archive_removed


def test_a_project_forgotten_here_does_not_come_back_from_the_other_writers_archive(tmp_path, _store):
    from windvane.store import MemoryStore

    a, b = MemoryStore(), MemoryStore()
    project, keeper = str(tmp_path / "p"), str(tmp_path / "q")
    assert a.remember_discovery(project, "a fact archived and then forgotten", auto_embed=False)[0]
    assert a.remember_discovery(keeper, "a fact from a project that stays", auto_embed=False)[0]
    assert a.archive_memory(project, a.get_project(project).entries[0].id)[0]
    b._load_archive()
    assert a.archive_memory(keeper, a.get_project(keeper).entries[0].id)[0]
    b.forget_project(project)
    fresh = MemoryStore()
    fresh._load_archive()
    assert fresh._normalize_path(project) not in fresh._archive_projects
    assert fresh._normalize_path(keeper) in fresh._archive_projects


def test_restoring_an_entry_already_active_leaves_one_copy(tmp_path, _store):
    """A crash between the two writes of a move leaves the entry in both
    tiers; a restore puts it right."""
    import copy

    from windvane.store import MemoryStore

    s = MemoryStore()
    project = str(tmp_path / "p")
    assert s.remember_discovery(project, "a fact in both tiers after a crash", auto_embed=False)[0]
    entry = s.get_project(project).entries[0]
    assert s._copy_into_archive(s.get_project(project), [copy.deepcopy(entry)])
    assert s.restore_from_archive(project, entry.id)[0]
    assert [e.id for e in s.get_project(project).entries] == [entry.id]
    assert not s._archive_projects[s._normalize_path(project)].entries


def test_a_restore_that_could_not_be_written_stays_archived(tmp_path, monkeypatch, _store, _busy_lock):
    from windvane import atomic
    from windvane.store import MemoryStore

    s = MemoryStore()
    project = str(tmp_path / "p")
    assert s.remember_discovery(project, "a fact on its way back", auto_embed=False)[0]
    mid = s.get_project(project).entries[0].id
    assert s.archive_memory(project, mid)[0]
    monkeypatch.setattr(atomic, "LOCK_WAIT_S", 0.05)
    lock = _busy_lock(_project_file(s, project))
    ok, message = s.restore_from_archive(project, mid)
    assert not ok and "archived" in message
    assert s.get_project(project).entries == []
    assert s.search_archive(project)
    lock.release()
    assert s.restore_from_archive(project, mid)[0]
    assert [e.id for e in MemoryStore().get_project(project).entries] == [mid]


def test_a_corrupt_file_that_cannot_be_moved_is_not_taken_for_an_empty_one(tmp_path, monkeypatch, _store):
    """read_json answered the default, and the caller wrote over the damage."""
    from windvane import atomic
    from windvane.events.common import _append_memory_entry
    from windvane.store import MemoryStore

    f = tmp_path / "f.json"
    f.write_text("{ damaged", encoding="utf-8")
    monkeypatch.setattr(atomic, "_REPLACE_WAITS", (0.0,))
    with monkeypatch.context() as m:
        m.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "in use")))
        assert atomic.quarantine(f) is False
        with pytest.raises(OSError):
            atomic.read_json(f, {})
    assert f.read_text(encoding="utf-8") == "{ damaged"

    project = str(tmp_path / "p")
    s = MemoryStore()
    assert s.remember_discovery(project, "the fact the damage replaces", auto_embed=False)[0]
    target = _project_file(s, project)
    target.write_text("{ damaged", encoding="utf-8")
    real = os.replace

    def only_writes(src, dst, *a, **k):
        if ".corrupt-" in str(dst):
            raise PermissionError(13, "in use")
        return real(src, dst, *a, **k)

    with monkeypatch.context() as m:
        m.setattr(os, "replace", only_writes)
        assert _append_memory_entry(project, {"id": "eX", "content": "a late entry", "category": "discovery"}) is False
        assert target.read_text(encoding="utf-8") == "{ damaged"
        # A file of the wrong shape, which only the store's own parse can tell.
        target.write_text('{"entries": 7}', encoding="utf-8")
        fresh = MemoryStore()
        assert not fresh.remember_discovery(project, "a fact for a file that cannot be moved", auto_embed=False)[0] or fresh._save_error
        assert target.read_text(encoding="utf-8") == '{"entries": 7}'
    # Moved aside as soon as it can be.
    assert atomic.quarantine(f) is True and _corpses(f)
    assert atomic.quarantine(f) is True  # somebody else moved it: nothing left to do

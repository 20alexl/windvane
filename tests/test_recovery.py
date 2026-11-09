"""What a reply promises: a save that did not reach its restore index says so, and
two writers on one store keep each other's changes, including when one lands
between the other's read and write."""

import json
import threading
from pathlib import Path

import pytest

from windvane import atomic, checkpoints as ck
from windvane.store import MemoryStore


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")


def _rings_unreadable(monkeypatch, only=None):
    real = atomic.read_json

    def read(path, default=None):
        name_ok = Path(path).name in (ck.HISTORY_FILENAME, ck.LATEST_FILENAME)
        if name_ok and (only is None or Path(path).parent == only):
            raise PermissionError("unavailable")
        return real(path, default)

    monkeypatch.setattr(atomic, "read_json", read)


# ── a save or a bank that did not land says so ──────────────────────────────


def test_a_save_no_ring_accepted_is_a_failure_with_its_id(tmp_path, monkeypatch):
    guard = ck.ContextGuard()
    project = str(tmp_path / "project")
    assert guard.save_checkpoint("Old task", "s", [], ["n"], [], project_path=project).status == "success"
    with monkeypatch.context() as m:
        _rings_unreadable(m)
        out = guard.save_checkpoint("New task", "s", [], ["n"], [], project_path=project)
    assert out.status == "failed" and "do not compact" in out.reasoning and "Filed under" not in out.reasoning
    assert "Old task" in guard.restore_checkpoint(project_path=project).to_formatted_string()
    assert "New task" in guard.restore_checkpoint(task_id=out.data["task_id"]).to_formatted_string()


def test_a_save_its_own_project_ring_missed_is_a_failure(tmp_path, monkeypatch):
    """A restore for the project reads that ring first and would resume the older record."""
    guard = ck.ContextGuard()
    project = tmp_path / "project"
    project.mkdir()
    ring = ck.register_project_ring(str(project))
    with monkeypatch.context() as m:
        _rings_unreadable(m, only=ring)
        out = guard.save_checkpoint("New task", "s", [], ["n"], [], project_path=str(project))
    assert out.status == "failed"


def test_the_tool_reports_a_failed_save_as_an_error(tmp_path, monkeypatch):
    from windvane import tools

    project = tmp_path / "project"
    project.mkdir()
    _rings_unreadable(monkeypatch)
    out = tools.run({"tool": "checkpoint", "arguments": {"operation": "save", "task_description": "New", "project_path": str(project)}})
    assert out["isError"] is True and "task_id: task_" in out["text"]


def _drafted(monkeypatch, project: Path):
    from windvane import draft

    monkeypatch.setattr(draft, "session_inputs", lambda *a: ("session", {}, ""))
    monkeypatch.setattr(draft, "draft", lambda *a, **k: {"task_description": "Critical task", "pending_steps": ["verify"],
                                                         "metadata": {"project_path": str(project)}})


def test_compact_now_with_no_ring_is_an_error_and_keeps_the_record(tmp_path, monkeypatch):
    """The mod asks for the compaction only on a reply that is not an error."""
    import re

    from windvane import tools

    project = tmp_path / "project"
    project.mkdir()
    _drafted(monkeypatch, project)
    with monkeypatch.context() as m:
        _rings_unreadable(m)
        out = tools.run({"tool": "compact_now", "arguments": {"project_path": str(project)}})
    assert out["isError"] is True and "compaction was not requested" in out["text"] and "Banked" not in out["text"]
    ident = re.search(r"task_id: (task_\d+)", out["text"])[1]
    assert "Critical task" in ck.ContextGuard().restore_checkpoint(task_id=ident).to_formatted_string()


def test_compact_now_with_its_ring_answers_as_before(tmp_path, monkeypatch):
    from windvane import tools

    project = tmp_path / "project"
    project.mkdir()
    _drafted(monkeypatch, project)
    out = tools.run({"tool": "compact_now", "arguments": {"project_path": str(project)}})
    assert out["isError"] is False and "Banked" in out["text"]


def _held(ring: Path):
    from windvane import proc_lock

    ring.mkdir(parents=True, exist_ok=True)
    return proc_lock.acquire(atomic.lock_path(ring / ck.HISTORY_FILENAME))


def test_a_deliberate_save_does_not_write_past_a_held_lock(tmp_path, monkeypatch):
    """A writer holding the lock read the ring first: what lands beside it, it
    then writes over, and the save that was acknowledged is gone."""
    guard = ck.ContextGuard()
    project = tmp_path / "project"
    project.mkdir()
    ring = ck.register_project_ring(str(project))
    monkeypatch.setattr(ck, "DELIBERATE_LOCK_WAIT_S", 0.05)
    lock = _held(ring)
    try:
        out = guard.save_checkpoint("New task", "s", [], ["n"], [], project_path=str(project))
    finally:
        lock.release()
    assert out.status == "failed"
    assert not (ring / ck.HISTORY_FILENAME).exists()
    assert "New task" in guard.restore_checkpoint(task_id=out.data["task_id"]).to_formatted_string()


def test_a_deliberate_save_waits_longer_than_a_hook_can(tmp_path, monkeypatch):
    """A tool call has a minute, a hook has seconds: a holder that takes more
    than the hooks' wait does not fail a save."""
    guard = ck.ContextGuard()
    project = tmp_path / "project"
    project.mkdir()
    ring = ck.register_project_ring(str(project))
    monkeypatch.setattr(atomic, "LOCK_WAIT_S", 0.01)
    monkeypatch.setattr(ck, "DELIBERATE_LOCK_WAIT_S", 3.0)
    lock = _held(ring)
    timer = threading.Timer(0.2, lock.release)
    timer.start()
    try:
        assert guard.save_checkpoint("New task", "s", [], ["n"], [], project_path=str(project)).status == "success"
    finally:
        timer.join()


def test_an_automatic_bank_still_lands_past_a_held_lock(tmp_path, monkeypatch):
    """A compaction that is coming anyway cannot be refused: this record is what
    it keeps, and the worst the race costs is a neighbour's."""
    ring = tmp_path / "ring"
    monkeypatch.setattr(atomic, "LOCK_WAIT_S", 0.02)
    lock = _held(ring)
    try:
        report = ck.write_handoff({"kind": "manual", "task_id": "t1", "summary": "banked"}, [ring])
    finally:
        lock.release()
    assert report["appended"] == [str(ring)]


# ── a refresh and the pointer ───────────────────────────────────────────────


def test_a_refresh_does_not_roll_the_pointer_back_over_a_newer_save(tmp_path, monkeypatch):
    ring = tmp_path / "ring"
    first = {"kind": "manual", "task_id": "t1", "summary": "first"}
    ck.write_handoff(dict(first), [ring])
    read = ck._load_history
    entered, go = threading.Event(), threading.Event()

    def paused(path, for_write=False):
        got = read(path, for_write=for_write)
        if threading.current_thread().name == "refresh":
            entered.set()
            go.wait(5)
        return got

    monkeypatch.setattr(ck, "_load_history", paused)
    t = threading.Thread(target=lambda: ck.replace_handoff({**first, "summary": "amended"}, [ring]), name="refresh")
    t.start()
    entered.wait(5)
    # The refresh holds the ring: the newer save waits for it, then lands over it.
    saver = threading.Thread(target=lambda: ck.write_handoff({"kind": "manual", "task_id": "t2", "summary": "second"}, [ring], must_land=False, wait=5))
    saver.start()
    go.set()
    t.join(5)
    saver.join(5)
    assert json.loads((ring / ck.LATEST_FILENAME).read_text(encoding="utf-8"))["task_id"] == "t2"


def test_a_refresh_past_a_held_lock_changes_nothing_and_says_so(tmp_path, monkeypatch):
    ring = tmp_path / "ring"
    entry = {"kind": "manual", "task_id": "t1", "summary": "original"}
    ck.write_handoff(dict(entry), [ring])
    before = {p.name: p.read_bytes() for p in ring.glob("*.json")}
    monkeypatch.setattr(atomic, "LOCK_WAIT_S", 0.02)
    lock = _held(ring)
    try:
        report = ck.replace_handoff({**entry, "summary": "amended"}, [ring])
    finally:
        lock.release()
    assert report["unread"] == [str(ring)] and report["replaced"] == []
    assert before == {p.name: p.read_bytes() for p in ring.glob("*.json")}


# ── memory: a change landing between a writer's read and its write ──────────


def _two(project: str):
    store = MemoryStore()
    assert store.remember_discovery(project, "alpha bravo charlie delta", auto_embed=False)[0]
    assert store.remember_discovery(project, "echo foxtrot golf hotel", auto_embed=False)[0]
    return store, [e.id for e in store.get_project(project).entries]


def _landing_first(monkeypatch, store: MemoryStore, change):
    """``change`` lands after ``store`` read the project and before it saves."""
    save = store._save

    def interleaved():
        change()
        return save()

    monkeypatch.setattr(store, "_save", interleaved)


def _entries(project: str) -> dict:
    return {e.id: e for e in MemoryStore().get_project(project).entries}


def test_edits_to_two_entries_both_stay(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, (a, b) = _two(project)
    second = MemoryStore()
    _landing_first(monkeypatch, second, lambda: first.modify_memory(project, a, relevance=9))
    assert second.modify_memory(project, b, relevance=8)[0]
    got = _entries(project)
    assert got[a].relevance == 9 and got[b].relevance == 8


def test_an_edit_does_not_bring_back_what_another_writer_deleted(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, (a, b) = _two(project)
    second = MemoryStore()
    _landing_first(monkeypatch, second, lambda: first.delete_memory(project, a))
    assert second.modify_memory(project, b, relevance=8)[0]
    got = _entries(project)
    assert a not in got and got[b].relevance == 8


def test_a_delete_stays_deleted_beside_another_writers_edit(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, (a, b) = _two(project)
    second = MemoryStore()
    _landing_first(monkeypatch, second, lambda: first.modify_memory(project, b, relevance=9))
    assert second.delete_memory(project, a)[0]
    got = _entries(project)
    assert a not in got and got[b].relevance == 9


def test_two_edits_to_one_entry_refuse_the_second_and_the_next_call_reloads(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, (a, _b) = _two(project)
    second = MemoryStore()
    with monkeypatch.context() as m:
        _landing_first(m, second, lambda: first.modify_memory(project, a, relevance=9))
        ok, message = second.modify_memory(project, a, relevance=8)
    assert not ok and "another writer" in message
    assert _entries(project)[a].relevance == 9
    assert second.get_project(project).entries[0].relevance == 9
    assert second.modify_memory(project, a, relevance=7)[0]
    assert _entries(project)[a].relevance == 7


def test_an_edit_to_what_another_writer_deleted_is_refused(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, (a, _b) = _two(project)
    second = MemoryStore()
    with monkeypatch.context() as m:
        _landing_first(m, second, lambda: first.delete_memory(project, a))
        ok, message = second.modify_memory(project, a, relevance=8)
    assert not ok and "another writer" in message
    assert a not in _entries(project)


def test_a_refused_project_does_not_fail_the_next_save_of_another(tmp_path, monkeypatch):
    project, other = str(tmp_path / "p"), str(tmp_path / "q")
    first, (a, _b) = _two(project)
    second = MemoryStore()
    with monkeypatch.context() as m:
        _landing_first(m, second, lambda: first.modify_memory(project, a, relevance=9))
        assert not second.modify_memory(project, a, relevance=8)[0]
    assert second.remember_discovery(other, "something about another project", auto_embed=False)[0]
    assert second._save_error is None


def test_a_recall_count_is_not_a_conflict(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, (a, _b) = _two(project)
    second = MemoryStore()

    def counted():
        entry = [e for e in first.get_project(project).entries if e.id == a][0]
        entry.access_count = 5
        first._dirty_projects.add(first._normalize_path(project))
        first._save()

    _landing_first(monkeypatch, second, counted)
    assert second.modify_memory(project, a, relevance=8)[0]
    got = _entries(project)[a]
    assert got.relevance == 8 and got.access_count == 5


def test_a_new_entry_beside_another_writers_new_entry(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, _ = _two(project)
    second = MemoryStore()
    _landing_first(monkeypatch, second, lambda: first.remember_discovery(project, "india juliet kilo lima", auto_embed=False))
    assert second.remember_discovery(project, "mike november oscar papa", auto_embed=False)[0]
    assert {"india juliet kilo lima", "mike november oscar papa"} <= {e.content for e in _entries(project).values()}


def test_a_summary_set_here_stays_and_one_set_there_comes_in(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, (a, _b) = _two(project)
    second = MemoryStore()

    def named():
        first.remember_project(project, summary="set by the other writer")

    _landing_first(monkeypatch, second, named)
    assert second.modify_memory(project, a, relevance=8)[0]
    assert MemoryStore().get_project(project).summary == "set by the other writer"


def test_a_changed_entrys_file_index_is_rebuilt_from_the_merge(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, (a, b) = _two(project)
    second = MemoryStore()
    _landing_first(monkeypatch, second, lambda: first.delete_memory(project, a))
    assert second.modify_memory(project, b, relevance=8)[0]
    index = MemoryStore().get_project(project).tag_memory_index
    assert a not in {i for ids in index.values() for i in ids}


def test_real_processes_racing_on_one_file_keep_both_edits(tmp_path):
    """No mock of the store: two processes, each past its read and held at a gate
    until the other is too, then both save at once."""
    import os
    import subprocess
    import sys

    project = str(tmp_path / "p")
    _, ids = _two(project)
    gate = tmp_path / "gate"
    gate.mkdir()
    program = """
import json, sys, time
from pathlib import Path
from windvane.store import MemoryStore
project, ident, name, gate, relevance = sys.argv[1:]
store = MemoryStore()
store.get_project(project)
save = store._save
def at_the_gate():
    Path(gate, name).write_bytes(b"here")
    deadline = time.monotonic() + 20
    while len(list(Path(gate).iterdir())) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    return save()
store._save = at_the_gate
print(json.dumps(store.modify_memory(project, ident, relevance=int(relevance))))
"""
    root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(root)}
    runs = [subprocess.Popen([sys.executable, "-c", program, project, ident, str(i), str(gate), str(7 + i)], cwd=root, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i, ident in enumerate(ids)]
    for run in runs:
        out, err = run.communicate(timeout=60)
        assert run.returncode == 0, err
        assert json.loads(out)[0], out
    got = _entries(project)
    assert got[ids[0]].relevance == 7 and got[ids[1]].relevance == 8


# ── global priorities and a forgotten project ───────────────────────────────


def test_two_writers_keep_each_others_global_priorities():
    one, other = MemoryStore(), MemoryStore()
    one.add_priority("first priority")
    other.add_priority("second priority")
    assert {e.content for e in MemoryStore()._global_entries} == {"first priority", "second priority"}


def test_a_save_with_nothing_global_to_say_leaves_the_file_alone(tmp_path):
    stale = MemoryStore()
    MemoryStore().add_priority("written after the other loaded")
    assert stale.remember_discovery(str(tmp_path / "p"), "a note about something else", auto_embed=False)[0]
    assert "written after the other loaded" in {e.content for e in MemoryStore()._global_entries}


def test_a_memory_remembered_after_another_writer_forgot_the_project_is_found(tmp_path):
    project = str(tmp_path / "p")
    first, _ = _two(project)
    stale = MemoryStore()
    stale.get_project(project)
    first.forget_project(project)
    assert stale.remember_discovery(project, "A new observation after forgetting", auto_embed=False)[0]
    got = MemoryStore().get_project(project)
    assert got is not None and [e.content for e in got.entries] == ["A new observation after forgetting"]

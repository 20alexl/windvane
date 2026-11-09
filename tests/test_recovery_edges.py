"""The edges of keeping two writers' changes: a save never answers success for
something it dropped, a damaged file or a failed merge never costs the copy held
here, and goals and banks say what really happened."""

import json
import os
import time
from pathlib import Path

import pytest

from windvane import atomic, checkpoints as ck, draft, goal, tools
from windvane.store import MemoryEntry, MemoryStore, ProjectMemory, _entry_print


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")


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


def _memory_file(store: MemoryStore, project: str) -> Path:
    return store._project_dir(store._normalize_path(project)) / "memory.json"


def _no_writes(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(atomic, "write_json", refuse)


def _call(warm, **arguments):
    out = tools.run({"tool": "memory", "arguments": arguments}, warm)
    return out["isError"], out["text"]


def _pending_edit_lost(monkeypatch, project: str, warm, memory_id: str):
    """A warm store, as the daemon holds one: an edit that could not be written
    waits in its copy, and another writer changes the same memory."""
    warm.store.get_project(project)
    with monkeypatch.context() as m:
        _no_writes(m)
        assert _call(warm, operation="modify", project_path=project, memory_id=memory_id, content="my pending edit")[0]
    assert MemoryStore().modify_memory(project, memory_id, content="their edit")[0]


# ── a change made after a refused one is not dropped with it ────────────────


def test_a_memory_remembered_after_a_refused_edit_is_on_disk(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    _store, (a, _b) = _two(project)
    warm = tools.Warm()
    _pending_edit_lost(monkeypatch, project, warm, a)
    failed, _text = _call(warm, operation="remember", project_path=project, content="a brand new thing worth keeping")
    assert not failed
    got = [e.content for e in _entries(project).values()]
    assert "a brand new thing worth keeping" in got and "their edit" in got and "my pending edit" not in got


def test_a_rule_added_after_a_refused_edit_is_on_disk(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    _store, (a, _b) = _two(project)
    warm = tools.Warm()
    _pending_edit_lost(monkeypatch, project, warm, a)
    failed, _text = _call(warm, operation="add_rule", project_path=project, content="never ship without tests", reason="r")
    assert not failed
    assert [e for e in _entries(project).values() if e.category == "rule"]


def test_a_restore_after_a_refused_edit_keeps_the_memory(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    _store, (a, b) = _two(project)
    warm = tools.Warm()
    assert not _call(warm, operation="archive", project_path=project, memory_id=b)[0]
    _pending_edit_lost(monkeypatch, project, warm, a)
    failed, _text = _call(warm, operation="restore", project_path=project, memory_id=b)
    assert not failed
    assert b in _entries(project)


# ── a damaged file or a failed merge does not cost the copy held here ───────


def test_a_damaged_file_on_a_stale_save_keeps_this_copy(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    store, (a, b) = _two(project)
    with monkeypatch.context() as m:
        _no_writes(m)
        assert not store.modify_memory(project, a, relevance=9)[0]
    _memory_file(store, project).write_bytes(b"{not json")
    assert store._save()
    got = _entries(project)
    assert set(got) == {a, b} and got[a].relevance == 9
    assert list(_memory_file(store, project).parent.glob("memory.json.corrupt-*"))


def test_a_damaged_global_file_keeps_the_priorities_held_here():
    store = MemoryStore()
    store.add_priority("first global priority")
    store.add_priority("second global priority")
    store._global_file.write_bytes(b"{not json")
    store.add_priority("third global priority")
    assert len(MemoryStore()._global_entries) == 3


def test_a_merge_that_raises_leaves_the_file_alone_and_says_so(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, (a, _b) = _two(project)
    second = MemoryStore()

    def broken(*_args):
        raise TypeError("a row that cannot be merged")

    monkeypatch.setattr(second, "_merge_onto_disk", broken)
    _landing_first(monkeypatch, second, lambda: first.remember_discovery(project, "india juliet kilo lima", auto_embed=False))
    ok, message = second.modify_memory(project, a, relevance=9)
    assert not ok and "Not saved" in message
    got = _entries(project)
    assert len(got) == 3 and got[a].relevance != 9


def test_a_row_with_bad_numbers_loads_with_good_ones():
    entry = MemoryEntry.from_dict({"id": "a", "content": "x", "access_count": None})
    assert entry.access_count == 1
    proj = ProjectMemory.from_dict({"project_path": "p", "last_updated": None, "last_cleanup": "soon"})
    assert isinstance(proj.last_updated, float) and isinstance(proj.last_cleanup, float)


# ── deleted here and edited there is refused like the reverse ───────────────


def test_a_delete_of_what_another_writer_edited_is_refused(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, (a, _b) = _two(project)
    second = MemoryStore()
    with monkeypatch.context() as m:
        _landing_first(m, second, lambda: first.modify_memory(project, a, content="their edit"))
        ok, message = second.delete_memory(project, a)
    assert not ok and "another writer" in message
    assert _entries(project)[a].content == "their edit"
    assert second.delete_memory(project, a)[0]
    assert a not in _entries(project)


def test_archiving_what_another_writer_edited_is_refused_and_taken_back(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, (a, _b) = _two(project)
    second = MemoryStore()
    with monkeypatch.context() as m:
        _landing_first(m, second, lambda: first.modify_memory(project, a, content="their edit"))
        ok, message = second.archive_memory(project, a)
    assert not ok and "another writer" in message
    assert _entries(project)[a].content == "their edit"
    assert not MemoryStore().search_archive(project)


# ── every caller is told, and the tool's text does not contradict itself ────


def test_promote_and_set_detector_say_when_they_were_refused(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, (a, _b) = _two(project)
    assert first.add_rule(project, "always keep the tests green", reason="r")[0]
    rule = next(e.id for e in first.get_project(project).entries if e.category == "rule")

    second = MemoryStore()
    with monkeypatch.context() as m:
        _landing_first(m, second, lambda: first.modify_memory(project, a, content="their edit"))
        ok, message = second.promote_to_rule(project, a)
    assert not ok and "Not saved" in message

    third = MemoryStore()
    with monkeypatch.context() as m:
        _landing_first(m, third, lambda: first.modify_memory(project, rule, content="their rule"))
        ok, message = third.set_detector(project, rule, {"command": "x"})
    assert not ok and "Not saved" in message


def test_the_tool_says_a_refused_promote_once_and_not_to_repeat_it_blindly(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, (a, _b) = _two(project)
    warm = tools.Warm()
    warm.store.get_project(project)
    with monkeypatch.context() as m:
        _landing_first(m, warm.store, lambda: first.modify_memory(project, a, content="their edit"))
        failed, text = _call(warm, operation="promote", project_path=project, memory_id=a)
    assert failed and text.count("another writer") == 1
    assert "Call this again" not in text and "Promoted" not in text


def test_a_remember_that_did_not_land_does_not_say_remembered(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    _two(project)
    warm = tools.Warm()
    warm.store.get_project(project)
    with monkeypatch.context() as m:
        _no_writes(m)
        failed, text = _call(warm, operation="remember", project_path=project, content="a thing that cannot land")
    assert failed and "Not saved" in text and "Remembered" not in text and "Call this again" in text


def test_a_global_write_that_failed_fails_the_save(monkeypatch):
    store = MemoryStore()
    with monkeypatch.context() as m:
        _no_writes(m)
        assert store.add_priority("a global priority") is False
    assert store._save_error


# ── a project's own fields ──────────────────────────────────────────────────


def test_two_writers_keep_each_others_key_files(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, _ids = _two(project)
    second = MemoryStore()
    proj = second.get_project(project)
    first.remember_key_file(project, "theirs.py", "their file")
    proj.key_files["mine.py"] = "my file"
    second._dirty_projects.add(proj.project_path)
    assert second._save()
    assert set(MemoryStore().get_project(project).key_files) == {"theirs.py", "mine.py"}


def test_a_key_removed_here_stays_removed_beside_another_writers_new_one(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, _ids = _two(project)
    first.remember_key_file(project, "old.py", "going away")
    second = MemoryStore()
    proj = second.get_project(project)
    first.remember_key_file(project, "theirs.py", "their file")
    del proj.key_files["old.py"]
    second._dirty_projects.add(proj.project_path)
    assert second._save()
    assert set(MemoryStore().get_project(project).key_files) == {"theirs.py"}


def test_two_writers_keep_each_others_recent_searches(tmp_path):
    project = str(tmp_path / "p")
    first, _ids = _two(project)
    second = MemoryStore()
    proj = second.get_project(project)
    first.log_search(project, "their query", 1, [])
    proj.recent_searches.append({"query": "my query"})
    second._dirty_projects.add(proj.project_path)
    assert second._save()
    got = {s.get("query") for s in MemoryStore().get_project(project).recent_searches}
    assert got == {"their query", "my query"}


def test_a_project_created_here_does_not_blank_another_writers_first_save(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, second = MemoryStore(), MemoryStore()
    with monkeypatch.context() as m:
        _landing_first(m, second, lambda: first.remember_project(project, summary="their summary"))
        second.remember_project(project, language="python")
    proj = MemoryStore().get_project(project)
    assert proj.summary == "their summary" and proj.language == "python"


# ── a second reading of the same fixes ──────────────────────────────────────


def test_a_rule_with_the_same_text_as_another_writers_new_memory_is_not_lost(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, _ids = _two(project)
    second = MemoryStore()
    text = "the same words from two sessions"
    save, saves = second._save, []

    def at_its_second_save():
        saves.append(1)
        if len(saves) == 2:
            assert first.remember_discovery(project, text, auto_embed=False)[0]
        return save()

    with monkeypatch.context() as m:
        m.setattr(second, "_save", at_its_second_save)
        ok, message = second.add_rule(project, text, detector={"command": "x"})
    assert not ok and "another writer" in message
    assert second.add_rule(project, text, detector={"command": "x"})[0]
    rules = [e for e in _entries(project).values() if e.category == "rule"]
    assert rules and rules[0].detector == {"command": "x"}


def test_the_same_memory_written_by_two_writers_is_kept_once(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, _ids = _two(project)
    second = MemoryStore()
    text = "the same words from two sessions"
    save, saves = second._save, []

    def at_its_second_save():
        saves.append(1)
        if len(saves) == 2:
            assert first.remember_discovery(project, text, auto_embed=False)[0]
        return save()

    with monkeypatch.context() as m:
        m.setattr(second, "_save", at_its_second_save)
        assert second.remember_discovery(project, text, auto_embed=False)[0]
    assert [e.content for e in _entries(project).values()].count(text) == 1


def test_the_order_of_tags_is_not_an_edit():
    one = MemoryEntry(id="a", content="x", tags=["a", "b"], related_files=["f", "g"])
    other = MemoryEntry(id="a", content="x", tags=["b", "a"], related_files=["g", "f"])
    assert _entry_print(one) == _entry_print(other)


def test_retrying_a_failed_write_does_not_bring_back_what_was_deleted_since(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, (a, _b) = _two(project)
    second = MemoryStore()
    real_write, fail = atomic.write_json, []

    def write(path, data, **kwargs):
        if fail and Path(path).name == "memory.json":
            fail.clear()
            raise OSError("disk full")
        return real_write(path, data, **kwargs)

    monkeypatch.setattr(atomic, "write_json", write)
    save = second._save

    def interleaved():
        assert first.remember_discovery(project, "india juliet kilo lima", auto_embed=False)[0]
        fail.append(1)
        return save()

    with monkeypatch.context() as m:
        m.setattr(second, "_save", interleaved)
        assert not second.modify_memory(project, a, relevance=9)[0]
    added = next(e.id for e in first.get_project(project).entries if e.content.startswith("india"))
    assert first.delete_memory(project, added)[0]
    assert second._save()
    got = _entries(project)
    assert added not in got and got[a].relevance == 9


def test_archiving_replaces_an_old_row_with_the_current_text(tmp_path):
    project = str(tmp_path / "p")
    store, (a, _b) = _two(project)
    proj = store.get_project(project)
    entry = next(e for e in proj.entries if e.id == a)
    assert store._copy_into_archive(proj, [entry])
    assert store.modify_memory(project, a, content="the corrected text")[0]
    assert store.archive_memory(project, a)[0]
    assert [e.content for e in MemoryStore().search_archive(project)] == ["the corrected text"]


def test_a_refused_cleanup_takes_its_rows_back_out_of_the_archive(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, (a, b) = _two(project)
    for entry in first.get_project(project).entries:
        entry.last_accessed = 1.0
        entry.relevance = 4
    first._dirty_projects.add(first._normalize_path(project))
    assert first._save()
    second = MemoryStore()
    with monkeypatch.context() as m:
        _landing_first(m, second, lambda: first.modify_memory(project, a, content="their correction"))
        report = second.cleanup_memories(project, dry_run=False, min_relevance=0)
    assert "another writer" in report["error"]
    assert _entries(project)[a].content == "their correction" and b in _entries(project)
    assert not MemoryStore().search_archive(project)


def test_a_stale_move_does_not_replace_a_newer_archive_row(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, (a, _b) = _two(project)
    second = MemoryStore()
    find, landed = second._get_entry_by_id, []

    def read_then_the_other_writer_lands(proj, memory_id):
        found = find(proj, memory_id)
        if not landed:
            landed.append(1)
            assert first.modify_memory(project, a, content="the corrected text")[0]
            assert first.archive_memory(project, a)[0]
        return found

    monkeypatch.setattr(second, "_get_entry_by_id", read_then_the_other_writer_lands)
    second.archive_memory(project, a)
    assert [e.content for e in MemoryStore().search_archive(project)] == ["the corrected text"]
    assert a not in _entries(project)


def test_a_refused_cleanup_leaves_another_writers_archive_row(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, (a, b) = _two(project)
    for entry in first.get_project(project).entries:
        entry.last_accessed = 1.0
        entry.relevance = 4
    first._dirty_projects.add(first._normalize_path(project))
    assert first._save()
    second = MemoryStore()

    def other_writer():
        assert first.modify_memory(project, a, content="their correction")[0]
        assert first.archive_memory(project, b)[0]

    with monkeypatch.context() as m:
        _landing_first(m, second, other_writer)
        report = second.cleanup_memories(project, dry_run=False, min_relevance=0)
    assert "another writer" in report["error"]
    assert [e.id for e in MemoryStore().search_archive(project)] == [b]
    assert _entries(project)[a].content == "their correction" and b not in _entries(project)


def test_a_refused_move_writes_nothing_to_the_archive(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, (a, _b) = _two(project)
    second = MemoryStore()
    second.get_project(project)
    copied = []
    monkeypatch.setattr(second, "_copy_into_archive", lambda *_a: copied.append(1) or True)
    _landing_first(monkeypatch, second, lambda: first.modify_memory(project, a, content="their edit"))
    assert not second.archive_memory(project, a)[0]
    assert not copied


def test_a_move_whose_hot_write_failed_finishes_at_the_next_save(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    store, (a, _b) = _two(project)
    real_write = atomic.write_json

    def write(path, data, **kwargs):
        if Path(path).name == "memory.json":
            raise OSError("disk full")
        return real_write(path, data, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(atomic, "write_json", write)
        ok, message = store.archive_memory(project, a)
    assert not ok and "Not saved" in message
    assert [e.id for e in MemoryStore().search_archive(project)] == [a]
    assert store._save()
    assert a not in _entries(project)
    assert [e.id for e in MemoryStore().search_archive(project)] == [a]


def _refreshable(monkeypatch, replaced: dict):
    monkeypatch.setattr(draft, "_hook", lambda _name: lambda *_a: ({"task_id": "t", "trigger": "manual"}, []))
    monkeypatch.setattr(draft, "merge_into_deliberate", lambda own, _record: (own, ["handoff_summary"]))
    monkeypatch.setattr(ck, "replace_handoff", lambda _entry, _dirs: replaced)


def test_a_refresh_a_held_ring_refused_stays_owed(tmp_path, monkeypatch):
    from windvane import pressure

    project = str(tmp_path)
    state: dict = {"edits_total": 5}
    pressure.pressure_state(state)["edits_at_manual_checkpoint"] = 3
    _refreshable(monkeypatch, {"unread": ["ring"]})
    assert draft.refresh_deliberate({}, project, "s", "", state) is None
    assert pressure.pressure_state(state)["edits_at_manual_checkpoint"] == 3
    _refreshable(monkeypatch, {"unread": []})
    assert draft.refresh_deliberate({}, project, "s", "", state) is not None
    assert pressure.pressure_state(state)["edits_at_manual_checkpoint"] == 5


def test_a_refresh_of_this_turns_save_a_held_ring_refused_stays_owed(tmp_path, monkeypatch):
    from windvane import pressure

    project = str(tmp_path)
    state: dict = {"edits_total": 5}
    pressure.pressure_state(state)["edits_at_manual_checkpoint"] = 5
    _refreshable(monkeypatch, {"unread": ["ring"]})
    assert draft.refresh_deliberate({}, project, "s", "", state, turn_saved=True) is None
    _refreshable(monkeypatch, {"unread": []})
    assert draft.refresh_deliberate({}, project, "s", "", state) is not None


# ── what is not an edit does not read as one ────────────────────────────────


def test_a_row_with_no_created_at_is_not_a_conflict(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    store, (a, b) = _two(project)
    path = _memory_file(store, project)
    data = json.loads(path.read_text(encoding="utf-8"))
    for row in data["entries"]:
        del row["created_at"]
    path.write_bytes(json.dumps(data).encode("utf-8"))
    first, second = MemoryStore(), MemoryStore()
    second.get_project(project)
    first.get_project(project)
    time.sleep(0.01)  # a row loaded again gets a new default time
    _landing_first(monkeypatch, second, lambda: first.modify_memory(project, b, relevance=8))
    assert second.modify_memory(project, a, relevance=9)[0]
    got = _entries(project)
    assert got[a].relevance == 9 and got[b].relevance == 8


def test_a_fractional_relevance_is_not_a_conflict(tmp_path, monkeypatch):
    project = str(tmp_path / "p")
    first, (a, b) = _two(project)
    second = MemoryStore()
    assert second.modify_memory(project, a, relevance=7.5)[0]
    _landing_first(monkeypatch, second, lambda: first.modify_memory(project, b, relevance=8))
    assert second.modify_memory(project, a, content="my edit")[0]
    assert _entries(project)[a].content == "my edit"


# ── a bank that did not land ────────────────────────────────────────────────


def _write_fails_in(monkeypatch, ring: Path):
    real = ck._atomic_write

    def write(path, data):
        if Path(path).parent == ring:
            raise OSError("disk full")
        return real(path, data)

    monkeypatch.setattr(ck, "_atomic_write", write)


def test_a_ring_write_that_failed_is_named_and_the_next_ring_is_still_tried(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir(), second.mkdir()
    _write_fails_in(monkeypatch, first)
    handoff = {"kind": "manual", "summary": "s", "task_description": "t", "project_path": "p"}
    report = ck.write_handoff(handoff, [first, second])
    assert report["unread"] == [str(first)]
    assert atomic.read_json(second / ck.HISTORY_FILENAME)


def test_an_automatic_bank_that_failed_raises_so_it_is_not_counted_as_banked(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    ring = ck.register_project_ring(str(project))
    _write_fails_in(monkeypatch, ring)
    record = {"task_description": "t", "current_step": "s", "completed_steps": [], "pending_steps": ["n"],
              "files_involved": ["a.py"], "handoff_summary": "note", "project_path": str(project)}
    monkeypatch.setattr(draft, "ring_record", lambda r, _s, _t: {**r, "kind": "manual", "summary": "note"})
    with pytest.raises(OSError):
        draft.bank(record, "session", trigger="stop")


def test_a_pruned_task_file_takes_its_lock_with_it(tmp_path):
    storage = tmp_path / "store"
    folder = storage / "checkpoints"
    folder.mkdir(parents=True)
    task = folder / "task_1.json"
    task.write_bytes(b"{}")
    lock = atomic.lock_path(task)
    lock.write_bytes(b"")
    old = time.time() - 400 * 86400
    os.utime(task, (old, old))
    assert task in ck.prune_task_files(storage)
    assert not task.exists() and not lock.exists()


# ── goals ───────────────────────────────────────────────────────────────────


def _message(ident, parent, text="Work", kind="user"):
    return {"uuid": ident, "parentUuid": parent, "type": kind, "message": {"content": text}}


def _status(ident, parent, **fields):
    return {"uuid": ident, "parentUuid": parent, "type": "attachment", "timestamp": "2026-01-01T00:00:00Z",
            "attachment": {"type": "goal_status", "condition": "Finish the task", **fields}}


def _system(ident, parent):
    return {"uuid": ident, "parentUuid": parent, "type": "system", "subtype": "stop_hook_summary"}


def _write(path: Path, *records, mode="wb"):
    with open(path, mode) as fh:
        fh.write(("\n".join(json.dumps(r) for r in records) + "\n").encode("utf-8"))
    return str(path)


def test_a_goal_set_after_a_system_record_past_the_last_message_is_seen(tmp_path):
    path = _write(tmp_path / "t.jsonl", _message("root", None), _system("s", "root"), _status("g", "s", sentinel=True))
    scan = goal.scan_goal(path)
    assert scan["seen"] and scan["active"]


def test_a_verdict_after_a_system_record_past_the_last_message_ends_the_goal(tmp_path):
    path = _write(tmp_path / "t.jsonl", _message("root", None), _status("g", "root", sentinel=True),
                  _message("w", "g"), _system("s", "w"), _status("v", "s", met=True))
    assert goal.scan_goal(path)["ended"] == "met"


def test_a_system_record_on_an_abandoned_branch_carries_nothing(tmp_path):
    path = _write(tmp_path / "t.jsonl", _message("root", None), _message("gone", "root"), _system("s", "gone"),
                  _status("g", "s", sentinel=True), _message("live", "root"))
    assert not goal.scan_goal(path)["seen"]


def test_a_goal_rewound_back_with_an_odd_run_record_still_starts(tmp_path):
    path = _write(tmp_path / "t.jsonl", _message("root", None), _status("g", "root", sentinel=True), _message("w", "g"))
    state: dict = {}
    goal._run(state)["auto"] = {"status": "met", "set_at": "2026-01-01T00:00:00Z"}
    event = goal.observe(state, path)
    assert event and event["event"] == "started"


def test_a_goal_whose_records_are_past_the_tail_keeps_running_and_counting(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, _message("root", None), _status("g", "root", sentinel=True), _message("w0", "g"))
    state: dict = {}
    assert goal.observe(state, str(path))["event"] == "started"
    filler = "x" * 4096
    rows = [_message(f"w{i}", f"w{i - 1}", filler) for i in range(1, 650)]  # over 2 MB
    _write(path, *rows, mode="ab")
    assert goal.observe(state, str(path), turn=True) is None
    run = goal.auto(state)
    assert run["status"] == goal.RUNNING and run["turns"] == 1

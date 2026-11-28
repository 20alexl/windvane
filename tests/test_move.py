"""A project's memory follows its folder: a move brings the old project's
memories and archive into the new one, and forgets the old one only once
they are written."""

import json
from pathlib import Path

import pytest

from windvane import atomic, move
from windvane.store import MemoryStore


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")


def _old_and_new(tmp_path: Path):
    old, new = tmp_path / "game", tmp_path / "games" / "game"
    old.mkdir()
    store = MemoryStore()
    assert store.add_rule(str(old), "never ship without the gate", reason="r")[0]
    assert store.remember_discovery(str(old), f"the lobby lives in {old / 'src' / 'lobby.luau'}", auto_embed=False)[0]
    assert store.remember_discovery(str(old), "a memory that goes to the archive later", auto_embed=False)[0]
    archived = next(e.id for e in store.get_project(str(old)).entries if "archive later" in e.content)
    assert store.archive_memory(str(old), archived)[0]
    new.mkdir(parents=True)
    old.rename(tmp_path / "gone")  # the folder moved away
    return str(old), str(new), archived


def test_a_move_brings_the_memories_and_the_archive_and_forgets_the_old_project(tmp_path):
    old, new, archived = _old_and_new(tmp_path)
    report = move.move_project(old, new)
    assert report["moved"] == 2 and report["archived"] == 1 and report["forgot"]
    store = MemoryStore()
    entries = store.get_project(new).entries
    assert {e.category for e in entries} == {"rule", "discovery"}
    lobby = next(e for e in entries if "lobby" in e.content)
    assert lobby.content.replace("\\", "/").lower().startswith(f"the lobby lives in {new}".replace("\\", "/").lower())
    assert store.get_project(old) is None
    assert [e.id for e in store.search_archive(new)] == [archived]


def test_what_the_new_project_already_has_is_not_doubled(tmp_path):
    old, new, _archived = _old_and_new(tmp_path)
    store = MemoryStore()
    assert store.add_rule(new, "never ship without the gate", reason="r")[0]
    report = move.move_project(old, new)
    assert report["moved"] == 1 and report["already_there"] == 1
    rules = [e for e in MemoryStore().get_project(new).entries if e.category == "rule"]
    assert len(rules) == 1


def test_a_move_that_could_not_write_keeps_the_old_project(tmp_path, monkeypatch):
    old, new, _archived = _old_and_new(tmp_path)

    def refuse(*_args, **_kwargs):
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(atomic, "write_json", refuse)
        report = move.move_project(old, new)
    assert "error" in report and not report.get("forgot")
    assert MemoryStore().get_project(old) is not None


def test_a_move_onto_itself_or_from_nothing_says_so(tmp_path):
    old, new, _archived = _old_and_new(tmp_path)
    assert "error" in move.move_project(new, new)
    assert "error" in move.move_project(str(tmp_path / "never"), new)


def test_the_command_line_prints_its_report(tmp_path, capsys):
    old, new, _archived = _old_and_new(tmp_path)
    assert move.main([old, new]) == 0
    assert json.loads(capsys.readouterr().out)["moved"] == 2


def test_the_hint_names_a_gone_project_with_this_folders_name(tmp_path):
    old, new, _archived = _old_and_new(tmp_path)
    MemoryStore().remember_project(new)
    lines = move.moved_hint(new)
    text = "\n".join(lines)
    assert "3 memories" in text or "2 memories" in text
    assert "move.py" in text and Path(old).name in text
    move.move_project(old, new)
    assert move.moved_hint(new) == []


def test_no_hint_for_a_project_whose_old_folder_still_exists(tmp_path):
    a, b = tmp_path / "one" / "app", tmp_path / "two" / "app"
    a.mkdir(parents=True), b.mkdir(parents=True)
    assert MemoryStore().remember_discovery(str(a), "a memory of the first app", auto_embed=False)[0]
    assert move.moved_hint(str(b)) == []

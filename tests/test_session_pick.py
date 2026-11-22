"""The checkpoint a session is shown at its start is its own: found however many
records other sessions saved since, and another session's record is never
presented as this session's state."""

import json
import time
from pathlib import Path

import pytest

common = pytest.importorskip("windvane.events.common", reason="the banner pick is the hooks port's")
from windvane import checkpoints as ck  # noqa: E402
from windvane.store import MemoryStore  # noqa: E402


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    monkeypatch.setattr(common, "_session_id", "mine")
    monkeypatch.setattr(common, "_stdin_cache", None)


def _hub(tmp_path: Path, *names: str):
    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)
    store = MemoryStore()
    store.remember_project(str(ws))
    rings = {}
    for name in names:
        (ws / name / ".git").mkdir(parents=True)
        store.remember_project(str(ws / name))
        rings[name] = ck.register_project_ring(str(ws / name))
    return ws, rings


def _record(task_id, session, project, created, text):
    return {"task_id": task_id, "kind": "manual", "created": created, "session_id": session,
            "project_path": common._normalize_path(str(project)), "summary": text, "task_description": text}


def _write(ring: Path, records: list):
    (ring / "handoff_history.json").write_bytes(json.dumps({"handoffs": records}).encode("utf-8"))


def test_a_resumed_session_finds_its_own_record_under_many_newer_ones(tmp_path):
    ws, rings = _hub(tmp_path, "game", "lab")
    now = time.time()
    _write(rings["game"], [_record("task_own", "mine", ws / "game", now - 86400, "wire the lobby")])
    _write(rings["lab"], [_record(f"task_{n}", "theirs", ws / "lab", now - n, f"their work {n}") for n in range(30)])
    restored, _skipped = common._banner_checkpoint(str(ws), str(ws), "resume", False, "")
    assert restored["task_id"] == "task_own"
    assert "another session" not in "\n".join(common._format_restored_full(restored))


def test_another_sessions_record_on_resume_is_a_labelled_pointer_not_the_state(tmp_path):
    ws, rings = _hub(tmp_path, "lab")
    record = _record("task_theirs", "theirs", ws / "lab", time.time() - 60, "build the GTA level")
    record["completed_steps"] = ["their first step"]
    _write(rings["lab"], [record])
    restored, _skipped = common._banner_checkpoint(str(ws), str(ws), "resume", False, "")
    text = "\n".join(common._format_restored_full(restored))
    assert "task_theirs" in text and "another session" in text
    assert "their first step" not in text


def test_a_fresh_start_labels_another_sessions_record(tmp_path):
    ws, rings = _hub(tmp_path, "lab")
    _write(rings["lab"], [_record("task_theirs", "theirs", ws / "lab", time.time() - 60, "their work")])
    restored, _skipped = common._banner_checkpoint(str(ws), str(ws), "startup", False, "")
    assert "another session" in "\n".join(common._format_restored_context(restored))


def test_the_marker_never_reaches_a_saved_record(tmp_path):
    ws, rings = _hub(tmp_path, "lab")
    original = _record("task_theirs", "theirs", ws / "lab", time.time() - 60, "their work")
    _write(rings["lab"], [original])
    common._banner_checkpoint(str(ws), str(ws), "resume", False, "")
    stored = json.loads((rings["lab"] / "handoff_history.json").read_text(encoding="utf-8"))["handoffs"]
    assert stored == [original]


def test_the_prompt_hooks_fallback_does_not_call_another_sessions_record_its_own(tmp_path):
    from windvane.events import prompt

    ws, rings = _hub(tmp_path, "lab")
    _write(rings["lab"], [_record("task_theirs", "theirs", ws / "lab", time.time() - 60, "their work")])
    lines = prompt._restored_lines(str(ws / "lab"))
    text = "\n".join(lines)
    assert "task_theirs" in text and "another session" in text and "CONTINUE FROM WHERE YOU LEFT OFF" not in text


def test_the_history_can_be_read_whole():
    assert ck.read_history([], limit=None) == []

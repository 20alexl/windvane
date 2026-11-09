"""A goal is read from the live branch of the conversation: what was rewound away
no longer sets, ends or clears it."""

import json
from pathlib import Path

import pytest

from windvane import goal


@pytest.fixture
def transcript(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    path = tmp_path / "session.jsonl"

    def write(*records) -> str:
        path.write_bytes(("\n".join(json.dumps(r) for r in records) + "\n").encode("utf-8"))
        return str(path)

    return write


def message(ident, parent, text="Work", kind="user"):
    return {"uuid": ident, "parentUuid": parent, "type": kind, "message": {"content": text}}


def status(ident, parent, **fields):
    return {"uuid": ident, "parentUuid": parent, "type": "attachment", "timestamp": "2026-01-01T00:00:00Z",
            "attachment": {"type": "goal_status", "condition": "Finish the task", **fields}}


CLEAR = "<command-name>/goal</command-name><command-args>clear</command-args>"


def test_a_goal_on_the_live_branch_is_active(transcript):
    scan = goal.scan_goal(transcript(message("root", None), status("g", "root", sentinel=True), message("w", "g", kind="assistant")))
    assert scan["seen"] and scan["active"] and scan["condition"] == "Finish the task"


def test_rewinding_past_the_goal_ends_the_run(transcript):
    root, sentinel, work = message("root", None), status("g", "root", sentinel=True), message("w", "g", kind="assistant")
    state: dict = {}
    assert goal.observe(state, transcript(root, sentinel, work))["event"] == "started"
    rewound = transcript(root, sentinel, work, message("other", "root", "A different task"))
    assert not goal.scan_goal(rewound)["seen"]
    assert goal.observe(state, rewound)["event"] == "ended"
    assert not goal.running(state)


def test_a_clear_on_an_abandoned_branch_does_not_end_the_goal(transcript):
    scan = goal.scan_goal(transcript(message("root", None), status("g", "root", sentinel=True), message("c", "g", CLEAR),
                                     message("other", "g", "Carry on")))
    assert scan["active"] and scan["ended"] is None


def test_a_verdict_on_an_abandoned_branch_does_not_end_the_goal(transcript):
    scan = goal.scan_goal(transcript(message("root", None), status("g", "root", sentinel=True), status("met", "g", met=True),
                                     message("w", "met", kind="assistant"), message("other", "g", "Carry on")))
    assert scan["active"] and scan["ended"] is None and scan["verdicts"] == 0


def test_a_verdict_and_a_clear_at_the_tip_count(transcript):
    assert goal.scan_goal(transcript(message("root", None), status("g", "root", sentinel=True),
                                     message("w", "g", kind="assistant"), status("v", "w", met=True)))["ended"] == "met"
    assert goal.scan_goal(transcript(message("root", None), status("g", "root", sentinel=True),
                                     message("c", "g", CLEAR)))["ended"] == "cleared"


def test_an_attachment_written_before_its_live_parent_counts(transcript):
    scan = goal.scan_goal(transcript(message("root", None), status("g", "root", sentinel=True), status("v", "w", met=True),
                                     message("w", "g", kind="assistant")))
    assert scan["ended"] == "met"


def test_an_attachment_late_from_an_abandoned_parent_does_not(transcript):
    scan = goal.scan_goal(transcript(message("root", None), message("old", "root", kind="assistant"), message("new", "root"),
                                     status("late", "old", sentinel=True)))
    assert not scan["seen"]


def test_a_record_without_ids_belongs_to_the_conversation_before_it(transcript):
    bare = {"type": "attachment", "attachment": {"type": "goal_status", "sentinel": True, "condition": "Old shape"}}
    assert not goal.scan_goal(transcript(message("root", None), message("old", "root", kind="assistant"), bare, message("new", "root")))["seen"]
    assert goal.scan_goal(transcript(message("root", None), bare))["active"]
    assert goal.scan_goal(transcript(bare))["active"]


def test_a_goal_and_its_verdict_survive_a_compaction(transcript):
    boundary = {"uuid": "cb", "parentUuid": None, "type": "system", "subtype": "compact_boundary", "logicalParentUuid": "g",
                "compactMetadata": {"preservedMessages": {"uuids": ["g"]}}}
    scan = goal.scan_goal(transcript(message("root", None), status("g", "root", sentinel=True), boundary,
                                     message("sum", "cb"), status("v", "sum", met=True)))
    assert scan["ended"] == "met"


def test_a_subagents_goal_is_not_this_conversations(transcript):
    child = status("child", "root", sentinel=True)
    child["isSidechain"] = True
    assert not goal.scan_goal(transcript(message("root", None), child))["seen"]


def test_a_tail_with_no_goal_is_not_parsed(transcript, monkeypatch):
    """This runs on every prompt and every stop over up to 2 MB of tail."""
    path = transcript(message("root", None), message("w", "root", kind="assistant"))

    def no_parse(_raw):
        raise AssertionError("parsed a tail that holds no goal")

    monkeypatch.setattr(goal.json, "loads", no_parse)
    assert not goal.scan_goal(path)["seen"]


def test_rewinding_a_completion_restores_the_goal_and_keeps_its_count(transcript):
    root, sentinel, work = message("root", None), status("g", "root", sentinel=True), message("w", "g", kind="assistant")
    state: dict = {}
    goal.observe(state, transcript(root, sentinel, work), turn=True)
    met = status("met", "w", met=True)
    assert goal.observe(state, transcript(root, sentinel, work, met), turn=True)["event"] == "ended"
    spent = goal.auto(state)["turns"]
    again = goal.observe(state, transcript(root, sentinel, work, met, message("other", "w", "Not done yet")), turn=True)
    assert again["event"] == "started" and goal.running(state)
    assert goal.auto(state)["turns"] == spent + 1


@pytest.mark.parametrize("how", ["halted", "capped", "stopped"])
def test_a_rewind_does_not_release_a_local_stop(transcript, how):
    path = transcript(message("root", None), status("g", "root", sentinel=True))
    state: dict = {}
    goal.observe(state, path)
    goal.stop(state, how)
    assert goal.observe(state, path) is None and not goal.running(state)

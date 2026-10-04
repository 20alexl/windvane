"""The live branch of a transcript: rewinds, compactions, leaf tool results."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from windvane import transcript as tc

CHECKPOINT_TOOL = "mcp__windvane__checkpoint"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")


def _tr_rec(uuid, parent, typ, content, **extra):
    """One transcript record as Claude Code writes it."""
    return {"uuid": uuid, "parentUuid": parent, "type": typ, "isSidechain": False,
            "timestamp": "2026-09-26T00:00:00Z", "message": {"role": typ, "content": content}, **extra}


def _tr_result(call, text, is_error=False):
    block = {"type": "tool_result", "tool_use_id": call, "content": [{"type": "text", "text": text}]}
    if is_error:
        block["is_error"] = True
    return [block]


def _tr_call(cid, name, inp):
    return [{"type": "tool_use", "id": cid, "name": name, "input": inp}]


def _write_transcript(path: Path, recs: list) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    return path


def _rewound_transcript(tmp_path: Path) -> Path:
    """A transcript after one rewind, as Claude Code writes it: append-only,
    the abandoned turn (prompt 2, checkpoint task_1) still in the file, the
    new prompt 3 hung off the END OF TURN 1 (its parent is r2), checkpoint
    task_2 on the live branch. Measured on a real rewind, 2026-09-26."""
    rec, result = _tr_rec, _tr_result

    def call(cid, op):
        return _tr_call(cid, CHECKPOINT_TOOL, {"operation": op})

    # the chain runs through the system records between turns: a prompt's
    # parent is the previous turn's turn_duration record, as measured. The
    # live branch ends with a checkpoint(restore) whose RESULT quotes task_1:
    # a quoted id is not a save.
    recs = [
        rec("r1", None, "user", "let's use sqlite for the alias store"),
        rec("r2", "r1", "assistant", [{"type": "text", "text": "Done; the store is sqlite now."}]),
        {"uuid": "s1", "parentUuid": "r2", "type": "system", "subtype": "turn_duration", "isSidechain": False},
        rec("r3", "s1", "user", "let's use the registry for every alias lookup"),
        rec("r4", "r3", "assistant", call("c1", "save")),
        rec("r5", "r4", "user", result("c1", "Checkpoint saved.\ntask_id: task_1\n")),
        rec("r6", "r5", "assistant", [{"type": "text", "text": "Saved."}]),
        {"uuid": "s2", "parentUuid": "r6", "type": "system", "subtype": "turn_duration", "isSidechain": False},
        rec("r7", "s1", "user", "never resolve aliases outside the registry"),
        rec("r8", "r7", "assistant", call("c2", "save")),
        rec("r9", "r8", "user", result("c2", "Checkpoint saved.\ntask_id: task_2\n")),
        rec("r10", "r9", "assistant", call("c3", "restore")),
        rec("r11", "r10", "user", result("c3", "**Task:** rewound branch task\ntask_id: task_1\n")),
        rec("r12", "r11", "assistant", [{"type": "text", "text": "Restored."}]),
        {"uuid": "s3", "parentUuid": "r12", "type": "system", "subtype": "turn_duration", "isSidechain": False},
    ]
    return _write_transcript(tmp_path / "rewound.jsonl", recs)


def test_a_rewind_leaves_a_fork_and_the_live_chain_skips_the_abandoned_branch(tmp_path: Path):
    """No hook fires on a rewind and no record marks it; the transcript is
    append-only and the rewind shows only as a fork. The live chain is the
    walk from the last record up its parent links (2026-09-26)."""
    p = _rewound_transcript(tmp_path)
    assert tc.live_chain(p) == {"r1", "r2", "s1", "r7", "r8", "r9", "r10", "r11", "r12"}
    live, everywhere = tc.branch_checkpoints(p)
    assert live == {"task_2"} and everywhere == {"task_1", "task_2"}
    assert tc.rewound_away({"task_id": "task_1"}, p)
    assert not tc.rewound_away({"task_id": "task_2"}, p)
    assert not tc.rewound_away({"task_id": "task_9"}, p)  # never saved through this transcript: nothing to judge
    assert not tc.rewound_away({"task_id": "task_1"}, None)
    assert not tc.rewound_away({}, p)


def test_live_records_are_the_live_chain_in_file_order(tmp_path: Path):
    p = _rewound_transcript(tmp_path)
    assert [d["uuid"] for d in tc.live_records(p)] == ["r1", "r2", "s1", "r7", "r8", "r9", "r10", "r11", "r12"]
    assert tc.live_chain(tmp_path / "missing.jsonl") is None
    assert tc.live_records(tmp_path / "missing.jsonl") == []
    assert tc.chain_of([]) is None


def test_a_sidechain_is_not_this_conversations_chain(tmp_path: Path):
    p = _write_transcript(tmp_path / "side.jsonl", [
        _tr_rec("p1", None, "user", "go"),
        _tr_rec("a1", "p1", "assistant", [{"type": "text", "text": "ok"}]),
        dict(_tr_rec("x1", "a1", "user", "a subagent's prompt"), isSidechain=True),
    ])
    assert tc.live_chain(p) == {"p1", "a1"}


def _compacted_transcript(tmp_path: Path) -> Path:
    """Two compactions, a rewind, four checkpoints, in the shapes measured on
    real transcripts (2026-10-03). task_1's result is a leaf: the next
    assistant record hangs off the record holding the tool_use, beside the
    result. Boundary b1 names its logical parent s1, written before it.
    Boundary b2's logical parent t1 was not yet flushed at compaction time:
    it is written after the compact summary with the summary as its parent,
    and the walk resumes at the newest preserved record before b2 (a3).
    task_3 is on the branch the user then rewound past (p4 forks off t1);
    task_4's result is a leaf again, beside the closing reply."""
    rec, res = _tr_rec, _tr_result

    def save(cid):
        return _tr_call(cid, CHECKPOINT_TOOL, {"operation": "save"})

    def boundary(uuid, logical, preserved):
        return {"uuid": uuid, "parentUuid": None, "logicalParentUuid": logical, "type": "system",
                "subtype": "compact_boundary", "isSidechain": False,
                "compactMetadata": {"trigger": "auto", "preservedMessages": {"uuids": preserved}}}

    def system(uuid, parent, subtype="turn_duration"):
        return {"uuid": uuid, "parentUuid": parent, "type": "system", "subtype": subtype, "isSidechain": False}

    def attachment(uuid, parent):
        return {"uuid": uuid, "parentUuid": parent, "type": "attachment", "isSidechain": False}

    recs = [
        rec("p1", None, "user", "port the alias store to sqlite"),
        rec("a1", "p1", "assistant", save("c1")),
        rec("r1", "a1", "user", res("c1", "Checkpoint saved.\ntask_id: task_1\n")),
        rec("a2", "a1", "assistant", _tr_call("c2", "Bash", {"command": "pytest -q tests/test_alias.py"})),
        rec("r2", "a2", "user", res("c2", "3 passed")),
        rec("a3", "r2", "assistant", [{"type": "text", "text": "Ported."}]),
        system("s1", "a3"),
        boundary("b1", "s1", ["a3", "s1"]),
        rec("u1", "b1", "user", "This session is being continued from a previous conversation.", isCompactSummary=True),
        rec("p2", "u1", "user", "now the registry lookups"),
        rec("a4", "p2", "assistant", save("c3")),
        rec("r3", "a4", "user", res("c3", "Checkpoint saved.\ntask_id: task_2\n")),
        rec("a5", "r3", "assistant", [{"type": "text", "text": "Saved; compacting next."}]),
        boundary("b2", "t1", ["a5", "t1"]),
        attachment("k1", "b2"),
        rec("u2", "k1", "user", "This session is being continued from a previous conversation.", isCompactSummary=True),
        attachment("t1", "u2"),
        rec("p3", "t1", "user", "try the cache first"),
        rec("a6", "p3", "assistant", save("c4")),
        rec("r4", "a6", "user", res("c4", "Checkpoint saved.\ntask_id: task_3\n")),
        rec("a7", "r4", "assistant", [{"type": "text", "text": "Cache tried."}]),
        rec("p4", "t1", "user", "keep the registry, no cache"),
        rec("a8", "p4", "assistant", save("c5")),
        rec("r5", "a8", "user", res("c5", "Checkpoint saved.\ntask_id: task_4\n")),
        rec("a9", "a8", "assistant", [{"type": "text", "text": "Saved."}]),
        system("s2", "a9"),
    ]
    return _write_transcript(tmp_path / "compacted.jsonl", recs)


def test_the_live_chain_crosses_both_compaction_shapes_and_still_skips_a_rewind(tmp_path: Path):
    """chain_of stopped at every compact_boundary (parentUuid null), so every
    checkpoint saved before the latest compaction read as rewound past
    (2026-10-03)."""
    p = _compacted_transcript(tmp_path)
    order = ["p1", "a1", "r1", "a2", "r2", "a3", "s1", "b1", "u1", "p2", "a4", "r3", "a5",
             "b2", "k1", "u2", "t1", "p4", "a8", "r5", "a9"]  # from the last user/assistant record
    assert [d["uuid"] for d in tc.live_records(p)] == order  # file order
    assert tc.live_chain(p) == set(order)  # both walks agree
    on_branch, everywhere = tc.branch_checkpoints(p)
    assert on_branch == {"task_1", "task_2", "task_4"} and everywhere == {"task_1", "task_2", "task_3", "task_4"}
    assert tc.rewound_away({"task_id": "task_3"}, p)
    for tid in ("task_1", "task_2", "task_4"):
        assert not tc.rewound_away({"task_id": tid}, p), tid


def test_a_leaf_tool_result_beside_the_chain_is_live(tmp_path: Path):
    """A checkpoint(save) result whose turn went on with another tool call is
    a leaf: the next assistant record shares its parent. No compaction, no
    rewind; it read as rewound (2026-10-03)."""
    rec, res = _tr_rec, _tr_result
    p = _write_transcript(tmp_path / "leaf.jsonl", [
        rec("p1", None, "user", "bank it and run the tests"),
        rec("a1", "p1", "assistant", _tr_call("c1", CHECKPOINT_TOOL, {"operation": "save"})),
        rec("r1", "a1", "user", res("c1", "Checkpoint saved.\ntask_id: task_1\n")),
        rec("a2", "a1", "assistant", _tr_call("c2", "Bash", {"command": "pytest -q tests/test_alias.py"})),
        rec("r2", "a2", "user", res("c2", "3 passed")),
        rec("x1", "a1", "user", "an abandoned prompt hung off the same record"),
        rec("a3", "r2", "assistant", [{"type": "text", "text": "Done."}]),
    ])
    assert tc.live_chain(p) == {"p1", "a1", "r1", "a2", "r2", "a3"}  # x1 answers no tool call
    assert tc.branch_checkpoints(p) == ({"task_1"}, {"task_1"})


def test_only_a_save_through_the_checkpoint_tool_counts(tmp_path: Path):
    """A task id printed by another tool, or by a restore, is not a save."""
    rec, res = _tr_rec, _tr_result
    p = _write_transcript(tmp_path / "other.jsonl", [
        rec("p1", None, "user", "look"),
        rec("a1", "p1", "assistant", _tr_call("c1", "Bash", {"command": "cat notes.txt"})),
        rec("r1", "a1", "user", res("c1", "task_id: task_5\n")),
        rec("a2", "r1", "assistant", _tr_call("c2", CHECKPOINT_TOOL, {"operation": "restore"})),
        rec("r2", "a2", "user", res("c2", "task_id: task_6\n")),
        rec("a3", "r2", "assistant", [{"type": "text", "text": "Read."}]),
    ])
    assert tc.branch_checkpoints(p) == (set(), set())


def test_a_bounded_tail_read_starts_at_a_whole_line(tmp_path: Path):
    p = _rewound_transcript(tmp_path)
    size = p.stat().st_size
    # A window that cuts into the first records: the partial line is dropped
    # and the walk ends where a parent falls outside the window.
    chain = tc.live_chain(p, tail_bytes=size // 2)
    assert chain and "r12" in chain and "r1" not in chain
    assert tc.live_chain(p, tail_bytes=size * 2) == tc.live_chain(p)

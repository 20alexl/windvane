"""The recorder's draft of a checkpoint: what it reads, and the three
fixes (a closing line that says a step is done closes it, a bulleted list
is not the handoff when prose stands near it, and a title carried across a
compaction from before the session is re-drafted)."""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    store = tmp_path / "store"
    monkeypatch.setenv("WINDVANE_DIR", str(store))
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    return store


def _rec(uuid, parent, typ, content, **extra):
    return {"uuid": uuid, "parentUuid": parent, "type": typ, "isSidechain": False,
            "timestamp": "2026-09-26T00:00:00Z", "message": {"role": typ, "content": content}, **extra}


def _res(call, text, is_error=False):
    block = {"type": "tool_result", "tool_use_id": call, "content": [{"type": "text", "text": text}]}
    if is_error:
        block["is_error"] = True
    return [block]


def _call(cid, name, inp):
    return [{"type": "tool_use", "id": cid, "name": name, "input": inp}]


def _fresh() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write(path: Path, recs: list) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    return path


def _project_with_ring(tmp_path, sid):
    """A registered project whose ring holds this session's deliberate checkpoint."""
    from windvane.store import MemoryStore

    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "pyproject.toml").write_bytes(b"[project]\nname = 'proj'\n")
    store = MemoryStore()
    store.remember_project(str(proj))
    ring = store._project_dir(store._normalize_path(str(proj)))
    entry = {"task_id": "task_9", "kind": "manual", "created": time.time() - 60, "session_id": sid,
             "summary": "wire the brief", "task_description": "Phase 2 brief CLI",
             "current_step": "the compact hook", "completed_steps": ["agents hook"],
             "next_steps": ["the compact hook", "smoke test"], "files_in_progress": ["src/loader.py"],
             "warnings": ["never push"], "context_needed": ["the mod design"], "project_path": str(proj)}
    (ring / "handoff_history.json").write_text(json.dumps({"handoffs": [entry]}), encoding="utf-8")
    return proj, ring, entry


def _session_transcript(tmp_path, proj, closing="Built the draft module.\n\nNext: **wire the CLI**, then the tests.\n\nWhen you have a moment, reconnect the server."):
    """Two tasks created, a compaction, task 1 started then completed, two
    edits, a commit, a failed commit, an edit on a rewound branch, and the
    closing reply; one commit is older than the previous checkpoint."""
    draft_py, cli_py = str(proj / "src" / "draft.py"), str(proj / "src" / "cli.py")
    recs = [
        _rec("u1", None, "user", "Build the recorder's draft of the checkpoint"),
        _rec("a0", "u1", "assistant", _call("c0", "Bash", {"command": 'git commit -q -m "old: before the previous checkpoint"'})),
        _rec("u0", "a0", "user", _res("c0", "ok")),
        _rec("a1", "u0", "assistant", _call("c1", "TaskCreate", {"subject": "the compact hook"})),
        _rec("u2", "a1", "user", _res("c1", "Task #1 created successfully: the compact hook")),
        _rec("a2", "u2", "assistant", _call("c2", "TaskCreate", {"subject": "wire the CLI"})),
        _rec("u3", "a2", "user", _res("c2", "Task #2 created successfully: wire the CLI")),
        {"uuid": "b1", "parentUuid": None, "logicalParentUuid": "u3", "type": "system",
         "subtype": "compact_boundary", "isSidechain": False},
        _rec("u4", "b1", "user", "This session is being continued from a previous conversation.", isCompactSummary=True),
        _rec("a3", "u4", "assistant", _call("c3", "TaskUpdate", {"taskId": "1", "status": "in_progress"})),
        _rec("u5", "a3", "user", _res("c3", "Updated task #1 status")),
        _rec("a4", "u5", "assistant", _call("c4", "Edit", {"file_path": draft_py, "old_string": "a", "new_string": "b"})),
        _rec("u6", "a4", "user", _res("c4", "ok")),
        _rec("x1", "u6", "assistant", _call("cx", "Edit", {"file_path": str(proj / "src" / "abandoned.py")})),
        _rec("x2", "x1", "user", _res("cx", "ok")),
        _rec("a5", "u6", "assistant", _call("c5", "Write", {"file_path": cli_py, "content": "x"})),
        _rec("u7", "a5", "user", _res("c5", "ok")),
        _rec("a6", "u7", "assistant", _call("c6", "Bash", {"command": 'git commit -q -m "draft: the recorder fills the record"'})),
        _rec("u8", "a6", "user", _res("c6", "abc123 draft: the recorder fills the record")),
        _rec("a7", "u8", "assistant", _call("c7", "Bash", {"command": 'git commit -q -m "a commit that failed"'})),
        _rec("u9", "a7", "user", _res("c7", "nothing to commit", is_error=True)),
        _rec("a8", "u9", "assistant", _call("c8", "TaskUpdate", {"taskId": "1", "status": "completed"})),
        _rec("u10", "a8", "user", _res("c8", "Updated task #1 status")),
        _rec("a9", "u10", "assistant", [{"type": "text", "text": closing}]),
        {"uuid": "s1", "parentUuid": "a9", "type": "system", "subtype": "turn_duration", "isSidechain": False},
    ]
    fresh = _fresh()
    for r in recs:
        r["timestamp"] = "2026-09-26T00:00:00Z" if r["uuid"] in ("a0", "u0") else fresh
    return _write(tmp_path / "draft.jsonl", recs)


SUMMARY = "Next: wire the CLI, then the tests."


def _context(monkeypatch):
    from windvane import draft

    monkeypatch.setattr(draft, "_session_context", lambda wp: {"decisions": ["use the ring vocabulary"]})


def _own_previous(monkeypatch, entry):
    """Stand in for the hooks' own-session pick (``_own_session_checkpoint``),
    which the hooks port owns: the ring record above IS this session's."""
    from windvane import draft

    monkeypatch.setattr(draft, "_previous", lambda *a, **k: (entry, "previous checkpoint"))


# ── the draft ───────────────────────────────────────────────────────────────


def test_the_draft_reads_the_previous_record_the_live_chain_and_the_hook_state(tmp_path, monkeypatch):
    from windvane import draft as d

    sid = "aaaaaaaa-0000-4000-8000-0000000000d1"
    proj, _ring, entry = _project_with_ring(tmp_path, sid)
    tp = _session_transcript(tmp_path, proj)
    _context(monkeypatch)
    _own_previous(monkeypatch, entry)
    monkeypatch.setattr(d, "work_project_of", lambda project_dir, state=None: project_dir)
    state = {"test_runs_this_session": 2, "last_test_passed": True,
             "pressure": {"milestone_pending": {"quote": "Phase 3 draft built", "kind": "claim"}}}

    rec = d.draft(str(proj), sid, str(tp), state)
    assert rec["task_description"] == "Phase 2 brief CLI"
    assert rec["completed_steps"] == ["commit: draft: the recorder fills the record", "the compact hook",
                                      "tests: 2 runs, last passed", "Phase 3 draft built"]
    assert rec["pending_steps"] == ["wire the CLI", "smoke test"]
    assert rec["files_involved"] == [str(proj / "src" / "draft.py"), str(proj / "src" / "cli.py")]
    assert rec["handoff_summary"] == SUMMARY and rec["current_step"] == SUMMARY
    assert rec["handoff_warnings"] == ["never push"] and rec["handoff_context_needed"] == ["the mod design"]
    assert rec["key_decisions"] == ["use the ring vocabulary"]
    assert rec["metadata"]["draft_sources"] == {
        "task_description": "previous checkpoint",
        "current_step": "closing reply",
        "completed_steps": "task list+commits+hook state",
        "pending_steps": "task list+previous checkpoint",
        "files_involved": "transcript edits",
        "key_decisions": "hook state",
        "handoff_summary": "closing reply",
        "handoff_context_needed": "previous checkpoint",
        "handoff_warnings": "previous checkpoint",
    }


def test_with_nothing_to_carry_the_first_prompt_is_the_task(tmp_path, monkeypatch):
    from windvane import draft as d

    proj, _ring, _entry = _project_with_ring(tmp_path, "someone")
    tp = _session_transcript(tmp_path, proj)
    _context(monkeypatch)
    empty = tmp_path / "empty"
    empty.mkdir()
    fresh = d.draft(str(empty), "nobody", str(tp), {})
    assert fresh["task_description"] == "Build the recorder's draft of the checkpoint"
    assert fresh["metadata"]["draft_sources"]["task_description"] == "first prompt"
    assert fresh["handoff_warnings"] == [] and fresh["pending_steps"] == ["wire the CLI"]
    blank = d.draft(str(empty), "nobody", str(tmp_path / "missing.jsonl"), {})
    assert blank["completed_steps"] == [] and blank["files_involved"] == [] and blank["metadata"]["draft"] is True


def test_the_draft_reads_each_commit_shape():
    from windvane.draft import commit_subjects

    cases = {
        "git commit -q -F - <<'EOF'\n0.8.55: one scorer daemon\n\nbody\nEOF": ["0.8.55: one scorer daemon"],
        "git add -A && git commit -m \"$(cat <<'EOF'\nfeat: default encoder\n\nbody\nEOF\n)\"": ["feat: default encoder"],
        'git -C sub commit -q -m "CLAUDE.md: Phase 3 merged\n\nCo-Authored-By: x" && git -C sub log -1': ["CLAUDE.md: Phase 3 merged"],
        'git -c core.hooksPath=/dev/null commit -q -m "session log" -m "trailer"': ["session log"],
        "git commit -m @'\nhere-string subject\n\nbody\n'@": ["here-string subject"],
        'git commit -am "quick fix"; git commit -q -m \'second\'': ["quick fix", "second"],
        'git commit -q -F "$S/msg.txt"': [],
        "git commit -q --no-edit && git merge --no-ff x": [],
        'c1=$(git commit-tree $t1 -p main -m "integration check")': [],
        "git status": [],
    }
    for command, want in cases.items():
        assert commit_subjects(command) == want, command


def test_a_commit_message_file_is_read(tmp_path):
    from windvane.draft import commit_subjects

    msg = tmp_path / "msg.txt"
    msg.write_bytes(b"\nfrom a file\n\nbody\n")
    assert commit_subjects(f'git commit -q -F "{msg.as_posix()}"') == ["from a file"]


# ── fix (a): the closing lines close a pending step ─────────────────────────


def test_a_closing_sentence_that_says_a_step_is_done_closes_it(tmp_path, monkeypatch):
    from windvane import draft as d

    sid = "aaaaaaaa-0000-4000-8000-0000000000a1"
    proj, _ring, entry = _project_with_ring(tmp_path, sid)
    tp = _session_transcript(
        tmp_path, proj,
        closing="Built the draft module and the smoke test passed.\n\nNext: wire the CLI, then the tests.",
    )
    _context(monkeypatch)
    _own_previous(monkeypatch, entry)
    rec = d.draft(str(proj), sid, str(tp), {})
    assert "smoke test" not in rec["pending_steps"] and rec["pending_steps"] == ["wire the CLI"]
    assert rec["completed_steps"][-1] == "smoke test"
    assert rec["metadata"]["draft_sources"]["completed_steps"].endswith("closing reply")


def test_only_a_completion_without_negation_closes_a_step():
    from windvane.draft import _closing_sentences, _said_done

    said = _closing_sentences("The parser fix landed; the loader rewrite is not merged yet.\n\nNext: docs.")
    assert _said_done("parser fix", said)
    assert not _said_done("loader rewrite", said)  # negated
    assert not _said_done("docs", said)  # no completion verb
    assert not _said_done("smoke test", _closing_sentences("Built the draft module."))  # other words
    assert _said_done("Write the export module", _closing_sentences("The export module is written."))
    assert not _said_done("the", _closing_sentences("the thing is done"))  # nothing distinctive


# ── fix (b): a bulleted list is not the handoff when prose stands near ──────


def test_the_closing_skips_a_bulleted_list_when_prose_stands_within_the_lookback():
    from windvane.draft import _closing

    reply = "Two files changed and the suite is green.\n\n- store.py: the merge\n- rules.py: the pack"
    assert _closing(reply) == "Two files changed and the suite is green."
    numbered = "The export landed.\n\n1. rules.md\n2. mistakes.md"
    assert _closing(numbered) == "The export landed."
    # every paragraph in the lookback a list: the last one stands
    only_lists = "- a done\n- b done\n\n- c next"
    assert _closing(only_lists) == "- c next"
    # a prose paragraph beyond the lookback does not count
    far = "Prose far back.\n\n- one\n\n- two\n\n- three"
    assert _closing(far) == "- three"


# ── fix (c): a title carried across a compaction from before the session ───


def test_a_title_older_than_the_session_is_redrafted_after_a_compaction(tmp_path, monkeypatch):
    from windvane import draft as d

    sid = "aaaaaaaa-0000-4000-8000-0000000000c1"
    proj, _ring, entry = _project_with_ring(tmp_path, sid)
    tp = _session_transcript(tmp_path, proj)  # has a compaction
    _context(monkeypatch)
    _own_previous(monkeypatch, entry)  # created 60 s ago

    started_after = {"run": {"started_at": time.time() - 5}}
    rec = d.draft(str(proj), sid, str(tp), started_after)
    assert rec["task_description"] == "Build the recorder's draft of the checkpoint"
    assert rec["metadata"]["draft_sources"]["task_description"] == "first prompt"
    # the rest still carries: warnings and pending are the project's state
    assert rec["handoff_warnings"] == ["never push"] and "smoke test" in rec["pending_steps"]

    started_before = {"run": {"started_at": time.time() - 3600}}
    assert d.draft(str(proj), sid, str(tp), started_before)["task_description"] == "Phase 2 brief CLI"

    # no compaction on the live chain: the title carries whatever its age
    recs = [json.loads(line) for line in tp.read_text(encoding="utf-8").splitlines()]
    flat = []
    for r in recs:
        if r.get("subtype") == "compact_boundary" or r.get("isCompactSummary"):
            continue
        if r["uuid"] == "a3":
            r["parentUuid"] = "u3"
        flat.append(r)
    plain = _write(tmp_path / "plain.jsonl", flat)
    assert d.draft(str(proj), sid, str(plain), started_after)["task_description"] == "Phase 2 brief CLI"


# ── banking and the CLI ─────────────────────────────────────────────────────


def test_bank_writes_an_automatic_ring_entry_and_summary_line(tmp_path, monkeypatch):
    from windvane import draft as d

    sid = "aaaaaaaa-0000-4000-8000-0000000000d4"
    proj, ring, entry = _project_with_ring(tmp_path, sid)
    tp = _session_transcript(tmp_path, proj)
    _context(monkeypatch)
    _own_previous(monkeypatch, entry)
    monkeypatch.setattr(d, "work_project_of", lambda project_dir, state=None: project_dir)
    rec = d.draft(str(proj), sid, str(tp), {})
    banked = d.bank(rec, sid, trigger="compact_now")
    assert (banked["kind"], banked["trigger"], banked["session_id"]) == ("auto", "compact_now", sid)
    assert banked["next_steps"] == banked["pending_steps"] == ["wire the CLI", "smoke test"]
    latest = json.loads((ring / "latest_handoff.json").read_text(encoding="utf-8"))
    assert latest["created"] == banked["created"] and latest["summary"] == SUMMARY
    line = d.summary_line(banked)
    assert line.startswith("Banked the drafted checkpoint (compact_now): Phase 2 brief CLI") and "2 pending" in line


def test_the_cli_prints_the_draft_as_json(tmp_path, monkeypatch, capsys):
    from windvane import draft as d

    sid = "aaaaaaaa-0000-4000-8000-0000000000d5"
    proj, _ring, entry = _project_with_ring(tmp_path, sid)
    tp = _session_transcript(tmp_path, proj)
    _context(monkeypatch)
    _own_previous(monkeypatch, entry)
    assert d.main(["--project", str(proj), "--session", sid, "--transcript", str(tp), "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["pending_steps"] == ["wire the CLI", "smoke test"] and got["handoff_summary"] == SUMMARY


def test_the_hooks_own_session_pick_feeds_the_draft(tmp_path, monkeypatch):
    """End to end with the hooks port's ``_own_session_checkpoint``."""
    pytest.importorskip("windvane.hooks.common", reason="_own_session_checkpoint is the hooks port's")
    from windvane import draft as d

    sid = "aaaaaaaa-0000-4000-8000-0000000000e1"
    proj, _ring, _entry = _project_with_ring(tmp_path, sid)
    tp = _session_transcript(tmp_path, proj)
    _context(monkeypatch)
    rec = d.draft(str(proj), sid, str(tp), {})
    assert rec["task_description"] == "Phase 2 brief CLI"
    assert rec["metadata"]["draft_sources"]["task_description"] == "previous checkpoint"

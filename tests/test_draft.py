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
    # The draft points the hook modules at the session under test
    # (common._session_id); put the value back so another file's tests do
    # not run as this session.
    from windvane.events import common

    monkeypatch.setattr(common, "_session_id", getattr(common, "_session_id", ""), raising=False)
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
    assert rec["completed_steps"] == ["agents hook", "commit: draft: the recorder fills the record", "the compact hook",
                                      "tests: 2 runs, last passed", "Phase 3 draft built"]
    assert rec["pending_steps"] == ["wire the CLI", "smoke test"]
    assert rec["files_involved"] == [str(proj / "src" / "draft.py"), str(proj / "src" / "cli.py")]
    assert rec["handoff_summary"] == SUMMARY and rec["current_step"] == SUMMARY
    assert rec["handoff_warnings"] == ["never push"] and rec["handoff_context_needed"] == ["the mod design"]
    assert rec["key_decisions"] == ["use the ring vocabulary"]
    assert rec["metadata"]["draft_sources"] == {
        "task_description": "previous checkpoint",
        "current_step": "closing reply",
        "completed_steps": "previous checkpoint+task list+commits+hook state",
        "pending_steps": "task list+previous checkpoint",
        "files_involved": "transcript edits",
        "key_decisions": "hook state",
        "handoff_summary": "closing reply",
        "handoff_context_needed": "previous checkpoint",
        "handoff_warnings": "previous checkpoint",
    }


def test_a_message_relayed_by_another_agent_is_never_the_first_prompt(tmp_path):
    from windvane import draft as d

    tp = tmp_path / "relay.jsonl"
    recs = [
        _rec("u0", None, "user", 'Another Claude session sent a message:\n<teammate-message teammate_id="port-hooks">seam names</teammate-message>'),
        _rec("a0", "u0", "assistant", "Noted."),
        _rec("u1", "a0", "user", "Port the hooks to the new engine"),
    ]
    tp.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    assert d.read_transcript(str(tp))["first_prompt"] == "Port the hooks to the new engine"


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


def test_a_compact_now_bank_is_a_deliberate_ring_entry_with_a_summary_line(tmp_path, monkeypatch):
    from windvane import checkpoints as ck
    from windvane import draft as d

    sid = "aaaaaaaa-0000-4000-8000-0000000000d4"
    proj, ring, entry = _project_with_ring(tmp_path, sid)
    tp = _session_transcript(tmp_path, proj)
    _context(monkeypatch)
    _own_previous(monkeypatch, entry)
    monkeypatch.setattr(d, "work_project_of", lambda project_dir, state=None: project_dir)
    rec = d.draft(str(proj), sid, str(tp), {})
    banked = d.bank(rec, sid, trigger="compact_now")
    assert (banked["kind"], banked["trigger"], banked["session_id"]) == ("manual", "compact_now", sid)
    assert banked["next_steps"] == banked["pending_steps"] == ["wire the CLI", "smoke test"]
    latest = json.loads((ring / "latest_handoff.json").read_text(encoding="utf-8"))
    assert latest["created"] == banked["created"] and latest["summary"] == SUMMARY
    # Deliberate: in the history too, newest, as the compaction banner's
    # own-session pick reads it.
    assert ck.read_history([ring])[0]["created"] == banked["created"]
    line = d.summary_line(banked)
    assert line.startswith("Banked the drafted checkpoint (compact_now): Phase 2 brief CLI") and "2 pending" in line


def test_the_hooks_bank_stays_an_automatic_entry(tmp_path, monkeypatch):
    from windvane import checkpoints as ck
    from windvane import draft as d

    sid = "aaaaaaaa-0000-4000-8000-0000000000d5"
    proj, ring, entry = _project_with_ring(tmp_path, sid)
    tp = _session_transcript(tmp_path, proj)
    _context(monkeypatch)
    _own_previous(monkeypatch, entry)
    monkeypatch.setattr(d, "work_project_of", lambda project_dir, state=None: project_dir)
    banked = d.bank(d.draft(str(proj), sid, str(tp), {}), sid)
    assert (banked["kind"], banked["trigger"]) == ("auto", "bank")
    # The pointer moved (the history's manual entry is older), the history did not grow.
    history = ck.read_history([ring])
    assert [h["kind"] for h in history if h["created"] == banked["created"]] == ["auto"]
    assert sum(1 for h in history if h["kind"] == "manual") == 1


def test_the_refresh_keeps_what_the_model_wrote_and_takes_the_draft_for_the_rest():
    from windvane import draft as d

    saved = {"task_id": "task_7", "kind": "manual", "created": 1000.0, "session_id": "s",
             "task_description": "Cursor pagination for GET /items",
             "handoff_summary": "Cursor paging is only planned.", "summary": "Cursor paging is only planned.",
             "completed_steps": [], "next_steps": ["Keep page= working"], "files_in_progress": [],
             "warnings": ["never push"], "context_needed": [], "decisions": [],
             "metadata": {"project_path": "/p", "drafted_fields": ["handoff_summary"]}}
    record = {"task_description": "We are adding cursor pagination. Read docs/API.md",
              "current_step": "the edit", "handoff_summary": "Added cursor paging to api.py; page= still works.",
              "completed_steps": ["Edit items_api/api.py"], "pending_steps": ["tests for the cursor"],
              "files_involved": ["/p/items_api/api.py", "/p/items_api/API.PY"], "key_decisions": [],
              "handoff_warnings": ["nothing was run"], "handoff_context_needed": []}
    out, fields = d.merge_into_deliberate(saved, record)
    # The model's own words stay: the task, and the pending steps it typed.
    assert out["task_description"] == saved["task_description"]
    assert out["pending_steps"] == out["next_steps"] == ["Keep page= working"]
    # A drafted or empty field takes the fresh draft; a list the model wrote gains the new items.
    assert out["handoff_summary"] == out["summary"] == "Added cursor paging to api.py; page= still works."
    assert out["current_step"] == "the edit"
    assert out["files_involved"] == out["files_in_progress"] == ["/p/items_api/api.py"]
    assert out["completed_steps"] == ["Edit items_api/api.py"]
    assert out["handoff_warnings"] == out["warnings"] == ["never push", "nothing was run"]
    assert sorted(fields) == ["completed_steps", "current_step", "files_involved", "handoff_summary", "handoff_warnings"]
    assert (out["kind"], out["task_id"], out["created"]) == ("manual", "task_7", 1000.0)
    md = out["metadata"]
    assert md["drafted_fields"] == ["completed_steps", "current_step", "files_involved", "handoff_summary"]
    assert md["refreshed"]["fields"] == fields and md["project_path"] == "/p"
    # The same draft again changes nothing.
    assert d.merge_into_deliberate(out, record) == (out, [])


def test_the_refresh_rewrites_the_session_record_in_place_once_the_session_edited_past_the_save(tmp_path, monkeypatch):
    from windvane import checkpoints as ck
    from windvane import draft as d

    sid = "aaaaaaaa-0000-4000-8000-0000000000d7"
    proj, ring, entry = _project_with_ring(tmp_path, sid)
    entry["metadata"] = {"drafted_fields": ["handoff_summary"]}
    (ring / "handoff_history.json").write_text(json.dumps({"handoffs": [entry]}), encoding="utf-8")
    (ring / "latest_handoff.json").write_text(json.dumps(entry), encoding="utf-8")
    task_file = ck.global_ring_dir() / "task_9.json"
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text(json.dumps({"task_id": "task_9", "files_involved": [], "handoff_summary": "wire the brief"}), encoding="utf-8")
    monkeypatch.setattr(d, "work_project_of", lambda project_dir, state=None: project_dir)
    monkeypatch.setattr(d, "_record_dirs", lambda project_dir, work_project: [ring])
    hooks = {"_own_session_checkpoint": lambda dirs, s, tp: (entry if s == sid else None, [])}
    monkeypatch.setattr(d, "_hook", lambda name: hooks[name])
    record = {"task_description": "Phase 2 brief CLI", "current_step": "", "completed_steps": ["agents hook", "the compact hook"],
              "pending_steps": ["smoke test"], "files_involved": ["src/loader.py", "src/compact.py"], "key_decisions": [],
              "handoff_summary": "The compact hook is in; next the smoke test.", "handoff_warnings": [], "handoff_context_needed": []}

    # No save mark, or no edit since the save: nothing happens.
    assert d.refresh_deliberate(record, str(proj), sid, "", {"edits_total": 3}) is None
    state = {"edits_total": 3, "pressure": {"edits_at_manual_checkpoint": 3}}
    assert d.refresh_deliberate(record, str(proj), sid, "", state) is None

    state["edits_total"] = 5
    out = d.refresh_deliberate(record, str(proj), sid, "", state)
    assert out is not None and out["task_id"] == "task_9" and out["kind"] == "manual"
    assert out["files_in_progress"] == ["src/loader.py", "src/compact.py"]
    assert out["completed_steps"] == ["agents hook", "the compact hook"]
    assert out["next_steps"] == ["the compact hook", "smoke test"]  # the model's own list stays
    assert out["summary"] == "The compact hook is in; next the smoke test."
    # In place: one history entry still, the pointer follows, the task file too.
    history = json.loads((ring / "handoff_history.json").read_text(encoding="utf-8"))["handoffs"]
    assert len(history) == 1 and history[0]["files_in_progress"] == out["files_in_progress"]
    assert json.loads((ring / "latest_handoff.json").read_text(encoding="utf-8"))["summary"] == out["summary"]
    assert json.loads(task_file.read_text(encoding="utf-8"))["files_involved"] == out["files_in_progress"]
    # The mark moved to the current count, so the same state refreshes nothing more.
    assert state["pressure"]["edits_at_manual_checkpoint"] == 5
    assert d.refresh_deliberate(record, str(proj), sid, "", state) is None
    # The turn that saved the record ending: refreshed whatever the edit count.
    record["handoff_summary"] = "The smoke test is in."
    entry["metadata"] = {"drafted_fields": ["handoff_summary"]}
    out = d.refresh_deliberate(record, str(proj), sid, "", state, turn_saved=True)
    assert out is not None and out["summary"] == "The smoke test is in."


def test_a_bank_for_a_project_the_store_has_not_met_registers_its_ring(tmp_path, monkeypatch):
    from windvane import checkpoints as ck
    from windvane import draft as d
    from windvane.store import MemoryStore

    sid = "aaaaaaaa-0000-4000-8000-0000000000d6"
    proj, ring, entry = _project_with_ring(tmp_path, sid)
    fresh = tmp_path / "fresh"
    (fresh / "src").mkdir(parents=True)
    (fresh / "pyproject.toml").write_bytes(b"[project]\nname = 'fresh'\n")
    assert ck.project_ring_dir(str(fresh)) is None
    tp = _session_transcript(tmp_path, proj)
    _context(monkeypatch)
    _own_previous(monkeypatch, entry)
    monkeypatch.setattr(d, "work_project_of", lambda project_dir, state=None: project_dir)
    rec = d.draft(str(proj), sid, str(tp), {})
    rec["metadata"]["project_path"] = str(fresh)
    banked = d.bank(rec, sid, trigger="compact_now")
    fresh_ring = MemoryStore()._project_dir(MemoryStore()._normalize_path(str(fresh)))
    assert ck.project_ring_dir(str(fresh)) == fresh_ring
    latest = json.loads((fresh_ring / "latest_handoff.json").read_text(encoding="utf-8"))
    assert latest["created"] == banked["created"]


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
    pytest.importorskip("windvane.events.common", reason="_own_session_checkpoint is the hooks port's")
    from windvane import draft as d

    sid = "aaaaaaaa-0000-4000-8000-0000000000e1"
    proj, _ring, _entry = _project_with_ring(tmp_path, sid)
    tp = _session_transcript(tmp_path, proj)
    _context(monkeypatch)
    rec = d.draft(str(proj), sid, str(tp), {})
    assert rec["task_description"] == "Phase 2 brief CLI"
    assert rec["metadata"]["draft_sources"]["task_description"] == "previous checkpoint"

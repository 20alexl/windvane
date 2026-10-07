"""The plugin tools: one dispatcher, a JSON object in and one out."""

import importlib.util
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
    store = tmp_path / "store"
    monkeypatch.setenv("WINDVANE_DIR", str(store))
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    return store


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    (p / "pyproject.toml").write_bytes(b"[project]\nname = 'proj'\n")
    return p


@pytest.fixture
def warm():
    from windvane.tools import Warm

    w = Warm()
    yield w
    w.close()


def call(warm, tool, proj=None, **arguments):
    from windvane.tools import run

    if proj is not None:
        arguments.setdefault("project_path", str(proj))
    out = run({"tool": tool, "arguments": arguments}, warm)
    assert set(out) == {"text", "isError", "ms"} and isinstance(out["ms"], int)
    return out


# ── errors ──────────────────────────────────────────────────────────────────


def test_bad_requests_answer_is_error(warm):
    from windvane.tools import run

    assert run({"tool": "nope", "arguments": {}})["isError"] is True
    assert "Unknown plugin tool" in run({"tool": "nope"})["text"]
    assert run({"tool": "memory", "arguments": "not an object"})["isError"] is True
    bad_op = run({"tool": "memory", "arguments": {"operation": "consolidate"}}, warm)
    assert bad_op["isError"] is True and "one of remember" in bad_op["text"]
    assert run("not a dict")["isError"] is True  # type: ignore[arg-type]


# ── checkpoint ──────────────────────────────────────────────────────────────


def test_checkpoint_save_list_restore(warm, proj):
    saved = call(warm, "checkpoint", proj, operation="save", task_description="Port the tools",
                 pending_steps=["spike"], handoff_summary="Port the tools: the spike is in.")
    assert saved["isError"] is False and "task_id: task_" in saved["text"]
    listed = call(warm, "checkpoint", proj, operation="list")
    assert "Port the tools: the spike is in." in listed["text"]
    restored = call(warm, "checkpoint", proj, operation="restore")
    assert "**Task:** Port the tools" in restored["text"] and "Port the tools: the spike is in." in restored["text"]
    assert "No checkpoint at index 7" in call(warm, "checkpoint", proj, operation="restore", index=7)["text"]


def test_checkpoint_save_answers_with_the_fill_and_the_choice(tmp_path, monkeypatch, warm, proj):
    """The save's reply carries the fill as a percent of the compaction point
    and, past the early mark, the question the save raises. Without a
    reading the reply says nothing about the context."""
    from windvane import pressure as cp

    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "750000")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    sid = "aaaaaaaa-0000-4000-8000-00000000000c"
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    plain = call(warm, "checkpoint", proj, operation="save", task_description="Port the tools")
    assert plain["isError"] is False and "Context:" not in plain["text"]

    rec = {"session_id": sid, "ts": time.time(), "source": "mod", "total_input_tokens": 440_000,
           "context_window_size": 1_000_000, "early_band": "45%"}
    p = cp.mirror_path(sid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(json.dumps(rec).encode("utf-8"))
    saved = call(warm, "checkpoint", proj, operation="save", task_description="Port the tools")
    assert saved["isError"] is False and "task_id: task_" in saved["text"]
    assert "Context: 59% of the 750K compaction point (heads-up at 87%, last call at 93%, auto-compaction at 96%)." in saved["text"]
    assert "This save closed a step: call compact_now now and end the turn" in saved["text"]
    assert "Carry on only if the next step needs something only this context holds" in saved["text"]
    # The follow-through is staged in the session's own state for the next turn.
    from windvane import draft

    _sid, state, _tp = draft.session_inputs(sid)
    assert state["pressure"]["compact_pending"]["fill_pct"] == "59%"


def _session_with_a_draft(tmp_path, monkeypatch, proj, sid):
    """A session the recorder can draft from: a previous own checkpoint and a
    transcript with a typed prompt, an edit and a closing reply."""
    from windvane import draft
    from windvane.store import MemoryStore

    store = MemoryStore()
    store.remember_project(str(proj))
    prev = {"task_id": "task_1", "kind": "manual", "created": time.time() - 60, "session_id": sid,
            "task_description": "Phase 2 brief CLI", "summary": "wire the brief",
            "next_steps": ["smoke test"], "warnings": ["never push"], "project_path": str(proj)}
    recs = [
        {"uuid": "u1", "parentUuid": None, "type": "user", "timestamp": "2026-10-01T00:00:00Z",
         "message": {"role": "user", "content": "Build the brief"}},
        {"uuid": "a1", "parentUuid": "u1", "type": "assistant", "timestamp": "2026-10-01T00:00:01Z",
         "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "Edit",
                                                       "input": {"file_path": str(proj / "brief.py")}}]}},
        {"uuid": "u2", "parentUuid": "a1", "type": "user", "timestamp": "2026-10-01T00:00:02Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "ok"}]}},
        {"uuid": "a2", "parentUuid": "u2", "type": "assistant", "timestamp": "2026-10-01T00:00:03Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "Next: the smoke test, then the docs."}]}},
    ]
    tp = tmp_path / "t.jsonl"
    tp.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    monkeypatch.setattr(draft, "session_inputs", lambda session_id="": (sid, {}, str(tp)))
    monkeypatch.setattr(draft, "_previous", lambda *a, **k: (prev, "previous checkpoint"))
    monkeypatch.setattr(draft, "_session_context", lambda wp: {"decisions": ["use the ring vocabulary"]})
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    return store


def test_a_bare_save_accepts_the_draft_and_one_field_amends_it(tmp_path, monkeypatch, warm, proj):
    from windvane import checkpoints as ck

    sid = "aaaaaaaa-0000-4000-8000-0000000000f1"
    store = _session_with_a_draft(tmp_path, monkeypatch, proj, sid)
    ring = store._project_dir(store._normalize_path(str(proj)))
    out = call(warm, "checkpoint", None, operation="save", project_path=str(proj))
    assert out["isError"] is False
    assert "Drafted by the recorder: task_description, current_step, pending_steps, files_involved, " \
           "key_decisions, handoff_summary, handoff_warnings" in out["text"], out["text"]
    saved = [e for e in ck.read_history([ring]) if e.get("kind") == "manual" and e.get("session_id") == sid][0]
    assert saved["task_description"] == "Phase 2 brief CLI" and saved["summary"] == "Next: the smoke test, then the docs."
    assert saved["next_steps"] == ["smoke test"] and saved["warnings"] == ["never push"]
    assert saved["files_in_progress"] == [str(proj / "brief.py")] and saved["decisions"] == ["use the ring vocabulary"]
    assert saved["metadata"]["drafted_fields"][0] == "task_description"

    time.sleep(0.01)
    out = call(warm, "checkpoint", None, operation="save", project_path=str(proj), handoff_summary="my own note")
    assert "handoff_summary" not in out["text"].split("Drafted by the recorder:")[1]
    newest = [e for e in ck.read_history([ring]) if e.get("kind") == "manual" and e.get("session_id") == sid][0]
    assert newest["summary"] == "my own note" and newest["next_steps"] == ["smoke test"]


def test_compact_now_banks_the_draft(tmp_path, monkeypatch, warm, proj):
    sid = "aaaaaaaa-0000-4000-8000-0000000000f2"
    store = _session_with_a_draft(tmp_path, monkeypatch, proj, sid)
    out = call(warm, "compact_now", proj)
    assert out["isError"] is False and out["text"].startswith("Banked the drafted checkpoint (compact_now): Phase 2 brief CLI")
    latest = json.loads((store._project_dir(store._normalize_path(str(proj))) / "latest_handoff.json").read_text(encoding="utf-8"))
    assert (latest["kind"], latest["trigger"], latest["session_id"]) == ("manual", "compact_now", sid)


# ── memory ──────────────────────────────────────────────────────────────────


def test_memory_operations(warm, proj):
    out = call(warm, "memory", proj, operation="remember", content="The cache is sqlite, not redis.")
    assert out["isError"] is False and "Remembered: The cache is sqlite" in out["text"]
    call(warm, "memory", proj, operation="remember", content="DECISION: keep the ring vocabulary", category="decision")
    assert "sqlite" in call(warm, "memory", proj, operation="recall")["text"]
    hits = call(warm, "memory", proj, operation="search", query="sqlite cache")["text"]
    assert hits.startswith("Found 1 memories") and "The cache is sqlite" in hits
    assert "needs_clarification" in call(warm, "memory", proj, operation="search")["text"]

    mid = warm.store.search_memories(str(proj), query="cache")[0].id
    assert "Modified: relevance" in call(warm, "memory", proj, operation="modify", memory_id=mid, relevance=7)["text"]
    assert f"Promoted {mid} to rule" in call(warm, "memory", proj, operation="promote", memory_id=mid)["text"]
    rules = call(warm, "memory", proj, operation="list_rules")["text"]
    assert rules.startswith(f"Rules for {warm.store._normalize_path(str(proj))}: 1 own") or "1 own" in rules
    added = call(warm, "memory", proj, operation="add_rule", content="Never push without the word", reason="pushes are public")
    assert "Rule added with id=" in added["text"]
    cut = call(warm, "memory", proj, operation="list_rules", limit=1)["text"]
    assert "... and 1 more" in cut and cut.count("\n  [") == 1
    assert "Deleted memory" in call(warm, "memory", proj, operation="delete", memory_id=mid)["text"]

    warm.store.remember_discovery(str(proj), "MISTAKE: dropped the index in db.py", category="mistake", relevance=9, auto_embed=False)
    listed = call(warm, "memory", proj, operation="list_mistakes")["text"]
    assert listed.startswith("Tracked mistakes (1):")
    mistake = warm.store.get_recent_memories(str(proj), category="mistake")[0].id
    ack = call(warm, "memory", proj, operation="acknowledge_mistake", memory_id=mistake)["text"]
    assert "acknowledged and archived" in ack
    assert call(warm, "memory", proj, operation="list_mistakes")["text"].startswith("No mistakes tracked for this project.")
    assert "Restored memory" in call(warm, "memory", proj, operation="restore", memory_id=mistake)["text"]
    assert "Would archive 0 memories" in call(warm, "memory", proj, operation="archive")["text"]
    assert "Forgot all memories" in call(warm, "memory", proj, operation="forget")["text"]
    assert warm.store.get_project(str(proj)) is None


def test_memory_recent_lists_the_newest_first(warm, proj):
    assert call(warm, "memory", proj, operation="recent")["text"] == "No recent memories"
    for text in ("first: the cache is sqlite", "second: the parser drops the BOM", "third: " + "x" * 80):
        warm.store.remember_discovery(str(proj), text, auto_embed=False)
        time.sleep(0.01)
    out = call(warm, "memory", proj, operation="recent", limit=2)["text"].splitlines()
    assert out[0] == "Recent memories (newest first):" and len(out) == 4
    assert "(0m ago) [discovery] third: xxx" in out[2] and out[2].endswith("...")
    assert "second: the parser drops the BOM" in out[3]
    mistakes = call(warm, "memory", proj, operation="recent", category="mistake")["text"]
    assert mistakes == "No recent memories"


def test_memory_archive_search_finds_what_aged_out(warm, proj):
    assert call(warm, "memory", proj, operation="archive_search", query="redis")["text"] == "No archived memories found"
    warm.store.remember_discovery(str(proj), "the old cache layout used redis keys", auto_embed=False)
    p = warm.store.get_project(str(proj))
    p.entries[0].last_accessed = time.time() - 40 * 86400
    warm.store._dirty_projects.add(p.project_path)
    warm.store._save()
    warm.store.archive_old_memories(str(proj), dry_run=False)
    out = call(warm, "memory", proj, operation="archive_search", query="redis cache")["text"]
    lines = out.splitlines()
    assert lines[0] == "Archived memories:" and "(0d archived) [discovery] the old cache layout used redis keys" in lines[2]
    assert "memory(restore, memory_id=" in out
    assert len([ln for ln in lines if "redis keys" in ln]) == 1  # each entry once


def test_mine_reindex_starts_the_miner_and_reports(warm, proj, monkeypatch):
    from windvane.mining import background

    out = call(warm, "mine", proj, operation="reindex")
    assert out["isError"] is False and "background processes are off" in out["text"]
    assert "Unknown reindex mode" in call(warm, "mine", proj, operation="reindex", mode="everything")["text"]

    started: list = []
    monkeypatch.setattr(background, "start_mining_background",
                        lambda project, mode="post_session", windvane_storage_dir="": started.append((project, mode)) or True)
    monkeypatch.setattr(background, "get_mining_status", lambda: {
        "status": "completed", "result": {"sessions": 3, "messages": 120, "extractions": 7, "embeddings": 40}})
    monkeypatch.setattr("windvane.tools.REINDEX_POLL_SECS", 0.01)
    out = call(warm, "mine", proj, operation="reindex", mode="bootstrap")["text"]
    assert out.splitlines() == ["Mining completed (mode=bootstrap):", "  Sessions indexed: 3", "  Messages: 120",
                                "  Extractions: 7 findings", "  Search chunks: 40"]
    call(warm, "mine", proj, operation="reindex")
    assert [m for _p, m in started] == ["bootstrap", "full"]  # incremental is the miner's full pass

    monkeypatch.setattr(background, "get_mining_status", lambda: {"status": "running", "phase": "embed"})
    monkeypatch.setattr("windvane.tools.REINDEX_WAIT_SECS", 0.05)
    assert "currently in 'embed' phase" in call(warm, "mine", proj, operation="reindex")["text"]
    monkeypatch.setattr(background, "start_mining_background", lambda *a, **k: False)
    monkeypatch.setattr(background, "is_mining_running", lambda: True)
    assert call(warm, "mine", proj, operation="reindex")["text"].startswith("Mining already running")


def test_set_detector_validates_before_storing(warm, proj):
    pytest.importorskip("windvane.compliance", reason="detector validation is the hooks port's compliance module")
    call(warm, "memory", proj, operation="add_rule", content="Never push without the word")
    rid = warm.store.get_rules(str(proj))[0].id
    bad = call(warm, "memory", proj, operation="set_detector", memory_id=rid, detector={"command": "("})
    assert "Detector rejected" in bad["text"]
    good = call(warm, "memory", proj, operation="set_detector", memory_id=rid, detector={"tools": ["Bash"], "command": r"\bgit push\b"})
    assert f"Detector set on rule {rid}" in good["text"]


def test_with_no_session_the_project_is_the_working_directory(warm, proj, monkeypatch):
    monkeypatch.chdir(proj)
    call(warm, "memory", None, operation="remember", content="remembered from the cwd")
    assert warm.store.search_memories(str(proj), query="cwd")


def test_with_no_session_the_sessions_directory_wins_over_the_processs_cwd(tmp_path, warm, proj, monkeypatch):
    """The daemon runs in the engine folder; the mod sends the session's
    working directory as CLAUDE_PROJECT_DIR, and a call naming no project
    files there, never under the process's own cwd."""
    engine_folder = tmp_path / "engine-folder"
    engine_folder.mkdir()
    monkeypatch.chdir(engine_folder)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    out = call(warm, "memory", None, operation="remember", content="filed under the session's directory")
    assert out["isError"] is False
    assert warm.store.search_memories(str(proj), query="directory")
    assert warm.store.get_project(str(engine_folder)) is None


def test_a_session_directory_inside_the_project_files_the_save_under_the_project(tmp_path, warm, proj, monkeypatch):
    """A shell that moved into a subfolder (the repository's own engine
    folder, 2026-10-05) is still in the project: the save is filed under the
    repository and the subfolder is never registered as a project."""
    sub = proj / "engine"
    sub.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(sub))
    out = call(warm, "checkpoint", None, operation="save", task_description="filed under the repository")
    assert out["isError"] is False and "Filed under proj" in out["text"]
    assert warm.store.get_project(str(sub)) is None


def test_with_no_project_named_by_the_hooks_the_drafts_work_project_wins_over_the_cwd(tmp_path, warm, proj, monkeypatch):
    """The session's state names no project, so session_project answers the
    cwd's repository: that is a fallback, not a hit, and the draft's work
    project comes next (its own pick stands in here; only the draft branch
    is under test). The entry files there, never under the cwd."""
    from windvane import draft

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(draft, "session_inputs", lambda session_id="": ("sid-1", {}, ""))
    monkeypatch.setattr(draft, "work_project_of", lambda project_dir, state=None: str(proj))
    call(warm, "memory", None, operation="remember", content="filed under the session's project")
    assert warm.store.search_memories(str(proj), query="filed")
    assert warm.store.get_project(str(elsewhere)) is None


def test_with_no_project_path_the_sessions_work_project_wins_over_the_cwd(tmp_path, _store, warm, proj, monkeypatch):
    """A session started at a workspace root that edits a sub-project: a call
    naming no project files under the sub-project the session's own edits
    name (the hooks' session_project, read from the session's state), never
    under the workspace root the process runs in."""
    pytest.importorskip("windvane.events.common", reason="session_project is the hooks port's")
    sid = "aaaaaaaa-0000-4000-8000-0000000000f9"
    (_store / "sessions").mkdir(parents=True, exist_ok=True)
    (_store / "sessions" / f"{sid}.json").write_text(
        json.dumps({"files_edited_this_session": [str(proj / "src" / "cache.py")]}), encoding="utf-8"
    )
    workspace = tmp_path  # proj sits under it
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    out = call(warm, "memory", None, operation="remember", content="filed under the session's project")
    assert out["isError"] is False
    assert warm.store.search_memories(str(proj), query="filed")
    assert warm.store.get_project(str(workspace)) is None


# ── log ─────────────────────────────────────────────────────────────────────


def test_log_mistake_and_decision(warm, proj):
    out = call(warm, "log", proj, operation="mistake", description="dropped the index", file_path="db.py", how_to_avoid="add it back")
    assert "Logged mistake: dropped the index" in out["text"]
    m = warm.store.get_recent_memories(str(proj), category="mistake")[0]
    assert m.content == "MISTAKE: dropped the index - Fix: add it back" and m.relevance == 9 and "db.py" in m.related_files
    out = call(warm, "log", proj, operation="decision", decision="sqlite for the cache", reason="one file", alternatives=["redis"])
    assert "Logged decision: sqlite for the cache" in out["text"]
    d = warm.store.get_recent_memories(str(proj), category="decision")[0]
    assert d.content == "DECISION: sqlite for the cache - Reason: one file (Alternatives: redis)"
    assert "needs_clarification" in call(warm, "log", proj, operation="decision", decision="x")["text"]


def test_a_mistake_logged_with_no_project_is_not_reported_as_saved():
    from windvane.log import WorkTracker

    class _Memory:
        def __init__(self):
            self.calls = []

        def remember_discovery(self, *a, **kw):
            self.calls.append((a, kw))

    mem = _Memory()
    tracker = WorkTracker(mem)
    assert tracker.log_mistake("dropped the index", "db.py", "add it back") is False and mem.calls == []
    tracker.start_session("/ws/proj")
    assert tracker.log_mistake("dropped the index", "db.py", "add it back") is True
    assert len(mem.calls) == 1 and mem.calls[0][0][0] == "/ws/proj"


# ── mine and deps ───────────────────────────────────────────────────────────


def test_mine_names_an_absent_module(warm, proj):
    for op, module in (("search", "windvane.mining.search"), ("run_status", "windvane.goal"), ("status", "windvane.mining.background")):
        out = call(warm, "mine", proj, operation=op, query="x")
        try:
            present = importlib.util.find_spec(module) is not None
        except ModuleNotFoundError:
            present = False
        if not present:
            assert out["isError"] is True and module in out["text"], (op, out)


def test_mine_reads_the_session_index_when_present(warm, proj):
    pytest.importorskip("windvane.mining.session_index")
    pytest.importorskip("windvane.mining.patterns")
    out = call(warm, "mine", proj, operation="struggles")
    assert out["isError"] is False and out["text"] in ("No session data found.", "No struggle patterns detected.") or out["text"].startswith("Struggle areas:")


def test_memory_archive_by_id_moves_one_entry_and_restore_brings_it_back(warm, proj):
    warm.store.remember_discovery(str(proj), "the cache layout uses redis keys", auto_embed=False)
    mid = warm.store.get_project(str(proj)).entries[0].id
    out = call(warm, "memory", proj, operation="archive", memory_id=mid)
    assert out["isError"] is False and f"Archived memory {mid}" in out["text"]
    assert warm.store.get_project(str(proj)).entries == []
    assert "redis keys" in call(warm, "memory", proj, operation="archive_search", query="redis")["text"]
    back = call(warm, "memory", proj, operation="restore", memory_id=mid)
    assert f"Restored memory {mid}" in back["text"]
    assert [e.id for e in warm.store.get_project(str(proj)).entries] == [mid]
    assert "not found" in call(warm, "memory", proj, operation="archive", memory_id="nope")["text"]


def test_memory_recall_lists_the_entries_one_per_line(warm, proj):
    assert call(warm, "memory", proj, operation="recall")["text"].startswith("No memory yet for")
    warm.store.remember_discovery(str(proj), "the cache layout uses redis keys", auto_embed=False)
    out = call(warm, "memory", proj, operation="recall")["text"]
    lines = out.splitlines()
    assert lines[0].startswith("Project memory for") and lines[0].endswith("1 entries")
    assert any(line.startswith("[") and "the cache layout uses redis keys" in line for line in lines[1:])
    assert "discoveries=" not in out


def test_deps_map_builds_the_projects_own_index_when_it_has_none(warm, proj):
    from windvane.code_index import index_dir_for

    (proj / "pkg").mkdir()
    (proj / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (proj / "pkg" / "base.py").write_text("class Base: pass\n", encoding="utf-8")
    (proj / "pkg" / "top.py").write_text("from .base import Base\n", encoding="utf-8")
    assert not (index_dir_for(str(proj)) / "code_index.json").exists()
    out = call(warm, "deps", proj, operation="map", symbol="Base")
    assert out["isError"] is False and "module: pkg.base" in out["text"]
    assert (index_dir_for(str(proj)) / "code_index.json").exists()


def test_deps_map_and_impact(warm, proj):
    from windvane.code_index import build_code_index, index_dir_for

    (proj / "pkg").mkdir()
    (proj / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (proj / "pkg" / "base.py").write_text("class Base: pass\n", encoding="utf-8")
    (proj / "pkg" / "top.py").write_text("from .base import Base\n", encoding="utf-8")
    build_code_index(str(proj), index_dir_for(str(proj)))
    out = call(warm, "deps", proj, operation="map", symbol="Base")
    assert out["isError"] is False and "module: pkg.base" in out["text"] and "imported by 1: pkg.top" in out["text"]
    imp = call(warm, "deps", proj, operation="impact", file_path=str(proj / "pkg" / "base.py"))
    assert "Risk level: medium" in imp["text"] and "pkg/top.py" in imp["text"]


# ── the subprocess entry point ──────────────────────────────────────────────


def test_the_cli_reads_one_object_and_prints_one_line(tmp_path, _store, proj):
    env = dict(os.environ, WINDVANE_DIR=str(_store), WINDVANE_NO_DAEMON="1", PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    env.pop("CLAUDE_CODE_SESSION_ID", None)

    def cli(payload: bytes):
        run = subprocess.run([sys.executable, "-m", "windvane.tools"], input=payload, capture_output=True, env=env, timeout=120)
        lines = run.stdout.decode().strip().splitlines()
        assert len(lines) == 1, (lines, run.stderr.decode(errors="replace"))
        return run.returncode, json.loads(lines[0])

    req = {"tool": "memory", "arguments": {"operation": "remember", "project_path": str(proj), "content": "The cache is sqlite — not redis."}}
    code, out = cli(json.dumps(req).encode("utf-8"))
    assert code == 0 and out["isError"] is False and "Remembered: The cache is sqlite" in out["text"]
    code, out = cli(json.dumps({"tool": "nope", "arguments": {}}).encode())
    assert code == 1 and out["isError"] is True
    code, out = cli(b"{not json")
    assert code == 1 and out["isError"] is True

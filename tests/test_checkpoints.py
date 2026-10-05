"""Checkpoints: the ring, the promotion guard, scope resolution, the ring
vocabulary, HANDOFF.md, provenance and task-file hygiene."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    store = tmp_path / "store"
    monkeypatch.setenv("WINDVANE_DIR", str(store))
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return store


def _must(value):
    assert value is not None
    return value


TWINS = [
    ("pending_steps", "next_steps"),
    ("files_involved", "files_in_progress"),
    ("handoff_warnings", "warnings"),
    ("handoff_context_needed", "context_needed"),
    ("key_decisions", "decisions"),
    ("timestamp", "created"),
]


# ── the ring: promotion guard, capped history, index access ─────────────────


def test_the_ring_keeps_deliberate_records_and_guards_the_pointer(tmp_path):
    from windvane import checkpoints as ck

    proj, glob = tmp_path / "proj", tmp_path / "global"
    r = ck.write_handoff({"kind": "auto", "summary": "Session stopped. 0 files edited.",
                          "next_steps": ["Review what was in progress"]}, [proj, glob])
    assert r["skipped"] and not (proj / ck.LATEST_FILENAME).exists()
    ck.write_handoff({"kind": "auto", "summary": "Edited 3 files", "files_in_progress": ["a.py", "b.py"],
                      "decisions": ["use X"]}, [proj, glob])
    assert (proj / ck.LATEST_FILENAME).exists()
    ck.write_handoff({"kind": "manual", "summary": "Block 1-4 research handoff", "next_steps": ["finish the split"],
                      "context_needed": ["docs/x.md"]}, [proj, glob])
    assert _must(ck.read_latest([proj, glob]))["kind"] == "manual"
    ck.write_handoff({"kind": "auto", "summary": "Session stopped. 0 files edited.",
                      "next_steps": ["Review what was in progress"]}, [proj, glob])
    assert _must(ck.read_latest([proj, glob]))["kind"] == "manual"  # a trivial auto never clobbers
    hist = ck.read_history([proj, glob])
    assert len(hist) == 2 and hist[0]["summary"].startswith("Block 1-4")
    assert _must(ck.get_by_index([proj, glob], 1))["summary"] == "Edited 3 files"
    cap = tmp_path / "cap"
    for i in range(30):
        ck.write_handoff({"kind": "manual", "summary": f"h{i}", "next_steps": [f"s{i}"]}, [cap], history_limit=20)
    assert len(ck._load_history(cap)) == 20


def test_restore_picks_the_newest_deliberate_record_in_scope(tmp_path):
    """A stale ancestor's manual must not win, and a routine auto must not
    bury a deliberate checkpoint."""
    from windvane import checkpoints as ck

    now = time.time()
    stale = {"summary": "ready for the old plan", "kind": "manual", "created": now - 368 * 3600}
    fresh = {"summary": "HONEST RESET", "kind": "manual", "created": now - 3600}
    mid = {"summary": "interim auto", "kind": "auto", "created": now - 50 * 3600}
    noise = {"summary": "Session stopped. 2 files edited", "kind": "auto", "created": now - 0.2 * 3600}

    def put(d, history, latest):
        d.mkdir(parents=True, exist_ok=True)
        (d / ck.HISTORY_FILENAME).write_text(json.dumps({"handoffs": history}))
        if latest is not None:
            (d / ck.LATEST_FILENAME).write_text(json.dumps(latest))

    near, far = tmp_path / "near", tmp_path / "far"
    put(near, [stale], stale)
    put(far, [mid, fresh, noise], fresh)
    dirs = [near, far]
    assert _must(ck.read_latest(dirs))["summary"] == "HONEST RESET"
    assert ck.read_ordered(dirs)[0]["summary"] == "HONEST RESET" == _must(ck.get_by_index(dirs, 0))["summary"]
    summaries = [h["summary"] for h in ck.read_ordered(dirs)]
    assert "ready for the old plan" in summaries and "Session stopped. 2 files edited" in summaries
    assert ck.read_latest([near], max_age_hours=48) is None
    autos = tmp_path / "autos"
    put(autos, [mid, noise], noise)
    assert _must(ck.read_latest([autos]))["summary"] == "Session stopped. 2 files edited"


# ── ContextGuard: the saved record ──────────────────────────────────────────


def _guard_with_ring(tmp_path, monkeypatch, ring_dir):
    from windvane import checkpoints as ck

    monkeypatch.setattr(ck, "project_ring_dir", lambda p: ring_dir if p else None)
    monkeypatch.setattr(ck, "global_ring_dir", lambda: tmp_path / "store" / "checkpoints")
    return ck.ContextGuard()


def _save(guard, **kw):
    return guard.save_checkpoint(
        task_description=kw.get("task_description", "Migrating auth to OAuth2"),
        current_step=kw.get("current_step", "Step 3: token refresh"),
        completed_steps=kw.get("completed_steps", ["Step 1", "Step 2"]),
        pending_steps=kw.get("pending_steps", ["Step 3", "Step 4"]),
        files_involved=kw.get("files_involved", ["auth.py", "oauth.py"]),
        key_decisions=kw.get("key_decisions", ["use authlib"]),
        project_path=kw.get("project_path", "/ws/proj"),
        handoff_summary=kw.get("handoff_summary"),
        handoff_context_needed=kw.get("handoff_context_needed", ["docs/oauth.md"]),
        handoff_warnings=kw.get("handoff_warnings", ["don't touch legacy auth"]),
    )


def test_a_first_save_registers_the_project_and_files_in_its_ring(tmp_path):
    """Unmocked: a project nothing was remembered about has no manifest row
    yet; its first checkpoint registers it and lands in its own ring, not in
    the global folder alone."""
    from windvane import checkpoints as ck

    proj = tmp_path / "ws" / "fresh"
    proj.mkdir(parents=True)
    assert ck.project_ring_dir(str(proj)) is None
    resp = ck.ContextGuard().save_checkpoint(
        task_description="First task", current_step="step 1", completed_steps=[], pending_steps=["step 2"],
        files_involved=["a.py"], project_path=str(proj),
    )
    assert "Filed under fresh." in resp.reasoning
    ring = ck.project_ring_dir(str(proj))
    assert ring is not None and (ring / ck.LATEST_FILENAME).exists()
    assert (ck.global_ring_dir() / ck.LATEST_FILENAME).exists()


def test_a_saved_record_carries_one_name_per_concept(tmp_path, monkeypatch):
    ring = tmp_path / "ring"
    guard = _guard_with_ring(tmp_path, monkeypatch, ring)
    resp = _save(guard, handoff_summary="OAuth2 60% done")
    text = resp.to_formatted_string()
    assert "task_id: task_" in text and "Filed under proj" in text and "2 steps done, 2 remaining" in text
    rec = json.loads((ring / "handoff_history.json").read_text(encoding="utf-8"))["handoffs"][-1]
    for old, new in TWINS:
        assert new in rec and old not in rec, (old, new)
    assert rec["task_description"] == "Migrating auth to OAuth2" and rec["summary"] == "OAuth2 60% done"
    assert rec["next_steps"] == ["Step 3", "Step 4"] and rec["decisions"] == ["use authlib"]
    assert rec["kind"] == "manual" and rec["project_path"] == "/ws/proj"
    # the task file keeps the checkpoint vocabulary
    task = json.loads((guard.storage_dir / f"{rec['task_id']}.json").read_text(encoding="utf-8"))
    assert task["pending_steps"] == ["Step 3", "Step 4"] and "next_steps" not in task
    # two saves in one second never share a task file
    second = _save(guard, task_description="another")
    assert second.data["task_id"] != rec["task_id"]


def test_a_collapsed_record_restores_like_a_dual_vocabulary_one(tmp_path, monkeypatch):
    from windvane import checkpoints as ck

    ring = tmp_path / "ring"
    guard = _guard_with_ring(tmp_path, monkeypatch, ring)
    _save(guard, handoff_summary="OAuth2 60% done")
    new_rec = json.loads((ring / "handoff_history.json").read_text(encoding="utf-8"))["handoffs"][-1]
    old_rec = dict(new_rec)
    for old, new in TWINS:
        old_rec[old] = new_rec.get(new)

    def restored(entry):
        monkeypatch.setattr(ck, "read_latest", lambda *a, **k: entry)
        return guard.restore_checkpoint(None, project_path="/ws/proj")

    new_out, old_out = restored(new_rec), restored(old_rec)
    assert new_out.reasoning == old_out.reasoning
    new_lines, old_lines = new_out.to_formatted_string().splitlines(), old_out.to_formatted_string().splitlines()
    assert set(new_lines) <= set(old_lines) and len(old_lines) > len(new_lines)


def test_a_handoff_shaped_record_restores_every_line(tmp_path, monkeypatch):
    from windvane import checkpoints as ck

    entry = {"kind": "manual", "created": time.time() - 60, "summary": "OAuth2 migration 60% done",
             "next_steps": ["refresh tokens", "add tests"], "decisions": ["use authlib", "drop legacy path"],
             "warnings": ["don't touch legacy auth"], "files_in_progress": ["auth.py"], "project_path": "/ws/proj"}
    monkeypatch.setattr(ck, "read_latest", lambda *a, **k: entry)
    out = ck.ContextGuard().restore_checkpoint(None, project_path="/ws/proj")
    text = out.to_formatted_string()
    assert text.startswith("**Handoff:** OAuth2 migration 60% done")
    assert "**Key decisions:** 2 recorded" in text and "don't touch legacy auth" in text
    assert "Remaining steps: refresh tokens, add tests" in text


def test_restore_by_index_addresses_the_ring_only(tmp_path):
    from windvane import checkpoints as ck

    out = ck.ContextGuard().restore_checkpoint(None, project_path=str(tmp_path / "nowhere"), index=3)
    assert out.status == "not_found" and "No checkpoint at index 3" in out.reasoning
    assert ck.ContextGuard().list_checkpoints(str(tmp_path / "nowhere")).status == "not_found"


# ── scope: own ring, descendants, ancestors; global only as fallback ────────


def test_a_root_restore_sees_a_descendants_newer_checkpoint_never_a_siblings(tmp_path):
    from windvane import checkpoints as ck
    from windvane.store import MemoryStore

    store = MemoryStore()
    ws, app, api, other = (tmp_path / "ws", tmp_path / "ws" / "app", tmp_path / "ws" / "app" / "api", tmp_path / "ws" / "other")
    for d in (ws, app, api, other):
        d.mkdir(parents=True, exist_ok=True)
        store.remember_project(str(d))
    root_dirs = ck.candidate_dirs(str(ws))
    api_dirs = ck.candidate_dirs(str(api))
    assert ck.project_ring_dir(str(api)) in root_dirs and ck.project_ring_dir(str(app)) in root_dirs
    assert ck.project_ring_dir(str(other)) not in api_dirs and ck.project_ring_dir(str(ws)) in api_dirs
    assert ck.global_ring_dir() not in root_dirs
    assert ck.candidate_dirs(str(tmp_path / "unregistered")) == [ck.global_ring_dir()]

    guard = ck.ContextGuard()
    _save(guard, task_description="stale root checkpoint", project_path=str(ws), handoff_summary="stale root")
    time.sleep(0.01)
    _save(guard, task_description="the real final checkpoint", project_path=str(api), handoff_summary="real final")
    text = guard.restore_checkpoint(None, project_path=str(ws)).to_formatted_string()
    assert "the real final checkpoint" in text and "Restored from api" in text
    other_latest = ck.read_latest(ck.candidate_dirs(str(other)))
    assert other_latest is None or other_latest.get("summary") != "real final"
    listing = guard.list_checkpoints(str(ws)).to_formatted_string()
    assert listing.splitlines()[0].startswith("2 checkpoint(s)") and "[0] (manual" in listing


def test_handoff_md_is_project_scoped(tmp_path, monkeypatch):
    from windvane import checkpoints as ck

    dirs = {"/ws/projA": tmp_path / "projects" / "aaaa1111", "/ws/projB": tmp_path / "projects" / "bbbb2222"}
    monkeypatch.setattr(ck, "project_ring_dir", lambda p: dirs.get(p))
    guard = ck.ContextGuard(storage_dir=tmp_path / "checkpoints")
    guard._write_handoff_md("Alpha work summary", ["do A1"], ["docs/a.md"], ["watch out A"], project_path="/ws/projA")
    a_md = dirs["/ws/projA"] / "HANDOFF.md"
    text = a_md.read_text(encoding="utf-8")
    assert "**Project:** /ws/projA" in text and "## Warnings" in text and "watch out A" in text
    guard._write_handoff_md("Beta work summary", ["do B1"], project_path="/ws/projB")
    assert "Alpha work summary" in a_md.read_text(encoding="utf-8")
    assert "Beta work summary" in (tmp_path / "checkpoints" / "HANDOFF.md").read_text(encoding="utf-8")
    written = guard._write_handoff_md("Orphan", ["x"], project_path="/ws/unknown")
    assert written == tmp_path / "checkpoints" / "HANDOFF.md" and "**Project:** /ws/unknown" in written.read_text(encoding="utf-8")


# ── provenance ──────────────────────────────────────────────────────────────


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)


def test_a_save_stamps_the_commit_and_a_restore_says_what_moved(tmp_path, monkeypatch):
    pytest.importorskip("windvane.repo_state")
    repo = tmp_path / "repo"
    repo.mkdir()
    if _git(repo, "init", "-q").returncode != 0:
        pytest.skip("git is not available")
    for k, v in (("user.email", "t@example.invalid"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(repo, "config", k, v)
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "first")
    from windvane import checkpoints as ck
    from windvane.store import MemoryStore

    MemoryStore().remember_project(str(repo))
    guard = ck.ContextGuard()
    resp = guard.save_checkpoint("Wire the parser", "parse", [], ["tests"], ["a.py"], project_path=str(repo))
    rec = json.loads((guard.storage_dir / f"{resp.data['task_id']}.json").read_text(encoding="utf-8"))
    head = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
    assert rec.get("commit") and head.startswith(rec["commit"][:7])
    (repo / "a.py").write_text("x = 2\n")
    _git(repo, "commit", "-qam", "second")
    text = guard.restore_checkpoint(None, project_path=str(repo)).to_formatted_string()
    assert "**Task:** Wire the parser" in text and "1 commit" in text.lower()
    assert f"HEAD {_git(repo, 'rev-parse', '--short', 'HEAD').stdout.strip()}, clean tree" in text
    # The working tree now: the first thing a resumed session loses.
    (repo / "a.py").write_text("x = 3\n")
    (repo / "b.py").write_text("y = 1\n")
    from windvane import repo_state

    assert repo_state.tree_text(repo_state.tree(str(repo))).endswith(", 1 modified, 1 untracked")
    assert repo_state.tree(str(tmp_path / "nowhere")) is None and repo_state.tree_text(None) == ""
    text = guard.restore_checkpoint(None, project_path=str(repo)).to_formatted_string()
    assert "1 modified, 1 untracked" in text


# ── hygiene ─────────────────────────────────────────────────────────────────


def test_old_unreferenced_task_files_are_pruned_and_ring_members_kept(tmp_path):
    from windvane import checkpoints as ck

    store = tmp_path / "s"
    c = store / "checkpoints"
    c.mkdir(parents=True)
    (store / "projects" / "h1").mkdir(parents=True)
    old = time.time() - 100 * 86400
    for name in ("task_1", "task_2", "task_3"):
        (c / f"{name}.json").write_text(json.dumps({"task_id": name}), encoding="utf-8")
    os.utime(c / "task_1.json", (old, old))
    os.utime(c / "task_2.json", (old, old))
    (store / "projects" / "h1" / "handoff_history.json").write_text(
        json.dumps({"handoffs": [{"task_id": "task_1", "kind": "manual", "created": old}]}), encoding="utf-8")
    removed = ck.prune_task_files(store, keep_days=ck.TASK_FILE_KEEP_DAYS)
    assert [p.name for p in removed] == ["task_2.json"]
    assert (c / "task_1.json").exists() and (c / "task_3.json").exists()
    assert ck.prune_task_files(store) == []


def test_the_guard_follows_the_configured_store(tmp_path, _store):
    from windvane.checkpoints import ContextGuard

    assert ContextGuard().storage_dir == _store / "checkpoints"


# ── this session's own checkpoint, skipping a rewound branch ────────────────


def _rec(uuid, parent, typ, content, **extra):
    return {"uuid": uuid, "parentUuid": parent, "type": typ, "isSidechain": False,
            "timestamp": "2026-09-26T00:00:00Z", "message": {"role": typ, "content": content}, **extra}


def _result(call, text):
    return [{"type": "tool_result", "tool_use_id": call, "content": [{"type": "text", "text": text}]}]


def _call(cid, op):
    return [{"type": "tool_use", "id": cid, "name": "mcp__windvane__checkpoint", "input": {"operation": op}}]


def _rewound(tmp_path):
    recs = [
        _rec("r1", None, "user", "let's use sqlite for the alias store"),
        _rec("r2", "r1", "assistant", [{"type": "text", "text": "Done; the store is sqlite now."}]),
        {"uuid": "s1", "parentUuid": "r2", "type": "system", "subtype": "turn_duration", "isSidechain": False},
        _rec("r3", "s1", "user", "let's use the registry for every alias lookup"),
        _rec("r4", "r3", "assistant", _call("c1", "save")),
        _rec("r5", "r4", "user", _result("c1", "Checkpoint saved.\ntask_id: task_1\n")),
        _rec("r6", "r5", "assistant", [{"type": "text", "text": "Saved."}]),
        _rec("r7", "s1", "user", "never resolve aliases outside the registry"),
        _rec("r8", "r7", "assistant", _call("c2", "save")),
        _rec("r9", "r8", "user", _result("c2", "Checkpoint saved.\ntask_id: task_2\n")),
        _rec("r12", "r9", "assistant", [{"type": "text", "text": "Restored."}]),
    ]
    p = tmp_path / "rewound.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    ring = tmp_path / "ring"
    ring.mkdir()
    now = time.time()
    entries = [
        {"task_id": "task_3", "kind": "manual", "created": now - 10, "session_id": "other", "summary": "another session's task", "task_description": "another session's task"},
        {"task_id": "task_1", "kind": "manual", "created": now - 20, "session_id": "mine", "summary": "rewound branch task", "task_description": "rewound branch task"},
        {"task_id": "task_2", "kind": "manual", "created": now - 30, "session_id": "mine", "summary": "own live task", "task_description": "own live task"},
    ]
    (ring / "handoff_history.json").write_text(json.dumps({"handoffs": entries}), encoding="utf-8")
    return p, ring


def test_restore_prefers_this_sessions_live_checkpoint(tmp_path, monkeypatch):
    common = pytest.importorskip("windvane.events.common", reason="_own_session_checkpoint is the hooks port's")
    tc = pytest.importorskip("windvane.transcript")
    from windvane import checkpoints as ck

    p, ring = _rewound(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "mine")
    monkeypatch.setattr(common, "_session_id", "mine", raising=False)
    monkeypatch.setattr(ck, "candidate_dirs", lambda project_dir="": [ring])
    monkeypatch.setattr(tc, "transcript_for_session", lambda sid: p if sid == "mine" else None)
    text = ck.ContextGuard(storage_dir=tmp_path / "ckpt").restore_checkpoint(None, project_path=str(tmp_path)).to_formatted_string()
    assert "own live task" in text and "another session's task" not in text
    assert "task_1" in text and "rewound" in text.lower()

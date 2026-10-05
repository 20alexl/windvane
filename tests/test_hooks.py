"""windvane.events: the hook events, end to end and piece by piece.

The events run in-process through ``windvane.events.dispatch`` (the daemon's
path) and, for the process-level cases, as ``python -m windvane.events``.
Every test runs against a temporary store and never starts a daemon.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

from windvane import events  # noqa: E402
from windvane.events import common  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path: Path):
    for k in list(os.environ):
        if k.startswith("WINDVANE_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", raising=False)
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    # A test must never start a daemon: it would outlive the temp store.
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    monkeypatch.setenv("WINDVANE_LIVE_MINE", "0")
    cfg = tmp_path / "claude-config"
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setattr(common, "_session_id", "")
    monkeypatch.setattr(common, "_stdin_cache", None)
    # The miner is a background process; a hook test never spawns one.
    try:
        from windvane.mining import background

        monkeypatch.setattr(background, "start_mining_background", lambda *a, **k: False)
        monkeypatch.setattr(background, "is_mining_running", lambda: True)
    except Exception:
        pass


def _run(event: str, payload: dict) -> dict:
    """Dispatch one event in-process; the parsed hook output ({} when silent)."""
    out = events.dispatch(event, json.dumps(payload)).strip()
    if not out:
        return {}
    return json.loads(out.splitlines()[-1])


def _ctx(out: dict) -> str:
    return str((out.get("hookSpecificOutput") or {}).get("additionalContext") or "")


def _state(store: Path, sid: str) -> dict:
    return json.loads((store / "sessions" / f"{sid}.json").read_text(encoding="utf-8"))


def _proj(tmp_path: Path, name: str = "proj") -> Path:
    p = tmp_path / name
    (p / ".git").mkdir(parents=True)
    (p / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# The dispatcher
# ---------------------------------------------------------------------------


def test_every_wire_event_has_a_handler():
    for event, (mod, fn) in events.EVENTS.items():
        assert callable(events._handler(event)), event
    for event in ("pre_edit_json", "post_edit_json", "bash_json", "prompt_json", "pre_read_json",
                  "tool_failure_json", "post_batch_json", "pre_bash_json", "pre_tool_json",
                  "post_milestone_json", "session_start_json", "stop_json", "pre_compact_json",
                  "post_compact_json", "stop_failure_json", "notification_json", "session_end_json"):
        assert event in events.EVENTS


def test_dispatch_resets_the_per_call_globals_and_an_unknown_event_is_silent():
    assert events.dispatch("no_such_event", '{"session_id": "s-x"}') == ""
    assert common._session_id == "" and common._stdin_cache is None
    events.dispatch("pre_tool_json", '{"session_id": "s-y", "tool_name": "Read"}')
    assert common._session_id == "" and common._stdin_cache is None


def test_hooks_json_registers_every_event_through_the_client():
    data = json.loads((REPO / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    assert data["modules"] == ["./register.ts"]
    seen = {}
    for event, matchers in data["hooks"].items():
        for m in matchers:
            for h in m["hooks"]:
                assert h["type"] == "command"
                cmd = h["command"]
                assert cmd.startswith('python -S "${CLAUDE_PLUGIN_ROOT}/windvane/daemon_client.py" ')
                wire = cmd.rsplit(" ", 1)[1]
                assert wire in events.EVENTS
                seen[(event, m["matcher"])] = (wire, h["timeout"])
    # Timeouts are seconds (Claude Code's unit): 3 on the per-tool hooks,
    # 5 on the turn-level ones, 10 where a cold client may start the daemon.
    assert seen == {
        ("UserPromptSubmit", ""): ("prompt_json", 5),
        ("PreToolUse", "Edit|Write"): ("pre_edit_json", 3),
        ("PreToolUse", "Read"): ("pre_read_json", 3),
        ("PreToolUse", "Bash|PowerShell"): ("pre_bash_json", 3),
        ("PreToolUse", ""): ("pre_tool_json", 3),
        ("Notification", ""): ("notification_json", 3),
        ("PostToolUse", "Bash|PowerShell"): ("bash_json", 3),
        ("PostToolUse", "Edit|Write"): ("post_edit_json", 3),
        ("PostToolUse", "ExitPlanMode|TaskUpdate"): ("post_milestone_json", 3),
        ("PostToolBatch", ""): ("post_batch_json", 3),
        ("StopFailure", ""): ("stop_failure_json", 5),
        ("PostToolUseFailure", ""): ("tool_failure_json", 3),
        ("PreCompact", ""): ("pre_compact_json", 10),
        ("PostCompact", ""): ("post_compact_json", 5),
        ("SessionStart", ""): ("session_start_json", 10),
        ("Stop", ""): ("stop_json", 5),
        ("SessionEnd", ""): ("session_end_json", 5),
    }


def test_a_hook_survives_a_working_directory_that_shadows_the_stdlib(tmp_path: Path):
    """A session cd'd into a vendored package holding an email.py: under
    `python -m` the cwd is first on sys.path and shadowed the stdlib."""
    cwd = tmp_path / "vendored"
    cwd.mkdir()
    (cwd / "email.py").write_text("from .presets import questions\n", encoding="utf-8")
    env = dict(os.environ, WINDVANE_DIR=str(tmp_path / "store"), WINDVANE_NO_DAEMON="1", WINDVANE_LIVE_MINE="0")
    env["PYTHONPATH"] = str(REPO)
    r = subprocess.run([sys.executable, "-m", "windvane.events", "post_compact_json"],
                       input='{"session_id": "s-shadow", "cwd": "%s"}' % str(cwd).replace("\\", "\\\\"),
                       capture_output=True, text=True, cwd=str(cwd), env=env, timeout=120, stdin=None)
    assert r.returncode == 0 and "Traceback" not in r.stderr, r.stderr[-800:]


def test_every_subprocess_in_the_hooks_detaches_stdin():
    """A child that inherits a stdio server's stdin stalls by the full timeout."""
    root = REPO / "windvane"
    files = list((root / "events").glob("*.py")) + [root / f"{n}.py" for n in (
        "pressure", "stall", "milestones", "compliance", "goal", "capture", "paths", "precheck",
        "hot_reader", "storage", "proc_lock", "config", "repo_state", "transcript", "alerts", "procs", "report")]
    offenders = []
    for p in files:
        src = p.read_text(encoding="utf-8")
        for m in re.finditer(r"\b(?:subprocess|_sp|sp)\.(run|Popen|check_output)\(", src):
            head = src[m.start(): m.start() + 900].split("\n\n", 1)[0]
            if "stdin" not in head and "**kwargs" not in head and "input=" not in head:
                offenders.append(f"{p.name}:{src[:m.start()].count(chr(10)) + 1}")
    assert offenders == [], offenders


def test_the_dropped_modules_left_no_code_path_in_the_hooks():
    src = "\n".join(p.read_text(encoding="utf-8") for p in (REPO / "windvane" / "events").glob("*.py"))
    for gone in ("rotation", "outcomes", "predictive", "commitments", "reflect", "scope_guard",
                 "get_scope_status", "take_pending_text", "search_spiral", "scout_search", "windvane.run "):
        assert gone not in src, gone
    for tag in re.findall(r"<(/?[a-z]+-[a-z-]+)", src):
        assert tag.lstrip("/").startswith("windvane-"), tag


# ---------------------------------------------------------------------------
# Session scoping and the state
# ---------------------------------------------------------------------------


def test_goal_bracket_resolves_from_the_sessions_edits_not_the_turns():
    # Every Stop moves files_edited_this_session into last_session_files and
    # clears it, so a turn with no edits must not send the bracket to the cwd.
    f = common._session_edit_files
    assert f({"files_edited_this_session": ["/w/p/a.py"]}) == ["/w/p/a.py"]
    assert f({"files_edited_this_session": [], "last_session_files": ["/w/p/b.py"]}) == ["/w/p/b.py"]
    abs_c = str(Path("/w/p/c.py").resolve())
    st = {"files_edited_this_session": [], "last_session_files": [], "loop": {"edit_counts": {abs_c: 3, "c.py": 3}}}
    assert f(st) == [abs_c]
    assert f({}) == []


def test_session_project_is_one_loader_for_every_hook(tmp_path: Path):
    ws = tmp_path / "ws"
    proj = ws / "proj-a"
    (proj / ".git").mkdir(parents=True)
    (ws / ".git").mkdir()
    t = tmp_path / "t.jsonl"

    def tu(name, fp):
        return json.dumps({"type": "assistant", "timestamp": "2026-09-11T10:00:00.000Z",
                           "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": name, "input": {"file_path": fp}}]}})
    outside = str(tmp_path / "home" / ".claude" / "memory.md")
    # The memory file outside the workspace is edited last and must not vote.
    t.write_text("\n".join([tu("Edit", str(proj / "a.py")), tu("Write", str(proj / "b.py")), tu("Edit", outside)]) + "\n", encoding="utf-8")
    norm = common._normalize_path
    st: dict = {"run": {"transcript_path": str(t)}}
    assert common.session_project(str(ws), st) == norm(str(proj))
    assert st["session_project_cache"]["value"] == norm(str(proj))
    # Cached: a changed transcript path with the same size is not re-read.
    st["run"]["transcript_path"] = str(tmp_path / "missing.jsonl")
    assert common.session_project(str(ws), st) == norm(str(proj))
    # No transcript, no state lists: the cwd mapped to its repository.
    assert common.session_project(str(proj), {}) == norm(str(proj))
    # Files outside the root never vote for the root.
    assert common._resolve_session_project(str(ws), [outside, str(proj / "a.py")]) == norm(str(proj))


def test_session_stays_active_across_sub_projects(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(common, "_session_id", "s-active-test")
    common.mark_session_started(str(tmp_path / "ws" / "app"))
    assert common.check_session_active(str(tmp_path / "ws" / "app"))
    assert common.check_session_active(str(tmp_path / "ws"))
    assert common.check_session_active(str(tmp_path / "ws" / "other-project"))
    st = common.load_state()
    st["last_session_start"] = 0
    common.save_state(st)
    # Then only the marker file answers, and it names its project.
    assert (tmp_path / "store" / "session_active").is_file()
    assert not common.check_session_active(str(tmp_path / "ws" / "other-project"))


def test_a_session_started_once_stays_started(tmp_path: Path):
    st = common.load_state()
    st["last_session_start"] = time.time() - 6 * 3600  # six quiet hours
    common.save_state(st)
    assert common.check_session_active(str(tmp_path)) is True


def test_the_test_status_survives_a_stop():
    st = common.load_state()
    st["last_test_passed"] = True
    st["files_edited_this_session"] = ["a.py"]
    common.save_state(st)
    common.mark_session_ended()
    st = common.load_state()
    assert st.get("last_test_passed") is True, "a Stop is not a session end; the next run must be able to flip"
    assert st.get("files_edited_this_session") == [] and st.get("last_session_files") == ["a.py"]


def test_counts_name_their_project():
    assert common._project_label("/w/service-a") == "service-a"
    assert common._project_label("/w/service-a/") == "service-a"
    assert common._project_label("") == "workspace"


def test_loop_warnings_are_a_code_signal():
    for p, want in (("docs/plan.md", False), ("README.md", False), ("config.json", False), ("notes.txt", False),
                    ("pyproject.toml", False), ("nb.ipynb", False), ("windvane/stall.py", True),
                    ("src/app.ts", True), ("game/Main.luau", True), ("tools/reindex.ps1", True), ("lib.rs", True)):
        assert common._is_code_file(p) is want, p


# ---------------------------------------------------------------------------
# Test runs
# ---------------------------------------------------------------------------


def test_output_markers_count_only_for_commands_that_can_run_tests():
    can = common._command_can_run_tests
    assert can("grep -n 'passed' tests/test_x.py") is False
    assert can("git log --oneline -- scripts/pytest_dots.py") is False
    assert can("cat out.txt") is False
    assert can("venv/Scripts/python.exe scripts/pytest_dots.py") is True
    assert can("python -m pytest -q tests") is True
    assert can("FOO=1 python check.py") is True
    assert can("./run_tests.sh") is True
    assert can("") is False


def test_test_invocation_reads_every_segment_and_read_only_tools_never_count():
    inv = common._is_test_invocation
    assert inv("cd /w/app; .venv/Scripts/python.exe -m pytest 2>&1 | tail -1") is True
    assert inv("FOO=1 pytest -q tests") is True
    assert inv("sed -n 100,121p tests/bench_scoring.py") is False
    assert inv("cat session-logs/2026-09-10.md") is False


def test_test_tracking_judges_every_segment_and_the_output_shape():
    can, marks = common._command_can_run_tests, common._output_has_test_markers
    assert can("export PATH=/x:$PATH; cd /w/wt && pwd; uv lock --upgrade-package foo") is False
    assert can("cd /w/wt && pwd; git diff --name-only --diff-filter=U; git merge --abort tests/test_x.py") is False
    assert can("timeout 590 bash /w/tools/box.sh 'smoke'") is True  # the output decides
    assert can("export A=1; uv run pytest -q tests") is True
    assert can("cd /w && python -m pytest tests/test_x.py") is True
    assert can("ssh -p 22 box 'cd /w && python -m pytest'") is True
    assert can("uv sync && uv lock") is False
    assert marks("Resolved 12 packages in 1.2s\n0 errors") is False
    assert marks("box smoke: 3 checks, 0 errors, done") is False
    assert marks("3 passed, 1 error in 0.4s") is True
    assert marks("collected 4 items") is True
    assert marks("Ran 3 tests\n\nOK\n") is True


def test_a_chain_that_reads_a_log_is_not_a_test_run_unless_a_runner_is_named():
    can, kind = common._command_can_run_tests, common._segment_kind
    assert kind("cat out.txt") == "read" and kind("cd /w") == "noise" and kind("bash count.sh") == "run"
    assert kind("for f in a b") == "noise" and kind("date +%H:%M") == "noise" and kind("timeout 60 bash x.sh") == "run"
    assert can("cat out.txt; bash /w/tools/count-pytest.sh log.txt") is False
    assert can("tail -3 out.txt; date; grep -c FAILED log.txt") is False
    assert can("pwd; tail -1 log.txt; git add tests/test_x.py; git commit -m x") is False
    assert can("export PATH=/x:$PATH; bash /w/tools/box.sh 'smoke'") is True
    assert can("cat out.txt | tail -1; PYTHONPATH=src python -m pytest tests/scripts") is True  # a runner is named
    assert can("venv/Scripts/python.exe scripts/check.py") is True


def test_a_targeted_command_names_its_tests():
    t = common._is_targeted_test
    assert t("python -m pytest -q tests/test_alias.py") is True
    assert t("pytest tests/test_alias.py::test_one") is True
    assert t("pytest -k alias") is True
    assert t("venv/Scripts/python.exe -m pytest -q tests/test_hooks.py tests/test_stall.py") is True
    assert t("jest src/alias.test.ts") is True
    assert t("pytest") is False
    assert t("python -m pytest -q") is False
    assert t("npm test") is False
    assert t("go test ./...") is False
    assert t("cat tests/test_alias.py") is False


def test_the_banner_shows_the_last_targeted_test_that_passed_with_its_files(tmp_path: Path):
    """The banner names one command: the last targeted run that passed (and
    still passes), its files and when -- never the whole suite."""
    proj = _proj(tmp_path)
    p = str(proj)
    common._record_test_command(p, "python -m pytest -q", True, ["a.py"])
    common._record_test_command(p, "python -m pytest -q tests/test_alias.py", True, ["alias.py", "store.py"])
    assert "tests/test_alias.py" in common._last_targeted_line(p)
    line = common._last_targeted_line(p)
    assert line.startswith("Last targeted test that passed: python -m pytest -q tests/test_alias.py")
    assert "files: alias.py, store.py" in line and "ago" in line
    # A later targeted run that fails takes it off the banner; the older pass
    # of another targeted command is shown instead.
    common._record_test_command(p, "pytest tests/test_other.py", True, ["other.py"])
    time.sleep(0.01)
    common._record_test_command(p, "pytest tests/test_other.py", False)
    assert "tests/test_alias.py" in common._last_targeted_line(p)
    # The same line rides the recurring block (resume / first edit).
    assert any(l.startswith("Last targeted test that passed") for l in common._recurring_lines(p, []))
    assert not any("Known-good" in l for l in common._recurring_lines(p, []))


def test_a_passing_targeted_run_through_the_hook_records_its_files(tmp_path: Path, monkeypatch):
    proj = _proj(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    sid = "s-tests"
    _run("post_edit_json", {"session_id": sid, "tool_name": "Edit", "tool_input": {"file_path": str(proj / "alias.py")}})
    out = _run("bash_json", {"session_id": sid, "tool_name": "Bash", "tool_input": {"command": "python -m pytest -q tests/test_alias.py"},
                             "tool_response": {"stdout": "3 passed in 0.1s", "stderr": ""}})
    assert "PASS Test tracked" in _ctx(out) and "<windvane-test-tracked>" in _ctx(out)
    line = common._last_targeted_line(str(proj))
    assert "tests/test_alias.py" in line and "alias.py" in line
    # A same-verdict rerun is tracked in silence.
    out = _run("bash_json", {"session_id": sid, "tool_name": "Bash", "tool_input": {"command": "python -m pytest -q tests/test_alias.py"},
                             "tool_response": {"stdout": "3 passed in 0.1s", "stderr": ""}})
    assert "Test tracked" not in _ctx(out)


# ---------------------------------------------------------------------------
# Compliance context, mined patterns, the banner pieces
# ---------------------------------------------------------------------------


def test_rule_context_sees_approval_and_session_created_paths():
    from windvane.events.post_tool import _note_created_paths
    from windvane.events.pre_tool import _rule_context

    st: dict = {"last_prompt": "approved, delete the scratch dir"}
    _note_created_paths(st, [{"tool_name": "Bash", "tool_input": {"command": "mkdir -p /w/app/.scratch/tmpwork"}},
                            {"tool_name": "Write", "tool_input": {"file_path": "/w/app/.scratch/tmpwork/plan.md"}}])
    assert any(c.lower().endswith("/w/app/.scratch/tmpwork") for c in st["created_paths"])
    ctx = _rule_context(st, {"command": "rm -rf /w/app/.scratch/tmpwork"})
    assert "reads as approval" in ctx and "this session created" in ctx
    assert _rule_context({"last_prompt": "what is the plan?"}, {"command": "rm -rf /w/app/src"}) == ""


def test_recurring_errors_are_scoped_to_the_sessions_project(tmp_path: Path):
    store = tmp_path / "store"
    ws = tmp_path / "ws"
    for name in ("service-a", "service-b"):
        (ws / name / ".git").mkdir(parents=True)
    (ws / ".git").mkdir()
    norm = common._normalize_path
    root, a, b = norm(str(ws)), norm(str(ws / "service-a")), norm(str(ws / "service-b"))
    (store / "projects" / "roothash").mkdir(parents=True)
    (store / "manifest.json").write_text(json.dumps({"projects": {root: {"hash": "roothash"}}}), encoding="utf-8")
    (store / "projects" / "roothash" / "patterns.json").write_text(json.dumps({
        "struggles": [],
        "recurring_errors": [
            {"error_type": "AttributeError", "example": "AttributeError: InputEncoderRegistry", "session_count": 8, "projects": [root, b]},
            {"error_type": "KeyError", "example": "KeyError: 'sue'", "session_count": 3, "projects": [root, a]},
            {"error_type": "FileNotFoundError", "example": "FileNotFoundError: /home/x/.windvane/projects/h//session_index.json", "session_count": 7, "projects": [root, a]},
            {"error_type": "ValueError", "example": "ValueError: legacy, unattributed", "session_count": 2},
        ],
    }), encoding="utf-8")
    text = "\n".join(common._recurring_lines(a, []))
    assert "KeyError: 'sue'" in text          # attributed to this project
    assert "InputEncoderRegistry" not in text  # another project's
    assert ".windvane/projects" not in text    # the store's own failure
    assert "legacy, unattributed" in text      # no attribution: shown
    text_b = "\n".join(common._recurring_lines(b, []))
    assert "InputEncoderRegistry" in text_b and "KeyError" not in text_b


def _tr_rec(uuid, parent, typ, content, **extra):
    return {"uuid": uuid, "parentUuid": parent, "type": typ, "isSidechain": False,
            "timestamp": "2026-09-26T00:00:00Z", "message": {"role": typ, "content": content}, **extra}


def _tr_result(call, text):
    return [{"type": "tool_result", "tool_use_id": call, "content": [{"type": "text", "text": text}]}]


def _tr_call(cid, op):
    return [{"type": "tool_use", "id": cid, "name": "mcp__windvane__checkpoint", "input": {"operation": op}}]


def _rewound_transcript(tmp_path: Path) -> Path:
    """One rewind: the abandoned turn (checkpoint task_1) stays in the file,
    the new prompt hangs off the end of turn 1, task_2 on the live branch."""
    rec, result = _tr_rec, _tr_result
    recs = [
        rec("r1", None, "user", "let's use sqlite for the alias store"),
        rec("r2", "r1", "assistant", [{"type": "text", "text": "Done; the store is sqlite now."}]),
        {"uuid": "s1", "parentUuid": "r2", "type": "system", "subtype": "turn_duration", "isSidechain": False},
        rec("r3", "s1", "user", "let's use the registry for every alias lookup"),
        rec("r4", "r3", "assistant", _tr_call("c1", "save")),
        rec("r5", "r4", "user", result("c1", "Checkpoint saved.\ntask_id: task_1\n")),
        rec("r6", "r5", "assistant", [{"type": "text", "text": "Saved."}]),
        {"uuid": "s2", "parentUuid": "r6", "type": "system", "subtype": "turn_duration", "isSidechain": False},
        rec("r7", "s1", "user", "never resolve aliases outside the registry"),
        rec("r8", "r7", "assistant", _tr_call("c2", "save")),
        rec("r9", "r8", "user", result("c2", "Checkpoint saved.\ntask_id: task_2\n")),
        rec("r10", "r9", "assistant", _tr_call("c3", "restore")),
        rec("r11", "r10", "user", result("c3", "**Task:** rewound branch task\ntask_id: task_1\n")),
        rec("r12", "r11", "assistant", [{"type": "text", "text": "Restored."}]),
        {"uuid": "s3", "parentUuid": "r12", "type": "system", "subtype": "turn_duration", "isSidechain": False},
    ]
    p = tmp_path / "rewound.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    return p


def _rewound_ring(tmp_path: Path) -> Path:
    ring = tmp_path / "ring"
    ring.mkdir(exist_ok=True)
    now = time.time()
    entries = [
        {"task_id": "task_3", "kind": "manual", "created": now - 10, "session_id": "other", "summary": "another session's task", "task_description": "another session's task"},
        {"task_id": "task_1", "kind": "manual", "created": now - 20, "session_id": "mine", "summary": "rewound branch task", "task_description": "rewound branch task"},
        {"task_id": "task_2", "kind": "manual", "created": now - 30, "session_id": "mine", "summary": "own live task", "task_description": "own live task",
         "current_step": "step two", "completed_steps": ["step one done"], "next_steps": ["step two", "step three"],
         "files_in_progress": ["a.py", "b.py"], "warnings": ["never push without the word"], "context_needed": ["read the design first"]},
    ]
    (ring / "handoff_history.json").write_text(json.dumps({"handoffs": entries}), encoding="utf-8")
    return ring


def test_a_resume_with_no_edits_takes_the_project_from_the_sessions_own_checkpoint(tmp_path: Path, monkeypatch):
    """A session at a workspace root that worked through subagents alone has
    no edits of its own; it used to be taken for the root, and its banner
    read the newest session of any project under it (the clocked-in report,
    2026-10-05). Its own checkpoint names the project it was filed under."""
    pytest.importorskip("windvane.checkpoints")
    from windvane import checkpoints as ck
    from windvane.events import session_start
    from windvane.store import MemoryStore

    ws = tmp_path / "ws"
    game = ws / "game"
    (game / ".git").mkdir(parents=True)
    (ws / ".git").mkdir()
    norm = common._normalize_path
    store = MemoryStore()
    store.remember_project(str(ws))
    store.remember_project(str(game))
    monkeypatch.setattr(common, "_session_id", "mine")
    monkeypatch.setattr(common, "_stdin_cache", None)
    ring = ck.register_project_ring(str(game))
    assert ring is not None
    now = time.time()
    own = {"task_id": "task_5", "kind": "manual", "created": now - 5, "session_id": "mine",
           "project_path": norm(str(game)), "summary": "wire the lobby", "task_description": "wire the lobby"}
    (ring / "handoff_history.json").write_text(json.dumps({"handoffs": [own]}), encoding="utf-8")

    # No transcript, no state lists: the cwd is the root, and it is a hub.
    work_project, resume_files = common._banner_work_project(str(ws), "compact", "")
    assert norm(work_project) == norm(str(ws)) and resume_files == []
    assert common._is_hub(str(ws)) and not common._is_hub(str(game))
    restored, _skipped = common._banner_checkpoint(str(ws), work_project, "compact", False, "")
    assert restored and restored["task_id"] == "task_5"
    assert common._banner_project_from_checkpoint(str(ws), work_project, "mine", restored) == norm(str(game))
    # Another session's record, or a record of the root itself, names nothing.
    other = dict(own, session_id="theirs")
    assert common._banner_project_from_checkpoint(str(ws), work_project, "mine", other) == work_project
    assert common._banner_project_from_checkpoint(str(ws), work_project, "mine", dict(own, project_path=norm(str(ws)))) == work_project
    # The edits named the project already: the record changes nothing.
    assert common._banner_project_from_checkpoint(str(ws), norm(str(game)), "mine", own) == norm(str(game))

    # The last-session block reads the index at the root. With the project
    # unknown on a compaction it prints nothing; with the project known it
    # shows that project's newest session; on a fresh start the root's
    # newest is shown as before.
    root_dir = common.get_project_memory_dir(str(ws))
    root_dir.mkdir(parents=True, exist_ok=True)
    (root_dir / "session_index.json").write_text(json.dumps({"version": 1, "sessions": {
        "s-other": {"session_id": "s-other", "last_timestamp": "2026-10-05T10:00:00Z", "git_branch": "main",
                    "files_edited": [str(ws / "tools" / "map.py")], "error_count": 0, "prompt_count": 4},
        "s-game": {"session_id": "s-game", "last_timestamp": "2026-10-05T09:00:00Z", "git_branch": "lane",
                   "files_edited": [str(game / "src" / "lobby.luau")], "error_count": 1, "prompt_count": 7},
    }}), encoding="utf-8")
    assert session_start._last_session_lines(str(ws), work_project, [], "compact") == []
    known = "\n".join(session_start._last_session_lines(str(ws), norm(str(game)), [], "compact"))
    assert "Last session" in known and "lobby.luau" in known and "map.py" not in known
    fresh = "\n".join(session_start._last_session_lines(str(ws), work_project, [], "startup"))
    assert "Last session" in fresh and "map.py" in fresh


def test_after_a_compaction_the_banner_shows_this_sessions_own_full_checkpoint(tmp_path: Path):
    pytest.importorskip("windvane.checkpoints")
    p = _rewound_transcript(tmp_path)
    ring = _rewound_ring(tmp_path)
    chosen, skipped = common._own_session_checkpoint([ring], "mine", str(p))
    assert chosen and chosen["task_id"] == "task_2" and [s["task_id"] for s in skipped] == ["task_1"]
    text = "\n".join(common._format_restored_full(chosen, skipped))
    for piece in ("own live task", "step one done", "step two", "step three", "a.py", "b.py",
                  "never push without the word", "read the design first", "task_1", "rewound"):
        assert piece in text, piece
    assert common._own_session_checkpoint([ring], "nobody", str(p)) == (None, [])


def test_the_rules_block_says_how_many_it_left_out():
    rules = [{"id": f"r{i}", "category": "rule", "content": f"rule number {i} " + "x" * 200} for i in range(8)]
    out = common._rules_block("/w/app", {"entries": rules})
    assert out[0].startswith("Rules (8, app)") and len(out) == 7
    assert out[-1] == "  ... and 3 more (the /windvane pane lists all)"
    assert all(len(line) <= 2 + 2 + 3 + 120 + 2 for line in out[1:6])
    few = common._rules_block("/w/app", {"entries": rules[:5]})
    assert len(few) == 6 and "more" not in few[-1]
    # A detector-backed rule is enforced on tool calls, so it is shown first and marked.
    rules[7] = {"id": "r7", "category": "rule", "content": "never rm", "detector": {"tools": ["Bash"], "command": r"\brm\b"}}
    first = common._rules_block("/w/app", {"entries": rules})[1]
    assert first == "  [r7] [detector] never rm"


def test_the_pre_edit_mistakes_put_the_projects_own_first():
    mem = {"entries": [
        {"id": "m-other", "category": "mistake", "content": "MISTAKE: dropped the index in db.py",
         "created_at": 200, "_inherited": True, "related_files": ["/w/other/db.py"]},
        {"id": "m-own", "category": "mistake", "content": "MISTAKE: forgot the migration for db.py",
         "created_at": 100, "_inherited": True, "related_files": ["/w/app/db.py"]},
    ]}
    assert common._file_mistakes(mem, "/w/app/db.py")[0].startswith("dropped")  # newest first, no project
    assert common._file_mistakes(mem, "/w/app/db.py", "/w/app")[0].startswith("forgot")  # the project's own first


def test_the_full_restore_shows_files_relative_to_the_records_project(tmp_path: Path):
    proj = tmp_path / "app"
    inside = proj / "src" / "draft.py"
    outside = tmp_path / "elsewhere" / "notes.md"
    entry = {"kind": "manual", "created": time.time(), "task_description": "t", "summary": "s",
             "project_path": str(proj).replace("\\", "/").lower(),
             "files_in_progress": [str(inside), str(outside), "rel/x.py"]}
    files = [l for l in common._format_restored_full(entry) if l.startswith("  Files: ")][0]
    assert files == "  Files: src/draft.py, notes.md, rel/x.py"
    assert entry["files_in_progress"][0] == str(inside)  # the record keeps the full path


def test_the_compaction_banner_leaves_out_what_the_mod_put_in_the_conversation(tmp_path: Path):
    import os as _os

    from windvane import pressure as cp

    (tmp_path / "store" / "sessions").mkdir(parents=True)
    sid = "aaaaaaaa-0000-4000-8000-00000000000c"
    assert not cp.compaction_briefed(sid) and not common._compaction_briefed("compact", sid)
    marker = cp.brief_marker_path(sid)
    marker.write_bytes(b'{"plugin": "windvane", "ts": 1.0}')
    assert common._compaction_briefed("compact", sid)
    assert not common._compaction_briefed("resume", sid) and not common._compaction_briefed("startup", sid)
    old = time.time() - cp.BRIEF_MARKER_FRESH_SECS - 5
    _os.utime(marker, (old, old))
    assert not common._compaction_briefed("compact", sid)


# ---------------------------------------------------------------------------
# SessionStart: the pack, the scaffold off by default
# ---------------------------------------------------------------------------


def test_a_fresh_start_seeds_rules_but_no_scaffold_unless_structure_is_on(tmp_path: Path, monkeypatch):
    pytest.importorskip("windvane.rules")
    pytest.importorskip("windvane.store")
    proj = _proj(tmp_path, "fresh")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    out = _run("session_start_json", {"session_id": "s-fresh", "source": "startup", "cwd": str(proj)})
    text = _ctx(out)
    assert text.startswith("windvane session started (startup)")
    assert "Rules seeded" in text
    assert not (proj / "CLAUDE.md").exists() and not (proj / ".learnings").exists() and not (proj / "session-logs").exists()
    assert "rotation" not in text.lower()

    on = _proj(tmp_path, "scaffolded")
    (on / ".windvane").mkdir()
    (on / ".windvane" / "config.json").write_text('{"structure": true}', encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(on))
    text = _ctx(_run("session_start_json", {"session_id": "s-scaffold", "source": "startup", "cwd": str(on)}))
    assert (on / "CLAUDE.md").is_file() and (on / ".learnings" / "ERRORS.md").is_file() and (on / "session-logs").is_dir()
    assert "Project structure created" in text


def test_session_start_announces_autonomy_mode(tmp_path: Path, monkeypatch):
    proj = _proj(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    text = _ctx(_run("session_start_json", {"session_id": "s-att", "source": "resume", "cwd": str(proj)}))
    assert "AUTONOMY MODE" not in text
    monkeypatch.setenv("WINDVANE_AUTONOMY", "1")
    monkeypatch.setenv("WINDVANE_STRIKE_CAP", "4")
    text = _ctx(_run("session_start_json", {"session_id": "s-aut", "source": "resume", "cwd": str(proj)}))
    assert "AUTONOMY MODE: halt armed at strike 4" in text and "NOT configured" in text


# ---------------------------------------------------------------------------
# The stall ladder through the hooks: text in autonomy mode only
# ---------------------------------------------------------------------------


def _no_effect_turn(sid: str, proj: Path) -> None:
    _run("post_batch_json", {"session_id": sid, "cwd": str(proj), "hook_event_name": "PostToolBatch",
                             "tool_calls": [{"tool_name": "Bash", "tool_input": {"command": "cat out.txt"}, "tool_use_id": "t1",
                                             "tool_response": {"stdout": "line"}}]})
    _run("stop_json", {"session_id": sid, "cwd": str(proj), "hook_event_name": "Stop", "last_assistant_message": "Still waiting.", "stop_hook_active": False})


def test_an_attended_session_counts_strikes_but_gets_no_stall_text(tmp_path: Path, monkeypatch):
    proj = _proj(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    sid = "s-attended"
    for _ in range(3):
        _no_effect_turn(sid, proj)
    st = _state(tmp_path / "store", sid)["stall"]
    assert st["strikes"] == 1 and isinstance(st.get("pending"), dict)
    assert any(e.get("kind") == "strike" for e in st.get("events", []))
    out = _run("post_batch_json", {"session_id": sid, "cwd": str(proj), "tool_calls": []})
    assert "windvane-stall" not in _ctx(out)
    st = _state(tmp_path / "store", sid)["stall"]
    assert st["strikes"] == 1 and st.get("pending") is None  # recorded, dropped, not delivered


def test_autonomy_mode_delivers_the_strike_once(tmp_path: Path, monkeypatch):
    proj = _proj(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    monkeypatch.setenv("WINDVANE_AUTONOMY", "1")
    sid = "s-auto-strike"
    for _ in range(3):
        _no_effect_turn(sid, proj)
    out = _run("post_batch_json", {"session_id": sid, "cwd": str(proj), "tool_calls": []})
    assert "windvane-stall" in _ctx(out) and "Strike 1 of 3" in _ctx(out)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolBatch"
    out = _run("post_batch_json", {"session_id": sid, "cwd": str(proj), "tool_calls": []})
    assert "windvane-stall" not in _ctx(out)
    # A subagent's batch is counted but never nudged.
    out = _run("post_batch_json", {"session_id": sid, "cwd": str(proj), "agent_id": "agent-1",
                                   "tool_calls": [{"tool_name": "Read", "tool_input": {"file_path": "x"}}]})
    assert out == {}


def test_the_halt_end_to_end(tmp_path: Path, monkeypatch):
    """Autonomy on, cap 2, one turn per strike: the halt arms, denies, keeps
    the record calls open, says so once, and the alerts are recorded."""
    proj = _proj(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    monkeypatch.setenv("WINDVANE_AUTONOMY", "1")
    monkeypatch.setenv("WINDVANE_STALL_TURNS", "1")
    monkeypatch.setenv("WINDVANE_STRIKE_CAP", "2")
    sid = "s-halt"
    store = tmp_path / "store"
    pre = {"session_id": sid, "cwd": str(proj), "hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {"file_path": "x"}}
    _no_effect_turn(sid, proj)
    assert _state(store, sid)["stall"]["strikes"] == 1 and not _state(store, sid)["stall"].get("halted")
    assert _run("pre_tool_json", pre) == {}
    _no_effect_turn(sid, proj)
    s = _state(store, sid)
    assert s["stall"]["strikes"] == 2 and isinstance(s["stall"].get("halted"), dict)
    assert any(a["kind"] == "halt" and a["sent"] is False for a in s.get("alerts", []))
    hso = _run("pre_tool_json", pre)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny" and "windvane.stall release" in hso["permissionDecisionReason"]
    assert _run("pre_tool_json", dict(pre, tool_name="mcp__other__query"))["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert _run("pre_tool_json", dict(pre, tool_name="PushNotification")) == {}
    assert _run("pre_tool_json", dict(pre, tool_name="mcp__windvane__checkpoint", tool_input={"operation": "save"})) == {}
    assert _run("pre_tool_json", dict(pre, agent_id="sub-1")) == {}  # a subagent finishes and reports
    assert _state(store, sid)["stall"]["halted"]["denied"] == 2
    bash = {"session_id": sid, "cwd": str(proj), "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "t9", "permission_mode": "bypassPermissions"}
    assert "windvane-halt" in _ctx(_run("pre_bash_json", bash))
    assert "windvane-halt" not in _ctx(_run("pre_bash_json", dict(bash, tool_use_id="t10")))


def test_alerts_through_the_hooks(tmp_path: Path, monkeypatch):
    proj = _proj(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    sink = tmp_path / "sink.py"
    log = tmp_path / "alerts.log"
    sink.write_text("import sys\nopen(sys.argv[2], 'a', encoding='utf-8').write(sys.argv[1].strip() + '\\n')\n", encoding="utf-8")
    monkeypatch.setenv("WINDVANE_ALERT_COMMAND", f'"{sys.executable}" "{sink}" {{message}} "{log}"')
    sid = "s-alerts"
    note = {"session_id": sid, "cwd": str(proj), "hook_event_name": "Notification", "notification_type": "agent_needs_input"}
    _run("notification_json", note)
    assert not log.exists(), "outside autonomy mode notifications are silent"
    monkeypatch.setenv("WINDVANE_AUTONOMY", "1")
    _run("notification_json", dict(note, notification_type="permission_prompt", message="Allow Bash?"))
    assert "waiting on you: permission_prompt" in log.read_text(encoding="utf-8")
    _run("notification_json", dict(note, notification_type="auth_success"))
    assert "auth_success" not in log.read_text(encoding="utf-8")
    _run("stop_failure_json", {"session_id": sid, "cwd": str(proj), "hook_event_name": "StopFailure", "error_type": "rate_limit", "error": "limit"})
    assert "stopped: rate_limit" in log.read_text(encoding="utf-8")


def test_stop_failure_records_every_rate_limit_window(tmp_path: Path, monkeypatch):
    from windvane import pressure as cp

    proj = _proj(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    sid = "s-limits"
    cp.record_statusline({"session_id": sid, "rate_limits": {
        "five_hour": {"used_percentage": 97, "resets_at": 1900000000},
        "seven_day_opus": {"used_percentage": 91, "resets_at": 1900500000}}})
    _run("stop_failure_json", {"session_id": sid, "cwd": str(proj), "error_type": "rate_limit", "error": "limit"})
    rec = _state(tmp_path / "store", sid)["run"]["last_failure"]
    assert rec["error_type"] == "rate_limit" and rec["five_hour_pct"] == 97
    assert rec["rate_limits"]["seven_day_opus"] == {"pct": 91, "resets_at": 1900500000}


# ---------------------------------------------------------------------------
# The /goal bracket through the hooks
# ---------------------------------------------------------------------------


def _ts(i: int) -> str:
    return f"2026-09-10T22:46:{i:02d}.000Z"


def _sentinel(cond: str, i: int) -> str:
    return json.dumps({"type": "attachment", "timestamp": _ts(i), "attachment": {"type": "goal_status", "met": False, "sentinel": True, "condition": cond}})


def _verdict(cond: str, i: int, met: bool, reason: str = "because") -> str:
    return json.dumps({"type": "attachment", "timestamp": _ts(i), "attachment": {
        "type": "goal_status", "met": met, "condition": cond, "reason": reason, "iterations": 1, "durationMs": 900, "tokens": 1200}})


def _noise(i: int) -> str:
    return json.dumps({"type": "assistant", "timestamp": _ts(i), "message": {"role": "assistant", "content": [{"type": "text", "text": "working"}]}})


def _write(path: Path, *lines: str) -> None:
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


def test_the_goal_bracket_end_to_end(tmp_path: Path, monkeypatch):
    proj = _proj(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    sink = tmp_path / "sink.py"
    log = tmp_path / "goal-alerts.log"
    sink.write_text("import sys\nopen(sys.argv[2], 'a', encoding='utf-8').write(sys.argv[1].strip() + '\\n')\n", encoding="utf-8")
    monkeypatch.setenv("WINDVANE_ALERT_COMMAND", f'"{sys.executable}" "{sink}" {{message}} "{log}"')
    sid = "s-goal-e2e"
    store = tmp_path / "store"
    t = tmp_path / "transcript.jsonl"
    stop = {"session_id": sid, "cwd": str(proj), "transcript_path": str(t), "hook_event_name": "Stop", "last_assistant_message": "Working on it.", "stop_hook_active": False}

    _write(t, _noise(1))
    assert _run("stop_json", stop) == {}
    assert not (_state(store, sid).get("run") or {}).get("auto")
    _write(t, _noise(1), _sentinel("count reaches three", 2))
    assert "block" not in json.dumps(_run("stop_json", stop))
    a = _state(store, sid)["run"]["auto"]
    assert a["status"] == "running" and a["turns"] == 1
    manifests = list((proj / ".windvane" / "runs").glob("*.manifest.json"))
    assert manifests and '"mode": "goal"' in manifests[0].read_text(encoding="utf-8")
    # No staged directive rides the next injection point any more.
    out = _run("bash_json", {"session_id": sid, "cwd": str(proj), "tool_name": "Bash", "tool_input": {"command": "ls"},
                             "tool_response": {"stdout": "a.py", "stderr": ""}})
    assert "windvane-goal" not in _ctx(out)
    _write(t, _noise(1), _sentinel("count reaches three", 2), _verdict("count reaches three", 3, False, "not yet"))
    _run("stop_json", dict(stop, stop_hook_active=True))
    a = _state(store, sid)["run"]["auto"]
    assert a["turns"] == 2 and a["verdicts"] == 1
    _write(t, _noise(1), _sentinel("count reaches three", 2), _verdict("count reaches three", 3, False),
           _verdict("count reaches three", 5, True, "the count printed 3"))
    _run("prompt_json", {"session_id": sid, "cwd": str(proj), "transcript_path": str(t), "hook_event_name": "UserPromptSubmit", "prompt": "thanks"})
    a = _state(store, sid)["run"]["auto"]
    assert a["status"] == "met" and "printed 3" in a["why"]
    alerts = log.read_text(encoding="utf-8").splitlines()
    assert len([x for x in alerts if "goal met" in x]) == 1, alerts
    reports = list((proj / ".windvane" / "runs").glob("*.md"))
    assert reports and "count reaches three" in reports[0].read_text(encoding="utf-8")
    _run("stop_json", stop)
    assert _state(store, sid)["run"]["auto"]["status"] == "met"
    assert len([x for x in log.read_text(encoding="utf-8").splitlines() if "goal met" in x]) == 1


def test_session_end_closes_a_goal_whose_verdict_landed_after_the_last_stop(tmp_path: Path, monkeypatch):
    proj = _proj(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    sid = "s-goal-end"
    t = tmp_path / "t-end.jsonl"
    _write(t, _sentinel("g2", 2))
    _run("stop_json", {"session_id": sid, "cwd": str(proj), "transcript_path": str(t), "last_assistant_message": "x"})
    _write(t, _sentinel("g2", 2), _verdict("g2", 4, True, "done"))
    _run("session_end_json", {"session_id": sid, "cwd": str(proj), "transcript_path": str(t), "reason": "other"})
    assert _state(tmp_path / "store", sid)["run"]["auto"]["status"] == "met"


# ---------------------------------------------------------------------------
# Stop banks the draft
# ---------------------------------------------------------------------------


def _edit_transcript(path: Path, *files: Path) -> None:
    """A transcript whose turn edited ``files`` and closed with a next step:
    what the recorder drafts from."""
    recs = [{"uuid": "u0", "parentUuid": None, "type": "user", "isSidechain": False, "timestamp": _ts(1),
             "message": {"role": "user", "content": "wire the alias CLI"}}]
    for i, f in enumerate(files):
        recs.append({"uuid": f"a{i}", "parentUuid": recs[-1]["uuid"], "type": "assistant", "isSidechain": False, "timestamp": _ts(2 + i),
                     "message": {"role": "assistant", "content": [
                         {"type": "tool_use", "id": f"e{i}", "name": "Edit", "input": {"file_path": str(f), "old_string": "a", "new_string": "b"}}]}})
        recs.append({"uuid": f"r{i}", "parentUuid": f"a{i}", "type": "user", "isSidechain": False, "timestamp": _ts(2 + i),
                     "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"e{i}", "content": "ok"}]}})
    recs.append({"uuid": "z", "parentUuid": recs[-1]["uuid"], "type": "assistant", "isSidechain": False, "timestamp": _ts(9),
                 "message": {"role": "assistant", "content": [{"type": "text", "text": "The parser is in place. Next: wire the CLI."}]}})
    _write(path, *(json.dumps(r) for r in recs))


def _latest(store: Path) -> dict:
    from windvane import checkpoints as ck

    p = ck.global_ring_dir() / ck.LATEST_FILENAME
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def test_stop_banks_the_draft_after_an_edit_at_most_once_per_ten_minutes(tmp_path: Path, monkeypatch):
    pytest.importorskip("windvane.draft")
    proj = _proj(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    store = tmp_path / "store"
    sid = "s-bank"
    t = tmp_path / "t-bank.jsonl"
    _edit_transcript(t, proj / "a.py")
    stop ={"session_id": sid, "cwd": str(proj), "transcript_path": str(t), "last_assistant_message": "Next: wire the CLI.", "stop_hook_active": False}

    # No edit yet: nothing banked by the draft.
    _run("stop_json", stop)
    assert not (_state(store, sid).get("draft_bank") or {}).get("at")

    _run("post_edit_json", {"session_id": sid, "tool_name": "Edit", "tool_input": {"file_path": str(proj / "a.py")}})
    _run("stop_json", stop)
    db = _state(store, sid)["draft_bank"]
    assert db["at"] and db["edits"] == 1
    assert _latest(store).get("trigger") == "stop" and _latest(store).get("session_id") == sid

    # Another edit inside ten minutes: no second bank.
    first_at = db["at"]
    _run("post_edit_json", {"session_id": sid, "tool_name": "Edit", "tool_input": {"file_path": str(proj / "b.py")}})
    _run("stop_json", stop)
    assert _state(store, sid)["draft_bank"]["at"] == first_at

    # Past the gap, a turn that saved a checkpoint itself is not banked over.
    st = _state(store, sid)
    st["draft_bank"]["at"] = time.time() - 3600
    (store / "sessions" / f"{sid}.json").write_text(json.dumps(st), encoding="utf-8")
    _run("post_batch_json", {"session_id": sid, "cwd": str(proj), "tool_calls": [
        {"tool_name": "mcp__windvane__checkpoint", "tool_input": {"operation": "save"}, "tool_use_id": "c1", "tool_response": "saved"}]})
    _run("stop_json", stop)
    st = _state(store, sid)
    assert st["draft_bank"]["at"] < time.time() - 3000 and st["draft_bank"]["edits"] == 2

    # The next edit past the gap is banked again.
    _run("post_edit_json", {"session_id": sid, "tool_name": "Edit", "tool_input": {"file_path": str(proj / "c.py")}})
    _run("stop_json", stop)
    assert _state(store, sid)["draft_bank"]["at"] > time.time() - 60


def test_stop_brings_a_save_made_before_the_turns_edits_up_to_the_draft(tmp_path: Path, monkeypatch):
    """The live demo: the model banks the checkpoint on the CHECKPOINT NOW
    note at the top of the turn, then edits; the record said the edit was
    still to come. The turn end refreshes it in place."""
    pytest.importorskip("windvane.draft")
    from windvane import checkpoints as ck

    proj = _proj(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    store = tmp_path / "store"
    sid = "s-refresh"
    t = tmp_path / "t-refresh.jsonl"
    _edit_transcript(t, proj / "api.py")
    stop = {"session_id": sid, "cwd": str(proj), "transcript_path": str(t),
            "last_assistant_message": "Added cursor paging. Next: the tests.", "stop_hook_active": False}

    # The turn's first act: the save, as the tool makes it (the tool process
    # learns the session from the environment), with the summary drafted.
    _run("post_batch_json", {"session_id": sid, "cwd": str(proj), "tool_calls": [
        {"tool_name": "mcp__windvane__checkpoint", "tool_input": {"operation": "save"}, "tool_use_id": "c1", "tool_response": "saved"}]})
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    resp = ck.ContextGuard().save_checkpoint(
        task_description="Cursor pagination for GET /items", current_step="", completed_steps=[], files_involved=[],
        handoff_summary="Cursor paging is only planned.", pending_steps=["Keep page= working"],
        project_path=str(proj), drafted_fields=["handoff_summary"])
    assert resp.status == "success"
    assert _state(store, sid)["pressure"]["edits_at_manual_checkpoint"] == 0
    before = ck.read_latest(ck.candidate_dirs(str(proj))) or {}
    assert before["kind"] == "manual" and before["files_in_progress"] == []

    # Then the edit, and the turn ends.
    _run("post_edit_json", {"session_id": sid, "tool_name": "Edit", "tool_input": {"file_path": str(proj / "api.py")}})
    _run("stop_json", stop)
    after = ck.read_latest(ck.candidate_dirs(str(proj))) or {}
    assert after["task_id"] == before["task_id"] and after["kind"] == "manual"
    assert [Path(f).name for f in after["files_in_progress"]] == ["api.py"]
    assert after["task_description"] == "Cursor pagination for GET /items"
    assert after["next_steps"] == ["Keep page= working"]
    assert after["summary"] != "Cursor paging is only planned." and after["metadata"]["refreshed"]["fields"]
    assert sum(1 for h in ck.read_history(ck.candidate_dirs(str(proj))) if h["kind"] == "manual") == 1
    # No automatic entry was banked over it, and the mark moved.
    assert not (_state(store, sid)["draft_bank"]).get("at")
    assert _state(store, sid)["pressure"]["edits_at_manual_checkpoint"] == 1

    # A turn with no further edit changes nothing.
    stamp = after["metadata"]["refreshed"]["at"]
    _run("stop_json", stop)
    assert (ck.read_latest(ck.candidate_dirs(str(proj))) or {})["metadata"]["refreshed"]["at"] == stamp


def test_stop_refreshes_a_save_made_after_the_turns_edits_with_the_turns_own_reply(tmp_path: Path, monkeypatch):
    """The second take: the model edits first and saves after, so no edit
    follows the save, but the drafted summary is the previous reply's. The
    turn end takes the turn's own closing reply."""
    pytest.importorskip("windvane.draft")
    from windvane import checkpoints as ck

    proj = _proj(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    store = tmp_path / "store"
    sid = "s-refresh-late"
    t = tmp_path / "t-late.jsonl"
    _edit_transcript(t, proj / "api.py")
    stop = {"session_id": sid, "cwd": str(proj), "transcript_path": str(t),
            "last_assistant_message": "Added cursor paging. Next: the tests.", "stop_hook_active": False}

    _run("post_edit_json", {"session_id": sid, "tool_name": "Edit", "tool_input": {"file_path": str(proj / "api.py")}})
    _run("post_batch_json", {"session_id": sid, "cwd": str(proj), "tool_calls": [
        {"tool_name": "mcp__windvane__checkpoint", "tool_input": {"operation": "save"}, "tool_use_id": "c1", "tool_response": "saved"}]})
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    resp = ck.ContextGuard().save_checkpoint(
        task_description="Cursor pagination for GET /items", current_step="", completed_steps=[], pending_steps=[],
        files_involved=[str(proj / "api.py")], handoff_summary="Cursor paging is only planned.",
        project_path=str(proj), drafted_fields=["handoff_summary", "files_involved"])
    assert resp.status == "success"
    assert _state(store, sid)["pressure"]["edits_at_manual_checkpoint"] == 1

    _run("stop_json", stop)
    after = ck.read_latest(ck.candidate_dirs(str(proj))) or {}
    assert after["kind"] == "manual" and "handoff_summary" in after["metadata"]["refreshed"]["fields"]
    # The reply is the Stop payload's, not the transcript's last text: at
    # Stop the file can still lack the reply that ended the turn (the live
    # demo's fourth take banked the line before the save that way).
    assert after["summary"] == "Added cursor paging. Next: the tests."
    assert after["task_description"] == "Cursor pagination for GET /items"
    assert sum(1 for h in ck.read_history(ck.candidate_dirs(str(proj))) if h["kind"] == "manual") == 1


def test_pre_compact_banks_the_draft_to_the_ring(tmp_path: Path, monkeypatch):
    pytest.importorskip("windvane.draft")
    proj = _proj(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    sid = "s-precompact"
    t = tmp_path / "t-pc.jsonl"
    _edit_transcript(t, proj / "a.py")
    _run("post_edit_json", {"session_id": sid, "tool_name": "Edit", "tool_input": {"file_path": str(proj / "a.py")}})
    _run("pre_compact_json", {"session_id": sid, "trigger": "auto", "transcript_path": str(t), "cwd": str(proj)})
    entry = _latest(tmp_path / "store")
    assert (entry.get("kind"), entry.get("trigger"), entry.get("session_id")) == ("auto", "auto", sid)
    from windvane.events.compact import _precompact_handoff

    empty = tmp_path / "empty"
    empty.mkdir()
    bare = _precompact_handoff(str(empty), {}, str(empty), "manual", {})
    assert bare["summary"] and bare["kind"] == "auto"


# ---------------------------------------------------------------------------
# An empty search: the nearest code-index symbols
# ---------------------------------------------------------------------------


def test_the_search_pattern_is_read_from_the_command():
    from windvane.events.post_tool import _failure_is_empty_search, _search_query

    assert _search_query('rg -n "AliasRegistery" src') == "AliasRegistery"
    assert _search_query("grep -rn --include=*.py resolve_alais .") == "resolve_alais"
    assert _search_query("cd src && git grep -n -e load_alias") == "load_alias"
    assert _search_query("rg -t py -g '*.py' alias_store") == "alias_store"
    assert _search_query("Get-ChildItem | Select-String -Pattern AliasStore") == "AliasStore"
    assert _search_query("ls src") == ""
    assert _search_query("cat a.py | head") == ""
    assert _failure_is_empty_search("Exit code 1") is True
    assert _failure_is_empty_search("Exit code 2\ngrep: nope: No such file or directory") is False


def _indexed_project(tmp_path: Path) -> Path:
    code_index = pytest.importorskip("windvane.code_index")
    proj = _proj(tmp_path, "indexed")
    (proj / "src").mkdir()
    (proj / "src" / "aliases.py").write_text(
        "class AliasRegistry:\n    pass\n\n\ndef resolve_alias(name):\n    return name\n\n\ndef load_alias_store(path):\n    return {}\n",
        encoding="utf-8")
    d = code_index.index_dir_for(str(proj))
    if d is None:
        pytest.skip("the store gives this project no index directory")
    d.mkdir(parents=True, exist_ok=True)
    if code_index.build_code_index(str(proj), d) is None:
        pytest.skip("the code index built nothing")
    return proj


def test_an_empty_search_injects_one_line_of_the_nearest_symbols(tmp_path: Path, monkeypatch):
    proj = _indexed_project(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    sid = "s-search"
    out = _run("bash_json", {"session_id": sid, "cwd": str(proj), "tool_name": "Bash",
                             "tool_input": {"command": "rg -n AliasRegistery src"}, "tool_response": {"stdout": "", "stderr": ""}})
    text = _ctx(out)
    hint = [l for l in text.splitlines() if "windvane-search" in l]
    assert len(hint) == 1 and "AliasRegistry" in hint[0] and 'No match for "AliasRegistery"' in hint[0]
    # grep/rg exit 1 when nothing matched: the failure hook says the same.
    out = _run("tool_failure_json", {"session_id": sid, "cwd": str(proj), "tool_name": "Bash",
                                     "tool_input": {"command": "grep -rn resolve_alais src"}, "error": "Exit code 1"})
    assert "resolve_alias" in _ctx(out) and _ctx(out).count("windvane-search") == 2
    # A search that found something, or a command that is no search: nothing.
    out = _run("bash_json", {"session_id": sid, "cwd": str(proj), "tool_name": "Bash",
                             "tool_input": {"command": "rg -n AliasRegistry src"}, "tool_response": {"stdout": "src/aliases.py:1:class AliasRegistry:", "stderr": ""}})
    assert "windvane-search" not in _ctx(out)
    out = _run("bash_json", {"session_id": sid, "cwd": str(proj), "tool_name": "Bash",
                             "tool_input": {"command": "true"}, "tool_response": {"stdout": "", "stderr": ""}})
    assert "windvane-search" not in _ctx(out)


# ---------------------------------------------------------------------------
# Edits and the pre-edit block
# ---------------------------------------------------------------------------


def test_the_loop_warning_speaks_at_the_threshold_and_asks_for_targeted_tests(tmp_path: Path, monkeypatch):
    proj = _proj(tmp_path)
    f = proj / "loop.py"
    f.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    sid = "s-loop"
    _run("session_start_json", {"session_id": sid, "source": "resume", "cwd": str(proj)})
    texts = []
    for _ in range(8):
        _run("post_edit_json", {"session_id": sid, "tool_name": "Edit", "tool_input": {"file_path": str(f)}})
        texts.append(_ctx(_run("pre_edit_json", {"session_id": sid, "tool_name": "Edit", "tool_input": {"file_path": str(f), "new_string": "x = 2\n"}})))
    assert "8 edits to loop.py without running its targeted tests" in texts[-1]
    assert not any("without running" in t for t in texts[:-1])


def test_a_subagent_edit_is_tracked_but_never_injected(tmp_path: Path, monkeypatch):
    proj = _proj(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    sid = "s-sub"
    out = _run("pre_edit_json", {"session_id": sid, "agent_id": "a1", "tool_name": "Edit", "tool_input": {"file_path": str(proj / "a.py")}})
    assert out == {}
    _run("post_edit_json", {"session_id": sid, "agent_id": "a1", "tool_name": "Edit", "tool_input": {"file_path": str(proj / "a.py")}})
    st = _state(tmp_path / "store", sid)
    assert str(proj / "a.py") in st["files_edited_this_session"] and st["edits_total"] == 1


def test_a_read_is_oriented_once_per_file(tmp_path: Path, monkeypatch):
    proj = _proj(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    last = tmp_path / "last-file.txt"
    monkeypatch.setenv("WINDVANE_LAST_FILE_PATH", str(last))
    sid = "s-read"
    payload = {"session_id": sid, "tool_name": "Read", "tool_input": {"file_path": str(proj / "a.py")}}
    _run("pre_read_json", payload)
    assert last.read_text(encoding="utf-8") == str(proj / "a.py")
    _run("pre_read_json", payload)
    assert _state(tmp_path / "store", sid)["read_injected"] == [str(proj / "a.py").replace("\\", "/").lower()]


def test_a_prompt_decision_is_stored_in_the_sessions_project(tmp_path: Path, monkeypatch):
    pytest.importorskip("windvane.capture")
    proj = _proj(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    from windvane import capture

    monkeypatch.setattr(capture, "capture_decision", lambda text, server_only=False: "use sqlite for the alias store")
    sid = "s-decide"
    _run("prompt_json", {"session_id": sid, "cwd": str(proj), "prompt": "from now on let's use sqlite for the alias store"})
    st = _state(tmp_path / "store", sid)
    assert st["last_prompt"].startswith("from now on")
    mem = common.load_project_memory(str(proj))
    assert any(e.get("content") == "DECISION: (from user) use sqlite for the alias store" for e in mem.get("entries", []))


def test_a_typed_decision_is_captured_by_the_real_gate(tmp_path: Path, monkeypatch):
    """Unmocked beside the case above: the regex tier and the shape gate
    judge the prompt; a question is not stored."""
    proj = _proj(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    sid = "s-decide-real"
    _run("prompt_json", {"session_id": sid, "cwd": str(proj), "prompt": "what is the alias store made of these days?"})
    _run("prompt_json", {"session_id": sid, "cwd": str(proj), "prompt": "From now on always use sqlite for the alias store."})
    contents = [e.get("content", "") for e in common.load_project_memory(str(proj)).get("entries", [])]
    assert "DECISION: (from user) From now on always use sqlite for the alias store" in contents
    assert not any("what is the alias store" in c for c in contents)


def _rule_project(tmp_path: Path, monkeypatch, deny: bool = False) -> tuple[Path, str]:
    store_mod = pytest.importorskip("windvane.store")
    proj = _proj(tmp_path, "ruled")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    s = store_mod.MemoryStore(str(tmp_path / "store"))
    det = {"tools": ["Bash"], "command": r"\brm\s+-rf\b", "note": "rm -rf"}
    if deny:
        det["unattended"] = "deny"
    ok, msg = s.add_rule(str(proj), "No recursive deletes without asking", reason="test", detector=det)
    assert ok, msg
    return proj, msg.split("id=")[-1].strip()


def test_a_rule_with_a_detector_is_injected_and_recorded_through_the_hooks(tmp_path: Path, monkeypatch):
    proj, rid = _rule_project(tmp_path, monkeypatch)
    sid = "s-compliance"
    store = tmp_path / "store"
    base = {"session_id": sid, "cwd": str(proj), "hook_event_name": "PreToolUse", "tool_name": "Bash", "permission_mode": "bypassPermissions"}
    out = _run("pre_bash_json", dict(base, tool_use_id="tu1", tool_input={"command": "rm -rf build"}))
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse" and "permissionDecision" not in hso
    assert "windvane-rule" in hso["additionalContext"] and rid in hso["additionalContext"]
    assert "windvane-rule" not in _ctx(_run("pre_bash_json", dict(base, tool_use_id="tu2", tool_input={"command": "cat build/log.txt"})))
    _run("pre_bash_json", dict(base, tool_use_id="tu1", tool_input={"command": "rm -rf build"}))
    matches = _state(store, sid)["compliance"]["matches"]
    assert len(matches) == 1 and matches[0]["verdict"] == "unattended"
    _run("post_batch_json", {"session_id": sid, "cwd": str(proj), "permission_mode": "bypassPermissions", "tool_calls": [
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf build"}, "tool_use_id": "tu1", "tool_response": {"stdout": ""}},
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf other"}, "tool_use_id": "tu9", "tool_response": {"stdout": ""}}]})
    assert [m["tool_use_id"] for m in _state(store, sid)["compliance"]["matches"]] == ["tu1", "tu9"]
    out = _run("pre_bash_json", dict(base, tool_use_id="tu3", agent_id="agent-1", tool_input={"command": "rm -rf sub"}))
    assert out == {}
    assert any(m.get("subagent") for m in _state(store, sid)["compliance"]["matches"])


def test_an_ask_first_rule_denies_only_in_autonomy_mode(tmp_path: Path, monkeypatch):
    proj, _rid = _rule_project(tmp_path, monkeypatch, deny=True)
    sid = "s-deny"
    base = {"session_id": sid, "cwd": str(proj), "tool_name": "Bash", "permission_mode": "bypassPermissions"}
    out = _run("pre_bash_json", dict(base, tool_use_id="d1", tool_input={"command": "rm -rf build"}))
    assert "permissionDecision" not in out["hookSpecificOutput"] and "windvane-rule" in _ctx(out)
    monkeypatch.setenv("WINDVANE_AUTONOMY", "1")
    out = _run("pre_bash_json", dict(base, tool_use_id="d2", tool_input={"command": "rm -rf build"}))
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny" and "refused by rule" in hso["permissionDecisionReason"]
    assert _state(tmp_path / "store", sid)["compliance"]["matches"][-1]["verdict"] == "denied"
    assert _run("pre_bash_json", dict(base, tool_use_id="d3", tool_input={"command": "ls"})).get("hookSpecificOutput", {}).get("permissionDecision") is None


def test_a_milestone_task_close_and_a_plan_approval_inject_at_once(tmp_path: Path, monkeypatch):
    proj = _proj(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(proj))
    sid = "s-ms"
    out = _run("post_milestone_json", {"session_id": sid, "tool_name": "ExitPlanMode", "tool_input": {}})
    assert _ctx(out)
    # A task close waits for its turn to end: the save usually follows the
    # TaskUpdate in the same turn. The nudge comes with the next prompt when
    # the turn ended without one.
    out = _run("post_milestone_json", {"session_id": sid, "tool_name": "TaskUpdate", "tool_input": {"status": "completed", "subject": "wire the CLI"}})
    assert "wire the CLI" not in _ctx(out)
    time.sleep(0.01)
    _run("stop_json", {"session_id": sid, "cwd": str(proj), "hook_event_name": "Stop", "last_assistant_message": "Still waiting.", "stop_hook_active": False})
    out = _run("prompt_json", {"session_id": sid, "cwd": str(proj), "hook_event_name": "UserPromptSubmit", "prompt": "go on"})
    assert "wire the CLI" in _ctx(out) and "marked a task done" in _ctx(out)

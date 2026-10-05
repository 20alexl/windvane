"""windvane.stall: strikes that count effect, not activity, and the halt.

The reference failure (2026-09-09): a /goal loop ran nine turns of `cat`
on a file that was never going to change. /goal's own stall rule never
fired because every turn used a tool; the ladder judges a turn by what it
CHANGED. Strikes are counted in every session; the strike text and the
halt are autonomy mode only.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from windvane import stall as st


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path: Path):
    for k in list(os.environ):
        if k.startswith("WINDVANE_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    # A test must never start a daemon: it would outlive the temp store.
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    # The plugin's userConfig is read from Claude Code's settings; point it
    # at an empty dir so a real setting never leaks into a test.
    cfg = tmp_path / "claude-config"
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))


@pytest.fixture
def autonomy(monkeypatch):
    monkeypatch.setenv("WINDVANE_AUTONOMY", "1")


def _no_fp(_project_dir):
    return None


def _cat_turn(state, turn_no, fp=_no_fp):
    st.note_tool(state, "Bash", {"command": "cat f"}, {"stdout": "same"})
    return st.close_turn(state, turn_no, "", fingerprint=fp)


def _edit_turn(state, turn_no, fp=_no_fp):
    st.note_tool(state, "Edit", {"file_path": "a.py"}, {})
    return st.close_turn(state, turn_no, "", fingerprint=fp)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

BASH_CASES = {
    "cat foo.py": False,
    "grep -rn x .": False,
    "git status --porcelain": False,
    "git log --oneline | head": False,
    "git diff HEAD": False,
    "ls -la": False,
    "sed -n 1,5p f": False,
    "python -c 'print(1)'": False,
    "venv/Scripts/python.exe -m pytest tests/": False,
    "venv/Scripts/python.exe tests/bench_x.py 2>&1 | tail -3": False,
    "cmd 2>&1 | head": False,
    "x > /dev/null 2>&1": False,
    "sed -i 's/a/b/' f": True,
    "echo hi > out.txt": True,
    "cat a >> b": True,
    "git commit -m x": True,
    "git add -A && git commit -m x": True,
    "python build.py": True,
    "python -m windvane.report": True,
    "mv a b": True,
    "trash old.txt": True,
    "pip install foo": True,
    "mkdir -p x/y": True,
    "tee out.log": True,
}


@pytest.mark.parametrize("cmd,mutates", list(BASH_CASES.items()))
def test_shell_classification(cmd, mutates):
    assert st.bash_mutates(cmd) is mutates


def test_tool_classification():
    c = st.classify_tool
    assert c("Edit", {"file_path": "a"}, {}) == "file"
    assert c("Write", {"file_path": "a"}, {}) == "file"
    assert c("NotebookEdit", {}, {}) == "file"
    assert c("Edit", {}, {"is_error": True}) == "none"
    assert c("Read", {"file_path": "a"}, {}) == "none"
    assert c("Grep", {"pattern": "x"}, {}) == "none"
    assert c("Monitor", {}, {}) == "park"
    assert c("ScheduleWakeup", {}, {}) == "park"
    assert c("Bash", {"command": "sleep 100", "run_in_background": True}, {}) == "park"
    assert c("Agent", {"prompt": "x"}, {}) == "delegate"
    assert c("Bash", {"command": "cat f"}, {"stdout": "x"}) == "none"
    assert c("Bash", {"command": "git commit -m x"}, {"stdout": "ok"}) == "commit"
    assert c("Bash", {"command": "sed -i s/a/b/ f"}, {"stdout": ""}) == "file"
    assert c("Bash", {"command": "sed -i s/a/b/ f"}, {"is_error": True}) == "none"
    assert c("Bash", {"command": "venv/Scripts/python.exe tests/bench_x.py"}, {"stdout": "ALL PASS"}) == "test"
    assert c("mcp__someserver__create_object", {}, {}) == "file"
    assert c("mcp__docs__get", {}, {}) == "none"


def test_every_windvane_tool_records_and_its_reads_do_not():
    c = st.classify_tool
    assert c("mcp__windvane__checkpoint", {"operation": "save"}, {}) == "record"
    assert c("mcp__windvane__checkpoint", {"operation": "checkpoint_save"}, {}) == "record"
    assert c("mcp__windvane__checkpoint", {}, {}) == "record"
    assert c("mcp__windvane__checkpoint", {"operation": "restore"}, {}) == "none"
    assert c("mcp__windvane__checkpoint", {"operation": "list"}, {}) == "none"
    assert c("mcp__windvane__memory", {"operation": "recall"}, {}) == "none"
    assert c("mcp__windvane__memory", {"operation": "remember"}, {}) == "record"
    assert c("mcp__windvane__log", {"operation": "decision"}, {}) == "record"
    assert c("mcp__windvane__mine", {"operation": "search"}, {}) == "none"
    assert c("mcp__windvane__mine", {"operation": "status"}, {}) == "none"


def test_records_are_kept_as_name_and_operation():
    state: dict = {}
    st.note_tool(state, "mcp__windvane__memory", {"operation": "remember", "content": "State banked: next step is the loader"})
    st.note_tool(state, "mcp__windvane__checkpoint", {"operation": "save", "task_description": "t"})
    st.note_tool(state, "mcp__windvane__checkpoint", {})
    turn = state["stall"]["turn"]
    assert turn["records"] == ["memory:remember", "checkpoint:save", "checkpoint"]
    assert turn.get("remember_state") is True
    state2: dict = {}
    st.note_tool(state2, "mcp__windvane__memory", {"operation": "remember", "content": "The pace is 0.15 because of the access policy"})
    assert not state2["stall"]["turn"].get("remember_state")


def test_test_outcomes():
    to = st.test_outcome
    assert to("python tests/bench_x.py", {"stdout": "...\nALL PASS\n"}) is True
    assert to("pytest -q", {"text": "3 passed, 1 failed"}) is False
    assert to("pytest -q", {"text": "12 passed in 0.3s"}) is True
    assert to("python tests/bench_x.py", {"stdout": "  [FAIL] x\nFAILED: 2"}) is False
    assert to("cat f", {"stdout": "12 passed"}) is None
    assert to("pytest -q", {"stdout": "collecting..."}) is None


def test_a_test_invocation_is_judged_on_what_runs():
    assert st._looks_like_test("cat scratchpad/goal-test/out.txt") is False
    assert st._looks_like_test("grep -rn foo tests/") is False
    assert st._looks_like_test("venv/scripts/python.exe tests/bench_x.py 2>&1 | tail -1") is True
    assert st._looks_like_test("python -m pytest -q") is True
    assert st._looks_like_test("cargo test --release") is True
    assert st._looks_like_test("npm test") is True
    assert st._looks_like_test("cd /e/x && .venv/scripts/python.exe -m pytest") is True


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


def test_the_ladder_in_autonomy_mode(autonomy):
    state: dict = {}
    assert [_cat_turn(state, i) for i in range(1, 3)] == ["noeffect", "noeffect"]
    assert st.stall_state(state)["strikes"] == 0 and st.stall_state(state)["pending"] is None
    _cat_turn(state, 3)
    s = st.stall_state(state)
    assert s["strikes"] == 1
    assert isinstance(s["pending"], dict) and s["pending"]["strike"] == 1
    assert s["noeffect_streak"] == 0
    text, changed = st.nudge(state)
    assert changed and "<windvane-stall>" in text and "Strike 1 of 3" in text and "3 turns with tool use and no effect" in text
    assert "Monitor" in text and "park" in text
    assert st.nudge(state) == ("", False)
    for i in range(4, 7):
        _cat_turn(state, i)
    assert st.stall_state(state)["strikes"] == 2
    text, _ = st.nudge(state, bearings=["CHECKPOINT [manual]: the task", "Rules (2):", "  [abc] rule"])
    assert "Strike 2 of 3" in text and "Bearings check" in text
    assert "CHECKPOINT [manual]" in text and "Rules (2)" in text
    for i in range(7, 10):
        _cat_turn(state, i)
    assert st.stall_state(state)["strikes"] == 3
    text, _ = st.nudge(state)
    assert "Strike 3 of 3" in text and "halts" in text and "operation save" in text
    for i in range(10, 13):
        _cat_turn(state, i)
    assert st.stall_state(state)["strikes"] == 3
    assert st.stall_state(state)["max_strikes"] == 3
    assert [e["turn"] for e in st.stall_state(state)["events"] if e["kind"] == "strike"] == [3, 6, 9, 12]


def test_an_attended_session_counts_strikes_but_gets_no_stall_text():
    state: dict = {}
    for i in range(1, 4):
        _cat_turn(state, i)
    s = st.stall_state(state)
    assert s["strikes"] == 1 and isinstance(s["pending"], dict)
    text, changed = st.nudge(state, bearings=["CHECKPOINT [manual]: x"])
    assert text == "" and changed is True, "the staged strike is dropped, the state changed"
    assert st.stall_state(state)["pending"] is None
    assert st.nudge(state) == ("", False)
    for i in range(4, 10):
        _cat_turn(state, i)
    assert st.nudge(state)[0] == ""
    s = st.stall_state(state)
    assert s["strikes"] == 3 and s["max_strikes"] == 3
    assert [e["turn"] for e in s["events"] if e["kind"] == "strike"] == [3, 6, 9], "every strike is still an event for the report"
    assert st.summary(state)["autonomy"] is False


def test_a_running_goal_turns_the_stall_text_on():
    state: dict = {"run": {"auto": {"status": "running"}}}
    for i in range(1, 4):
        _cat_turn(state, i)
    text, _ = st.nudge(state)
    assert "Strike 1 of 3" in text


def test_neutral_turns_are_transparent():
    state: dict = {}
    _cat_turn(state, 1)
    _cat_turn(state, 2)
    assert st.close_turn(state, 3, "", fingerprint=_no_fp) == "neutral"  # no tools at all
    assert st.stall_state(state)["noeffect_streak"] == 2
    st.note_tool(state, "Monitor", {}, {})
    st.note_tool(state, "Bash", {"command": "cat f"}, {"stdout": ""})
    assert st.close_turn(state, 4, "", fingerprint=_no_fp) == "neutral", "a parked turn is neutral even with a read in it"
    assert st.stall_state(state)["strikes"] == 0
    _cat_turn(state, 5)
    assert st.stall_state(state)["strikes"] == 1
    assert st.stall_state(state)["turns"]["neutral"] == 2
    st.note_tool(state, "Agent", {"prompt": "build it"}, {})
    assert st.close_turn(state, 6, "", fingerprint=_no_fp) == "good"


def test_strikes_decay_and_never_go_below_zero():
    state: dict = {}
    for i in range(1, 7):
        _cat_turn(state, i)
    assert st.stall_state(state)["strikes"] == 2
    st.nudge(state)
    for i in range(7, 11):
        _edit_turn(state, i)
    assert st.stall_state(state)["strikes"] == 2
    _edit_turn(state, 11)
    s = st.stall_state(state)
    assert s["strikes"] == 1 and s["good_streak"] == 0
    for i in range(12, 17):
        _edit_turn(state, i)
    assert st.stall_state(state)["strikes"] == 0
    for i in range(17, 23):
        _edit_turn(state, i)
    assert st.stall_state(state)["strikes"] == 0
    assert [e["turn"] for e in st.stall_state(state)["events"] if e["kind"] == "decay"] == [11, 16]
    # A no-effect turn in the middle resets the good streak.
    for i in range(23, 26):
        _cat_turn(state, i)
    assert st.stall_state(state)["strikes"] == 1
    for i in range(26, 29):
        _edit_turn(state, i)
    _cat_turn(state, 29)
    for i in range(30, 33):
        _edit_turn(state, i)
    assert st.stall_state(state)["strikes"] == 1
    assert st.stall_state(state)["good_streak"] == 3
    assert st.stall_state(state)["last_change_at"] > 0


def test_a_test_run_is_progress_only_when_its_status_flips():
    state: dict = {}
    cmd = {"command": "venv/Scripts/python.exe tests/bench_x.py"}

    def run(turn, out):
        st.note_tool(state, "Bash", cmd, {"stdout": out})
        return st.close_turn(state, turn, "", fingerprint=_no_fp)

    assert run(1, "FAILED: 2") == "good"  # unknown -> fail
    assert run(2, "FAILED: 2") == "noeffect"  # re-run and hope
    assert run(3, "FAILED: 2") == "noeffect"
    assert run(4, "ALL PASS") == "good"
    assert run(5, "ALL PASS") == "noeffect"
    other = {"command": "venv/Scripts/python.exe tests/bench_other.py"}
    st.note_tool(state, "Bash", other, {"stdout": "ALL PASS"})
    assert st.close_turn(state, 6, "", fingerprint=_no_fp) == "neutral", "a different test run with the same status is verification"
    third = {"command": "venv/Scripts/python.exe -m pytest tests/ -q"}
    st.note_tool(state, "Bash", third, {"stdout": "40 passed"})
    assert st.close_turn(state, 7, "", fingerprint=_no_fp) == "neutral"
    assert st.stall_state(state)["strikes"] == 0 and st.stall_state(state)["noeffect_streak"] == 1
    st.note_tool(state, "Bash", third, {"stdout": "40 passed"})
    assert st.close_turn(state, 8, "", fingerprint=_no_fp) == "noeffect"
    st.note_tool(state, "Bash", third, {"stdout": "40 passed"})
    assert st.close_turn(state, 9, "", fingerprint=_no_fp) == "noeffect" and st.stall_state(state)["strikes"] == 1
    st.note_tool(state, "Bash", {"command": "venv/Scripts/python.exe -m pytest tests/slow -q"}, {"stdout": "collecting ... (no verdict in tail)"})
    assert st.close_turn(state, 10, "", fingerprint=_no_fp) == "neutral"


def test_the_tree_fingerprint_rescues_a_mutation_the_heuristic_missed():
    state: dict = {}
    fps = iter(["A", "A", "B", "B"])

    def fp(_):
        return next(fps)

    st.note_tool(state, "Bash", {"command": "cat f"}, {"stdout": ""})
    assert st.close_turn(state, 1, "", fingerprint=fp) == "noeffect", "the first reading is not an effect"
    st.note_tool(state, "Bash", {"command": "cat f"}, {"stdout": ""})
    assert st.close_turn(state, 2, "", fingerprint=fp) == "noeffect"
    st.note_tool(state, "Bash", {"command": "cat out"}, {"stdout": ""})
    assert st.close_turn(state, 3, "", fingerprint=fp) == "good"
    assert "tree" in st.stall_state(state)["last_effects"]
    _edit_turn(state, 4)
    assert st.stall_state(state)["tree_fingerprint"] is None, "a flagged effect invalidates the stored reading"
    st.note_tool(state, "Bash", {"command": "cat f"}, {"stdout": ""})
    assert st.close_turn(state, 5, "", fingerprint=fp) == "noeffect"


def _git(repo: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, env=env, stdin=subprocess.DEVNULL)


def test_the_real_fingerprint(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    try:
        _git(repo, "init", "-q")
        (repo / "a.txt").write_bytes(b"one\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "-m", "first")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git is not usable here")
    first = st.tree_fingerprint(str(repo))
    assert isinstance(first, str) and len(first) == 40
    (repo / "a.txt").write_bytes(b"two\n")
    assert st.tree_fingerprint(str(repo)) != first
    assert st.tree_fingerprint(str(tmp_path / "no-such-dir")) is None
    assert st.tree_fingerprint("") is None


def test_the_reference_case_nine_cat_turns():
    state: dict = {}
    for i in range(1, 10):
        st.note_tool(state, "Bash", {"command": "cat scratchpad/goal-test/out.txt"}, {"stdout": "line 1\n"})
        st.close_turn(state, i, "", fingerprint=_no_fp)
    s = st.stall_state(state)
    assert s["strikes"] == 3
    assert [e["turn"] for e in s["events"]] == [3, 6, 9]
    summ = st.summary(state)
    assert summ["max_strikes"] == 3 and summ["turns"]["noeffect"] == 9


def test_events_are_capped(monkeypatch):
    monkeypatch.setenv("WINDVANE_STALL_TURNS", "1")
    state: dict = {}
    for i in range(1, 80):
        _cat_turn(state, i)
    assert len(st.stall_state(state)["events"]) == st.EVENTS_KEEP
    assert st.stall_state(state)["events"][-1]["turn"] == 79


def test_the_knobs_come_from_the_project_config(tmp_path: Path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".windvane").mkdir(parents=True)
    (proj / ".windvane" / "config.json").write_text(json.dumps({"stall_turns": 2, "strike_cap": 2, "stall_decay": 1}), encoding="utf-8")
    assert (st.stall_turns(str(proj)), st.strike_cap(str(proj)), st.decay_turns(str(proj))) == (2, 2, 1)
    assert (st.stall_turns(), st.strike_cap(), st.decay_turns()) == (st.STALL_TURNS, st.STRIKE_CAP, st.DECAY_GOOD_TURNS)
    state: dict = {}
    for i in range(1, 7):
        st.note_tool(state, "Bash", {"command": "cat f"}, {"stdout": ""})
        st.close_turn(state, i, str(proj), fingerprint=_no_fp)
    s = st.stall_state(state)
    assert s["strikes"] == 2 and [e["turn"] for e in s["events"]] == [2, 4, 6]
    st.note_tool(state, "Edit", {"file_path": "a.py"}, {})
    st.close_turn(state, 7, str(proj), fingerprint=_no_fp)
    assert st.stall_state(state)["strikes"] == 1, "one good turn decays with stall_decay 1"
    monkeypatch.setenv("WINDVANE_STRIKE_CAP", "0")
    assert st.strike_cap() == st.STRIKE_CAP, "a non-positive value falls back to the default"
    monkeypatch.setenv("WINDVANE_STRIKE_CAP", "5")
    assert st.strike_cap(str(proj)) == 5, "the environment wins over the project file"
    monkeypatch.setenv("WINDVANE_AUTONOMY", "1")
    assert "Strike 1 of 5" in st.strike_text(1, 3, st.stall_state({}), project_dir=str(proj))


# ---------------------------------------------------------------------------
# The halt
# ---------------------------------------------------------------------------


def test_the_halt_arms_only_in_autonomy_mode_at_the_cap_once(monkeypatch):
    state: dict = {}
    for i in range(1, 10):
        _cat_turn(state, i)
    assert st.stall_state(state)["strikes"] == 3
    assert st.maybe_halt(state, 9) is False and st.halted(state) is None
    monkeypatch.setenv("WINDVANE_AUTONOMY", "1")
    assert st.autonomy_on()
    assert st.maybe_halt(state, 9) is True
    h = st.halted(state)
    assert h is not None and h["turn"] == 9 and h["strikes"] == 3 and h["denied"] == 0
    assert st.stall_state(state)["events"][-1]["kind"] == "halt"
    assert st.maybe_halt(state, 10) is False
    state2: dict = {}
    for i in range(1, 6):
        _cat_turn(state2, i)
    assert st.maybe_halt(state2, 5) is False, "below the cap, no halt even in autonomy mode"

    r = st.deny_reason(state, "Read")
    assert "python -m windvane.stall release" in r and "checkpoint tool with operation save" in r and "PushNotification" in r and "Denied: Read" in r
    t = st.halt_text(state, "abc-123")
    assert t.startswith("<windvane-halt>") and t.index("checkpoint tool with operation save") < t.index("PushNotification") and "Session abc-123" in t
    st.note_denied(state)
    assert st.halted(state)["denied"] == 1
    ev_before = len(st.stall_state(state)["events"])
    _cat_turn(state, 10)
    assert len(st.stall_state(state)["events"]) == ev_before, "turns after the halt are not more strikes"
    st.note_tool(state, "Bash", {"command": "cat f"}, {"stdout": ""})
    assert st.close_turn(state, 11, "", fingerprint=_no_fp) == "halted"

    assert st.release(state, "test") is True and st.halted(state) is None
    assert st.stall_state(state)["strikes"] == 0
    assert st.stall_state(state)["events"][-1]["kind"] == "release"
    assert st.release(state) is False
    s = st.summary(state)
    assert s["autonomy"] is True and s["halted"] is None


def test_the_halt_leaves_a_record_open():
    assert {"PushNotification", "mcp__windvane__checkpoint", "ToolSearch", "SendMessage"} == set(st.HALT_ALLOWED_TOOLS)
    assert "still open" in st._halt_cause({"reason": "turn cap", "turn": 150})
    assert "for 9 turns" in st._halt_cause({"strikes": 3, "turn": 9})


def test_the_cli_reports_and_releases_a_halt(capsys):
    common = pytest.importorskip("windvane.events.common", reason="windvane.events.common is the parent agent's module")
    sid = "s-stall-cli"
    common._session_id = sid
    state = common.load_state()
    st.stall_state(state)["halted"] = {"at": time.time(), "turn": 2, "strikes": 2, "denied": 2}
    st.stall_state(state)["strikes"] = 2
    common.save_state(state)
    assert st.main(["status", sid]) == 0
    out = capsys.readouterr().out
    assert '"halted"' in out and '"denied": 2' in out
    assert st.main(["release", sid]) == 0
    assert "released" in capsys.readouterr().out
    common._session_id = sid
    after = common.load_state()
    assert after["stall"].get("halted") is None and after["stall"]["strikes"] == 0
    assert st.main(["release", sid]) == 1
    assert "not halted" in capsys.readouterr().out
    assert st.main(["bogus"]) == 2


# ---------------------------------------------------------------------------
# The run report reads the same state
# ---------------------------------------------------------------------------


def test_the_run_report_has_a_stalls_section(tmp_path: Path):
    pytest.importorskip("windvane.storage", reason="windvane.storage is the parent agent's module")
    from windvane import report as rr

    state = {"run": {"started_at": time.time() - 60, "start_commit": "abc"}, "pressure": {"stops_total": 9}}
    for i in range(1, 10):
        st.note_tool(state, "Bash", {"command": "cat f"}, {"stdout": ""})
        st.close_turn(state, i, "", fingerprint=_no_fp)
    proj = tmp_path / "rr-proj"
    proj.mkdir()
    r = rr.collect("s-rr", str(proj), state)
    assert isinstance(r["stalls"], dict) and r["stalls"]["max_strikes"] == 3
    assert not any("stall" in n for n in r["not_measured"])
    md = rr.render_md(r)
    assert "## Stalls (3 strikes at peak, 3 at end)" in md
    assert re.search(r"\| 9 \| strike \| 3 \|", md) is not None
    state["stall"]["halted"] = {"at": time.time(), "turn": 2, "strikes": 2, "denied": 2}
    md = rr.render_md(rr.collect("s-rr", str(proj), state))
    assert "**HALTED** at turn 2" in md and "2 tool call(s) denied" in md

"""windvane.goal: the bracket around Claude Code's own /goal loop.

A goal is set only by typing /goal; windvane watches the transcript and
keeps the run record in step. The record shapes are the ones observed on a
headless run (Claude Code 2.1.268).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from windvane import goal as ar
from windvane import stall as st


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path: Path):
    for k in list(os.environ):
        if k.startswith("WINDVANE_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    cfg = tmp_path / "claude-config"
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))


# --- transcript records in the observed shapes -----------------------------


def _ts(i: int) -> str:
    return f"2026-09-10T22:46:{i:02d}.000Z"


def rec_sentinel(cond: str, i: int) -> str:
    return json.dumps({"type": "attachment", "timestamp": _ts(i), "attachment": {"type": "goal_status", "met": False, "sentinel": True, "condition": cond}})


def rec_verdict(cond: str, i: int, met: bool, reason: str = "because", failed: bool = False) -> str:
    att = {"type": "goal_status", "met": met, "condition": cond, "reason": reason, "iterations": 1, "durationMs": 900, "tokens": 1200}
    if failed:
        att["failed"] = True
    return json.dumps({"type": "attachment", "timestamp": _ts(i), "attachment": att})


def rec_command(args: str, i: int) -> str:
    content = f"<command-name>/goal</command-name>\n            <command-message>goal</command-message>\n            <command-args>{args}</command-args>"
    return json.dumps({"type": "user", "timestamp": _ts(i), "message": {"role": "user", "content": content}})


def rec_noise(i: int) -> str:
    return json.dumps({"type": "assistant", "timestamp": _ts(i), "message": {"role": "assistant", "content": [{"type": "text", "text": "working; the goal is near"}]}})


def _goal_rec(i: int, cond: str, **att) -> str:
    a = {"type": "goal_status", "condition": cond, "met": False, **att}
    return json.dumps({"type": "attachment", "timestamp": f"2026-09-11T17:{i:02d}:00.000Z", "attachment": a})


def _write(path: Path, *lines: str) -> str:
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return str(path)


# ---------------------------------------------------------------------------
# scan_goal
# ---------------------------------------------------------------------------


def test_scan_goal_reads_the_observed_records(tmp_path: Path):
    t = tmp_path / "t.jsonl"
    sentinel = {"type": "attachment", "timestamp": "2026-09-10T22:45:54.205Z",
                "attachment": {"type": "goal_status", "met": False, "sentinel": True, "condition": "tests pass"}}
    verdict = {"type": "attachment", "timestamp": "2026-09-10T22:46:09.000Z",
               "attachment": {"type": "goal_status", "met": True, "condition": "tests pass", "reason": "pytest exited 0"}}
    clear = {"type": "user", "timestamp": "2026-09-10T22:46:11.161Z",
             "message": {"role": "user", "content": "<command-name>/goal</command-name>\n<command-message>goal</command-message>\n<command-args>clear</command-args>"}}
    t.write_text(json.dumps(sentinel) + "\n", encoding="utf-8")
    s = ar.scan_goal(str(t))
    assert s["active"] and s["condition"] == "tests pass"
    t.write_text(json.dumps(sentinel) + "\n" + json.dumps(verdict) + "\n", encoding="utf-8")
    assert ar.scan_goal(str(t))["ended"] == "met"
    t.write_text(json.dumps(sentinel) + "\n" + json.dumps(clear) + "\n", encoding="utf-8")
    assert ar.scan_goal(str(t))["ended"] == "cleared"
    assert ar.scan_goal(str(tmp_path / "missing.jsonl"))["seen"] is False


def test_scan_goal_over_every_record_shape(tmp_path: Path):
    t = tmp_path / "t1.jsonl"
    assert ar.scan_goal(str(tmp_path / "missing.jsonl"))["seen"] is False
    _write(t, rec_noise(1))
    assert ar.scan_goal(str(t))["seen"] is False
    _write(t, rec_noise(1), rec_sentinel("tests pass", 2), rec_noise(3))
    s = ar.scan_goal(str(t))
    assert s["seen"] and s["active"] and s["condition"] == "tests pass" and s["set_at"] == _ts(2) and s["ended"] is None
    _write(t, rec_sentinel("tests pass", 2), rec_verdict("tests pass", 4, False, "no test output yet"))
    s = ar.scan_goal(str(t))
    assert s["active"] and s["verdicts"] == 1 and s["last_reason"] == "no test output yet" and s["last_met"] is False
    _write(t, rec_sentinel("tests pass", 2), rec_verdict("tests pass", 4, False), rec_verdict("tests pass", 6, True, "pytest exited 0"))
    s = ar.scan_goal(str(t))
    assert s["ended"] == "met" and not s["active"] and s["verdicts"] == 2
    _write(t, rec_sentinel("fly", 2), rec_verdict("fly", 4, False, "impossible", failed=True))
    s = ar.scan_goal(str(t))
    assert s["ended"] == "failed" and not s["active"]
    _write(t, rec_sentinel("tests pass", 2), rec_verdict("tests pass", 4, False), rec_command("clear", 5))
    s = ar.scan_goal(str(t))
    assert s["ended"] == "cleared" and not s["active"]
    for w in ("stop", "off", "reset", "none", "cancel", "CLEAR"):
        _write(t, rec_sentinel("g", 2), rec_command(w, 3))
        assert ar.scan_goal(str(t))["ended"] == "cleared", w
    _write(t, rec_command("clear", 1), rec_sentinel("g", 2))
    assert ar.scan_goal(str(t))["active"] is True, "a clear BEFORE the sentinel does not end it"
    # A tool_result echoing the command text (a fixture read back) is a list,
    # not a slash command.
    echo = json.dumps({"type": "user", "timestamp": _ts(3), "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "<command-name>/goal</command-name>\n<command-args>clear</command-args>"}]}})
    _write(t, rec_sentinel("g", 2), echo)
    assert ar.scan_goal(str(t))["active"] is True
    _write(t, rec_sentinel("first", 2), rec_verdict("first", 3, True), rec_sentinel("second", 5))
    s = ar.scan_goal(str(t))
    assert s["active"] and s["condition"] == "second" and s["set_at"] == _ts(5) and s["verdicts"] == 0
    _write(t, rec_command("the file exists", 1), rec_noise(2))
    assert ar.scan_goal(str(t))["seen"] is False, "a /goal command without a sentinel is not a goal (the sentinel is the record)"


def test_a_verdict_whose_sentinel_left_the_tail_still_reads_as_active(tmp_path: Path):
    big = tmp_path / "big.jsonl"
    filler = json.dumps({"type": "assistant", "message": {"content": "x" * 5000}})
    _write(big, rec_sentinel("old goal", 1), *([filler] * 30), rec_verdict("old goal", 40, False, "still going"))
    s = ar.scan_goal(str(big), tail_bytes=20_000)
    assert s["seen"] and s["active"] and s["condition"] == "old goal"


def test_a_met_record_with_the_sentinel_flag_ends_the_goal(tmp_path: Path):
    """2.1.268 writes the met record as {met: true, sentinel: true} with no
    reason. Read as a new set, it started a phantom run that counted three
    days of ordinary turns to the cap and halted a session with no goal
    (2026-09-14)."""
    t = tmp_path / "t.jsonl"
    t.write_text(_goal_rec(31, "land the branches", sentinel=True) + "\n" + _goal_rec(47, "land the branches", sentinel=True, met=True) + "\n", encoding="utf-8")
    s = ar.scan_goal(str(t))
    assert s["seen"] and s["active"] is False and s["ended"] == "met" and s["verdicts"] == 1
    t.write_text(_goal_rec(31, "land the branches", sentinel=True) + "\n", encoding="utf-8")
    state: dict = {}
    ev = ar.observe(state, str(t), str(tmp_path), turn=True)
    assert ev and ev["event"] == "started"
    t.write_text(_goal_rec(31, "land the branches", sentinel=True) + "\n" + _goal_rec(47, "land the branches", sentinel=True, met=True) + "\n", encoding="utf-8")
    ev = ar.observe(state, str(t), str(tmp_path), turn=True)
    assert ev and ev["event"] == "ended" and ev["auto"]["status"] == "met"
    assert not (state.get("stall") or {}).get("halted")


def test_recent_edit_files_reads_the_transcript(tmp_path: Path):
    t = tmp_path / "t.jsonl"

    def tu(name, fp):
        return json.dumps({"type": "assistant", "timestamp": "2026-09-11T10:00:00.000Z",
                           "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": name, "input": {"file_path": fp}}]}})

    t.write_text("\n".join([tu("Read", "/w/p/readme.md"), tu("Edit", "/w/p/a.py"), tu("Write", "/w/p/b.py"), tu("Edit", "/w/p/a.py")]) + "\n", encoding="utf-8")
    assert ar.recent_edit_files(str(t)) == ["/w/p/b.py", "/w/p/a.py"]
    assert ar.recent_edit_files(str(tmp_path / "missing.jsonl")) == []
    assert ar.recent_edit_files("") == []


# ---------------------------------------------------------------------------
# observe
# ---------------------------------------------------------------------------


def test_observe_arms_and_ends_a_goal_run(tmp_path: Path):
    t = tmp_path / "t.jsonl"
    sentinel = {"type": "attachment", "timestamp": "2026-09-10T22:45:54.205Z",
                "attachment": {"type": "goal_status", "met": False, "sentinel": True, "condition": "g"}}
    t.write_text(json.dumps(sentinel) + "\n", encoding="utf-8")
    state: dict = {}
    ev = ar.observe(state, str(t), str(tmp_path), turn=True)
    assert ev and ev["event"] == "started" and ar.running(state)
    assert state["run"]["auto"]["turns"] == 1
    assert ar.env_or_state_autonomy(state) is True
    t.write_text(json.dumps(sentinel) + "\n" + json.dumps({"type": "attachment", "timestamp": "2026-09-10T22:46:09.000Z",
                 "attachment": {"type": "goal_status", "met": True, "condition": "g", "reason": "done"}}) + "\n", encoding="utf-8")
    state["run"]["transcript_path"] = str(t)
    from windvane import repo_state

    assert repo_state.goal_for_session(state) == "g"  # running: every checkpoint carries it
    ev = ar.observe(state, str(t), str(tmp_path), turn=True)
    assert ev and ev["event"] == "ended" and state["run"]["auto"]["status"] == "met"
    assert ar.env_or_state_autonomy(state) is False
    # A met goal is not stamped on later checkpoints.
    assert "goal" not in state["run"]
    assert repo_state.goal_for_session(state) == ""


def test_the_run_record_follows_the_goal(tmp_path: Path):
    t = tmp_path / "t2.jsonl"
    state: dict = {}
    _write(t, rec_noise(1))
    assert ar.observe(state, str(t), str(tmp_path), turn=True) is None and ar.auto(state) is None and not st.autonomy_on(state)
    _write(t, rec_noise(1), rec_sentinel("tests pass", 2))
    ev = ar.observe(state, str(t), str(tmp_path), turn=False)
    a = ar.auto(state)
    assert ev and ev["event"] == "started" and a["status"] == "running" and a["goal"] == "tests pass"
    assert "pending_text" not in a, "the launcher-era directive is gone"
    assert state["run"]["goal"] == "tests pass"
    assert st.autonomy_on(state), "autonomy mode is on from the state alone"
    assert a["max_turns"] == ar.DEFAULT_TURN_CAP
    assert ar.observe(state, str(t), str(tmp_path), turn=False) is None and a["turns"] == 0
    assert ar.observe(state, str(t), str(tmp_path), turn=True) is None and a["turns"] == 1
    _write(t, rec_sentinel("tests pass", 2), rec_verdict("tests pass", 4, False, "no output yet"))
    assert ar.observe(state, str(t), str(tmp_path), turn=True) is None and a["verdicts"] == 1 and a["last_reason"] == "no output yet"
    _write(t, rec_sentinel("tests pass", 2), rec_verdict("tests pass", 4, False), rec_verdict("tests pass", 6, True, "pytest exited 0"))
    ev = ar.observe(state, str(t), str(tmp_path), turn=True)
    assert ev and ev["event"] == "ended" and a["status"] == "met" and "pytest exited 0" in a["why"] and not st.autonomy_on(state)
    assert ar.observe(state, str(t), str(tmp_path), turn=True) is None and a["status"] == "met", "the same goal does not restart"
    s = ar.summary(state)
    assert s and "duration_s" in s and s["goal"] == "tests pass"


def test_cleared_failed_and_replaced(tmp_path: Path):
    t = tmp_path / "t3.jsonl"
    state: dict = {}
    _write(t, rec_sentinel("g", 2))
    ar.observe(state, str(t), str(tmp_path))
    _write(t, rec_sentinel("g", 2), rec_command("clear", 3))
    ev = ar.observe(state, str(t), str(tmp_path))
    assert ev and ev["event"] == "ended" and ar.auto(state)["status"] == "cleared"
    state = {}
    _write(t, rec_sentinel("fly", 2))
    ar.observe(state, str(t), str(tmp_path))
    _write(t, rec_sentinel("fly", 2), rec_verdict("fly", 3, False, "impossible", failed=True))
    assert ar.observe(state, str(t), str(tmp_path))["event"] == "ended" and ar.auto(state)["status"] == "failed"
    state = {}
    _write(t, rec_sentinel("one", 2))
    ar.observe(state, str(t), str(tmp_path))
    _write(t, rec_sentinel("one", 2), rec_sentinel("two", 5))
    ev = ar.observe(state, str(t), str(tmp_path))
    assert ev and ev["event"] == "started" and ar.auto(state)["goal"] == "two" and ev["replaced"]["status"] == "cleared"


def test_a_run_whose_goal_left_the_transcript_tail_ends_without_a_halt(tmp_path: Path):
    t = tmp_path / "t.jsonl"
    t.write_text(_goal_rec(31, "g", sentinel=True) + "\n", encoding="utf-8")
    state: dict = {}
    ar.observe(state, str(t), str(tmp_path), turn=True)
    a = ar.auto(state)
    assert a and a["status"] == "running"
    a["turns"] = a["max_turns"] - 1  # one turn from the cap
    t.write_text(json.dumps({"type": "user", "message": {"content": "just chatting"}}) + "\n", encoding="utf-8")
    ev = ar.observe(state, str(t), str(tmp_path), turn=True)
    assert ev and ev["event"] == "ended" and ev["auto"]["status"] == "cleared"
    assert not (state.get("stall") or {}).get("halted"), "a goal that cannot be seen never arms the halt"


def test_the_strike_halt_and_the_turn_cap_end_the_run(tmp_path: Path, monkeypatch):
    t = tmp_path / "t4.jsonl"
    state: dict = {"stall": {"halted": {"turn": 4, "strikes": 3, "denied": 0}}}
    _write(t, rec_sentinel("g", 2))
    ar.observe(state, str(t), str(tmp_path))
    ev = ar.observe(state, str(t), str(tmp_path), turn=True)
    assert ev and ev["event"] == "ended" and ar.auto(state)["status"] == "halted"
    monkeypatch.setenv("WINDVANE_GOAL_TURN_CAP", "2")
    state = {}
    _write(t, rec_sentinel("never", 2))
    ar.observe(state, str(t), str(tmp_path))
    assert ar.auto(state)["max_turns"] == 2
    assert ar.observe(state, str(t), str(tmp_path), turn=True) is None
    ev = ar.observe(state, str(t), str(tmp_path), turn=True)
    h = st.halted(state)
    assert ev and ev["event"] == "ended" and ar.auto(state)["status"] == "capped"
    assert h and h.get("reason") == "turn cap" and state["stall"].get("pending_halt") is True
    assert st.stall_state(state)["events"][-1]["kind"] == "halt"
    assert "turn cap" in st.deny_reason(state, "Bash") and "/goal clear" in st.deny_reason(state, "Bash")
    assert "turn cap" in st.halt_text(state, "s")


def test_the_end_alert_texts():
    for status, needle in (("met", "goal met"), ("failed", "impossible"), ("cleared", "cleared"), ("capped", "/goal clear"), ("halted", "halted")):
        assert needle in ar.end_alert_text({"status": status, "turns": 3, "goal": "g", "max_turns": 5, "why": ""}, "abcdef12"), status
    assert "python -m windvane.stall release" in ar.end_alert_text({"status": "capped", "max_turns": 5}, "s")
    assert ar.end_alert_text({"status": "met", "turns": 3, "goal": "g"}, "abcdef1234").startswith("goal met in session abcdef12 ")


def test_the_turn_cap_from_config_and_env(tmp_path: Path, monkeypatch):
    proj = tmp_path / "p"
    (proj / ".windvane").mkdir(parents=True)
    (proj / ".windvane" / "config.json").write_text('{"goal_turn_cap": 7}', encoding="utf-8")
    assert ar.turn_cap(str(proj)) == 7
    monkeypatch.setenv("WINDVANE_GOAL_TURN_CAP", "3")
    assert ar.turn_cap(str(proj)) == 3
    assert ar.turn_cap(str(tmp_path / "none")) == 3
    monkeypatch.delenv("WINDVANE_GOAL_TURN_CAP")
    assert ar.turn_cap(str(tmp_path / "none")) == ar.DEFAULT_TURN_CAP
    (proj / ".windvane" / "config.json").write_text('{"goal_turn_cap": 0}', encoding="utf-8")
    assert ar.turn_cap(str(proj)) == ar.DEFAULT_TURN_CAP, "a non-positive cap falls back to the default"


def test_autonomy_from_the_knob_and_no_recursion(monkeypatch):
    assert ar.env_or_state_autonomy(None) is False
    assert ar.env_or_state_autonomy({}) is False
    assert ar.env_or_state_autonomy({"run": {"auto": {"status": "running"}}}) is True
    assert ar.running({"run": {"auto": {"status": "met"}}}) is False
    monkeypatch.setenv("WINDVANE_AUTONOMY", "1")
    assert ar.env_or_state_autonomy(None) is True and st.autonomy_on({}) is True


# ---------------------------------------------------------------------------
# The manifest and the report
# ---------------------------------------------------------------------------


def test_a_start_writes_the_manifest(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    t = tmp_path / "t.jsonl"
    _write(t, rec_sentinel("count reaches three", 2))
    state: dict = {}
    ev = ar.observe(state, str(t), str(proj), turn=True)
    assert ev and ev["event"] == "started"
    p = ar.write_start_manifest(str(proj), "s-goal-manifest", ev["auto"])
    assert p is not None and p.parent == proj / ".windvane" / "runs" and p.name.endswith("-s-goal-m.manifest.json")
    m = json.loads(p.read_text(encoding="utf-8"))
    assert m["mode"] == "goal" and m["goal"] == "count reaches three" and m["max_turns"] == ar.DEFAULT_TURN_CAP
    assert m["session_id"] == "s-goal-manifest" and m["start_commit"] == "" and isinstance(m["rules"], list)
    assert p.read_bytes().endswith(b"}\n") and b"\r\n" not in p.read_bytes()


def test_the_manifest_names_the_start_commit(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    try:
        for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "first"]):
            subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git is not usable here")
    head = ar._git_head(str(repo))
    assert len(head) >= 7
    assert ar._git_head(str(tmp_path / "missing")) == ""


def test_the_manifest_snapshots_the_rules_in_scope(tmp_path: Path, monkeypatch):
    storage = pytest.importorskip("windvane.storage", reason="windvane.storage is the parent agent's module")
    pm = {"entries": [
        {"id": "r1", "category": "rule", "content": "No rm", "detector": {"tools": ["Bash"], "command": r"\brm\b", "note": "rm"}},
        {"id": "r2", "category": "rule", "content": "Be direct"},
    ]}
    monkeypatch.setattr(storage, "load_project_memory", lambda _p: pm)
    snap = ar._rules_snapshot(str(tmp_path))
    assert snap == [{"id": "r1", "rule": "No rm", "detector": True, "note": "rm"},
                    {"id": "r2", "rule": "Be direct", "detector": False, "note": ""}]


def test_the_run_report_has_the_goal_run_section(tmp_path: Path):
    pytest.importorskip("windvane.storage", reason="windvane.storage is the parent agent's module")
    from windvane import report as rr

    proj = tmp_path / "proj"
    proj.mkdir()
    t = tmp_path / "t.jsonl"
    _write(t, rec_sentinel("count reaches three", 2))
    state: dict = {"run": {"started_at": time.time() - 60}}
    ar.observe(state, str(t), str(proj), turn=True)
    _write(t, rec_sentinel("count reaches three", 2), rec_verdict("count reaches three", 5, True, "the count printed 3"))
    ar.observe(state, str(t), str(proj), turn=True)
    md = rr.render_md(rr.collect("s-goal-report", str(proj), state))
    assert "## Goal run (met)" in md and "count reaches three" in md and "of the 150 cap" in md

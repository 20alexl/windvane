"""Context pressure, milestones, the usage budget, the run report, repo state,
alerts and the process census: the measured facts as unit checks."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from windvane import alerts, milestones as ms, pressure as cp, procs, repo_state, report as rr

ROOT = Path(__file__).resolve().parent.parent

_KNOB_ENV = (
    "WINDVANE_OUTPUT_RESERVE", "WINDVANE_CHECKPOINT_MARGIN", "WINDVANE_HEADSUP_FRACTION",
    "WINDVANE_CHECKPOINT_CADENCE", "WINDVANE_BUDGET_FIVE_HOUR_PCT", "WINDVANE_BUDGET_SEVEN_DAY_PCT",
    "WINDVANE_BUDGET_PCT", "WINDVANE_ALERT_COMMAND", "WINDVANE_GIT_TRACE",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path: Path):
    for k in ("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_CODE_SESSION_ID", *_KNOB_ENV):
        monkeypatch.delenv(k, raising=False)
    # Never the real store, never a daemon for a temp store (it outlives the
    # temp dir and idles 30 minutes), never the person's own settings or
    # managed policy (a real autoCompactWindow would move every number here).
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(cp, "_managed_dir", lambda: tmp_path / "no-managed")
    monkeypatch.setattr(cp, "_managed_registry", lambda: {})
    # The report looks a transcript up under Claude's projects dir: an empty
    # one here, never the person's own sessions.
    try:
        from windvane.mining import jsonl_reader as _jr

        monkeypatch.setattr(_jr, "_get_claude_projects_dir", lambda: tmp_path / "claude_projects")
    except ImportError:
        pass


def _payload(sid, used, window=1_000_000, model="Claude Fable 5.1"):
    return {
        "session_id": sid,
        "model": {"id": "claude-fable-5-1", "display_name": model},
        "context_window": {
            "total_input_tokens": used,
            "context_window_size": window,
            "used_percentage": 100.0 * used / window,
        },
        "cost": {"total_cost_usd": 1.25},
        "cwd": "/w/proj",
    }


# ---------------------------------------------------------------------------
# Where compaction fires, and the bands
# ---------------------------------------------------------------------------


def test_checkpoint_band_sits_above_the_measured_compaction():
    # 2026-09-10: a 750K setting compacted at 717,578 tokens.
    th = cp.thresholds(1_000_000, 750_000)
    assert th["trigger_at"] == 718_000
    assert th["checkpoint_at"] == 698_000
    assert th["checkpoint_at"] < 717_578 < th["trigger_at"] + 1_000
    assert th["headsup_at"] == 650_000


def test_small_window_bands_stay_ordered():
    th = cp.thresholds(200_000, 200_000)
    assert th["headsup_at"] < th["checkpoint_at"] < th["trigger_at"] < 200_000


@pytest.mark.parametrize(
    "raw,want",
    [
        (200000, 200000), ("200000", 200000), ("500k", 500_000), ("500K", 500_000),
        ("1M", 1_000_000), (200, 200_000), ("750", 750_000), (None, None),
        (True, None), ("", None), ("auto", None),
    ],
)
def test_parse_window_value_accepts_every_documented_form(raw, want):
    assert cp.parse_window_value(raw) == want


def test_compaction_point_precedence_and_defaults(tmp_path: Path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    assert cp.compaction_point(1_000_000, str(proj)) == (967_000, "model-default")
    assert cp.compaction_point(200_000, str(proj)) == (200_000, "model-default")

    (proj / ".claude" / "settings.json").write_text(json.dumps({"autoCompactWindow": "500k"}), encoding="utf-8")
    assert cp.compaction_point(1_000_000, str(proj)) == (500_000, "settings")

    # Managed (enterprise) settings outrank every project and user file;
    # drop-ins in managed-settings.d/ merge alphabetically, later wins.
    managed = tmp_path / "managed"
    (managed / "managed-settings.d").mkdir(parents=True)
    monkeypatch.setattr(cp, "_managed_dir", lambda: managed)
    (managed / "managed-settings.json").write_text(json.dumps({"autoCompactWindow": "600k"}), encoding="utf-8")
    assert cp.compaction_point(1_000_000, str(proj)) == (600_000, "managed")
    (managed / "managed-settings.d" / "10-base.json").write_text(json.dumps({"autoCompactWindow": "620k"}), encoding="utf-8")
    (managed / "managed-settings.d" / "20-team.json").write_text(json.dumps({"autoCompactWindow": "650k"}), encoding="utf-8")
    assert cp.compaction_point(1_000_000, str(proj)) == (650_000, "managed")
    monkeypatch.setattr(cp, "_managed_registry", lambda: {"autoCompactWindow": 700000})
    assert cp.compaction_point(1_000_000, str(proj)) == (700_000, "managed")
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "800000")
    assert cp.compaction_point(1_000_000, str(proj)) == (800_000, "env")
    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    monkeypatch.setattr(cp, "_managed_dir", lambda: tmp_path / "no-managed")
    monkeypatch.setattr(cp, "_managed_registry", lambda: {})

    (proj / ".claude" / "settings.local.json").write_text(json.dumps({"autoCompactWindow": 300}), encoding="utf-8")
    assert cp.compaction_point(1_000_000, str(proj)) == (300_000, "settings")
    assert cp.compaction_point(200_000, str(proj)) == (200_000, "settings")  # capped at the window

    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "750000")
    assert cp.compaction_point(1_000_000, str(proj)) == (750_000, "env")
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "2000000")
    assert cp.compaction_point(1_000_000, str(proj)) == (1_000_000, "env")
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "500")  # below the documented minimum
    assert cp.compaction_point(1_000_000, str(proj))[1] == "settings"
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "banana")
    assert cp.compaction_point(1_000_000, str(proj))[1] == "settings"
    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW")

    # A background job (`claude --bg`) saves its launch flags; the flag sits
    # between the env var and the settings files. The job dir is the first
    # eight characters of the session id, but the file's own session id decides.
    jobs = tmp_path / "cfg" / "jobs"
    sid = "3ba90f62-0000-4000-8000-000000000001"
    other = "3ba90f62-0000-4000-8000-000000000002"
    job = jobs / sid[:8]
    job.mkdir(parents=True)
    (job / "state.json").write_text(json.dumps({
        "sessionId": sid, "resumeSessionId": sid,
        "respawnFlags": ["-n", "worker", "--autocompact", "200k", "--effort", "medium", "--model", "opus"],
        "providerEnv": {},
    }), encoding="utf-8")
    assert cp.job_autocompact(sid) == 200_000
    assert cp.job_autocompact("") is None
    assert cp.compaction_point(1_000_000, str(proj), sid) == (200_000, "launch flag")
    assert cp.compaction_point(1_000_000, str(proj)) == (300_000, "settings")
    assert cp.compaction_point(1_000_000, str(proj), other) == (300_000, "settings")
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "750000")
    assert cp.compaction_point(1_000_000, str(proj), sid) == (750_000, "env")
    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    (job / "state.json").write_text(json.dumps({"sessionId": sid, "respawnFlags": ["--autocompact=150k", "--model", "opus"]}), encoding="utf-8")
    assert cp.compaction_point(1_000_000, str(proj), sid) == (150_000, "launch flag")
    (job / "state.json").write_text(json.dumps({"sessionId": sid, "respawnFlags": ["--model", "claude-fable-5-1"]}), encoding="utf-8")
    assert cp.compaction_point(1_000_000, str(proj), sid) == (300_000, "settings")
    # A resumed job whose dir is named for an older id: found by scanning.
    moved = jobs / "aaaaaaaa"
    moved.mkdir(parents=True)
    (moved / "state.json").write_text(json.dumps({
        "sessionId": "aaaaaaaa-0000-4000-8000-000000000003", "resumeSessionId": other,
        "respawnFlags": ["--autocompact", "180k"],
    }), encoding="utf-8")
    assert cp.compaction_point(1_000_000, str(proj), other) == (180_000, "launch flag")
    (job / "state.json").write_text("{not json", encoding="utf-8")
    assert cp.compaction_point(1_000_000, str(proj), sid) == (300_000, "settings")


def test_thresholds_and_their_knobs(tmp_path: Path, monkeypatch):
    th = cp.thresholds(1_000_000, 967_000)
    assert th["headsup_at"] == 867_000 and th["checkpoint_at"] == 915_000
    th = cp.thresholds(200_000, 200_000)
    assert th["trigger_at"] == 168_000 and th["checkpoint_at"] == 158_000  # 10K margin
    assert th["headsup_at"] == 148_000 and th["headsup_at"] < th["checkpoint_at"]
    monkeypatch.setenv("WINDVANE_OUTPUT_RESERVE", "40000")
    assert cp.thresholds(1_000_000, 750_000)["trigger_at"] == 710_000
    monkeypatch.setenv("WINDVANE_CHECKPOINT_MARGIN", "30000")
    assert cp.thresholds(1_000_000, 750_000)["checkpoint_at"] == 680_000
    monkeypatch.setenv("WINDVANE_OUTPUT_RESERVE", "junk")  # nonsense -> default
    monkeypatch.setenv("WINDVANE_CHECKPOINT_MARGIN", "-5")
    assert cp.thresholds(1_000_000, 750_000)["checkpoint_at"] == 698_000
    monkeypatch.setenv("WINDVANE_HEADSUP_FRACTION", "0.2")
    assert cp.thresholds(1_000_000, 750_000)["headsup_at"] == 550_000
    monkeypatch.setenv("WINDVANE_HEADSUP_FRACTION", "2")  # not a fraction -> default
    assert cp.thresholds(1_000_000, 750_000)["headsup_at"] == 650_000
    for k in ("WINDVANE_OUTPUT_RESERVE", "WINDVANE_CHECKPOINT_MARGIN", "WINDVANE_HEADSUP_FRACTION"):
        monkeypatch.delenv(k)
    # The project's own config file is a layer too.
    proj = tmp_path / "proj"
    (proj / ".windvane").mkdir(parents=True)
    (proj / ".windvane" / "config.json").write_text(json.dumps({"output_reserve": 40000}), encoding="utf-8")
    assert cp.thresholds(1_000_000, 750_000, str(proj))["trigger_at"] == 710_000
    assert cp.thresholds(1_000_000, 750_000)["trigger_at"] == 718_000


def test_the_mirror_round_trips():
    p = cp.record_statusline(_payload("s-1", 123_456))
    assert p is not None and p.name == "s-1.ctx.json"
    m = cp.read_mirror("s-1")
    assert m is not None and m["total_input_tokens"] == 123_456 and m["context_window_size"] == 1_000_000
    assert abs(time.time() - m["ts"]) < 5
    assert cp.record_statusline({"context_window": {}}) is None
    assert cp.read_mirror("nope") is None and cp.read_mirror("") is None


def test_assessment_bands(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "750000")
    a = cp.assess({"total_input_tokens": 600_000, "context_window_size": 1_000_000, "ts": time.time()})
    assert a["band"] == "clear" and a["distance"] == 150_000
    assert cp.assess({"total_input_tokens": 660_000, "context_window_size": 1_000_000})["band"] == "headsup"
    assert cp.assess({"total_input_tokens": 725_000, "context_window_size": 1_000_000})["band"] == "checkpoint"
    assert cp.assess({"total_input_tokens": 0, "context_window_size": 1_000_000})["band"] == "nodata"
    assert cp.assess(None)["band"] == "nodata"


def test_a_mirror_the_mod_marked_early_opens_the_checkpoint_band(monkeypatch):
    """The mod's early_compaction row opens its band below the margin and
    says so in the mirror; the engine's nudge fires there and names the
    row. Without the mark the same fill is clear."""
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "750000")
    plain = {"total_input_tokens": 400_000, "context_window_size": 1_000_000}
    assert cp.assess(plain)["band"] == "clear"
    a = cp.assess({**plain, "early_band": "40%"})
    assert a["band"] == "checkpoint" and a["early"] == "40%"
    text = cp.checkpoint_text(a)
    assert text.startswith("<windvane-context>CHECKPOINT NOW: the early_compaction setting (40%)")
    assert "turn boundary after the save compacts" in text and "318K tokens to the auto-compaction trigger" in text
    assert "early_compaction" not in cp.checkpoint_text(cp.assess({**plain, "total_input_tokens": 725_000}))


def test_nudges_latch_once_per_band_per_compaction_cycle(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "750000")
    sid = "s-nudge"
    state: dict = {"last_session_start": time.time()}

    cp.record_statusline(_payload(sid, 400_000))
    t, ch = cp.nudge(state, sid)
    assert t == "" and not ch

    cp.record_statusline(_payload(sid, 660_000))
    t, ch = cp.nudge(state, sid)
    assert "Context pressure" in t and "Compaction #1 is coming" in t and ch
    assert "~698K" in t
    assert cp.nudge(state, sid) == ("", False)

    cp.record_statusline(_payload(sid, 700_000))
    t, ch = cp.nudge(state, sid)
    assert t.startswith("<windvane-context>CHECKPOINT NOW") and ch
    assert "18K tokens to the auto-compaction trigger (~718K; the 750K setting minus the output reserve)" in t
    assert "checkpoint tool" in t and "operation save" in t
    assert cp.nudge(state, sid)[0] == ""

    # Compaction. The mirror still holds 700K until the statusline re-runs.
    time.sleep(0.02)
    cp.note_compaction(state)
    a = cp.current_assessment(state, sid)
    assert a["band"] == "nodata" and "predates" in a["reason"]
    assert cp.nudge(state, sid)[0] == ""
    r = cp.rhythm_text(state, sid)
    assert r.startswith("Compaction #1.") and "checkpoint at ~698K" in r
    assert "auto-compaction at ~718K (the 750K env setting minus the output reserve)" in r

    time.sleep(0.02)
    cp.record_statusline(_payload(sid, 120_000))
    assert cp.nudge(state, sid)[0] == ""
    cp.record_statusline(_payload(sid, 660_000))
    assert "Compaction #2 is coming" in cp.nudge(state, sid)[0]
    cp.record_statusline(_payload(sid, 730_000))
    assert "CHECKPOINT NOW" in cp.nudge(state, sid)[0]

    # Skipping straight into the checkpoint band latches heads-up too.
    state2: dict = {"last_session_start": time.time()}
    cp.record_statusline(_payload("s-jump", 740_000))
    t, _ = cp.nudge(state2, "s-jump")
    assert "CHECKPOINT NOW" in t and "Context pressure:" not in t
    cp.record_statusline(_payload("s-jump", 745_000))
    assert cp.nudge(state2, "s-jump")[0] == ""


def test_the_fallback_cadence_and_the_deliberate_save_reset(monkeypatch):
    assert cp.CADENCE_STOPS >= 50  # a long fuse, not a schedule
    monkeypatch.setenv("WINDVANE_CHECKPOINT_CADENCE", "3")
    sid = "s-cadence"
    state: dict = {"last_session_start": time.time()}
    cp.record_statusline(_payload(sid, 100_000))
    for _ in range(2):
        cp.note_stop(state)
    assert cp.nudge(state, sid)[0] == ""
    cp.note_stop(state)
    t, ch = cp.nudge(state, sid)
    assert "Checkpoint fallback: 3 turns" in t and ch
    assert cp.nudge(state, sid)[0] == ""
    for _ in range(3):
        cp.note_stop(state)
    assert "Checkpoint fallback" in cp.nudge(state, sid)[0]
    for _ in range(2):
        cp.note_stop(state)
    cp.note_manual_checkpoint(state)
    cp.note_stop(state)
    assert cp.nudge(state, sid)[0] == "" and state["pressure"]["stops_since_checkpoint"] == 1
    # A band nudge takes the slot; the cadence waits.
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "750000")
    for _ in range(3):
        cp.note_stop(state)
    cp.record_statusline(_payload(sid, 660_000))
    t, _ = cp.nudge(state, sid)
    assert "Context pressure" in t and "cadence" not in t.lower()


def test_a_configured_but_silent_statusline_is_announced_once(tmp_path: Path):
    proj = tmp_path / "proj-nr"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": "x"}}), encoding="utf-8")
    assert cp.nudge({"last_session_start": time.time() - 10}, "s-silent", str(proj))[0] == ""
    state = {"last_session_start": time.time() - cp.NOT_RECORDING_AFTER_SECS - 1}
    t, ch = cp.nudge(state, "s-silent", str(proj))
    assert "no context reading has arrived" in t and ch
    assert "python -m windvane.pressure statusline" in t
    assert cp.nudge(state, "s-silent", str(proj))[0] == ""
    assert cp.session_start_text(str(proj)) == ""
    bare = tmp_path / "proj-bare"
    bare.mkdir()
    assert "no statusLine configured" in cp.session_start_text(str(bare))


def test_the_statusline_cli_records_and_prints(tmp_path: Path):
    store = tmp_path / "store"
    env = dict(os.environ, WINDVANE_DIR=str(store), CLAUDE_CODE_AUTO_COMPACT_WINDOW="750000", PYTHONPATH=str(ROOT))
    run = lambda args, stdin="": subprocess.run(  # noqa: E731
        [sys.executable, "-m", "windvane.pressure", *args], input=stdin,
        capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=60,
    )
    r = run(["statusline"], json.dumps(_payload("s-cli", 600_000)))
    assert r.returncode == 0
    assert "ctx 600K/1000K" in r.stdout and "compact at 750K (150K left)" in r.stdout
    assert (store / "sessions" / "s-cli.ctx.json").is_file()
    r = run(["assess", "s-cli"])
    assert r.returncode == 0 and '"band": "clear"' in r.stdout
    assert run(["statusline"], "not json").returncode == 0  # a statusline must keep rendering
    r = run(["bogus"])
    assert r.returncode == 2 and "python -m windvane.pressure" in r.stdout


def test_a_background_jobs_autocompact_flag_moves_the_nudges(tmp_path: Path, monkeypatch):
    """A session launched `claude --bg ... --autocompact 200k` on a 1M model:
    the flag is not in a hook's environment, so the nudges were keyed on the
    model default (~967K) and compaction at 200K came first. The job's saved
    respawnFlags carry the flag; the assessment reads it."""
    cfg = tmp_path / "cfg"
    sid = "51c9a4bf-0000-4000-8000-000000000001"
    job = cfg / "jobs" / sid[:8]
    job.mkdir(parents=True)
    (job / "state.json").write_bytes(json.dumps({
        "sessionId": sid, "resumeSessionId": sid,
        "respawnFlags": ["-n", "worker", "--autocompact", "200k", "--effort", "medium", "--model", "opus"],
        "providerEnv": {},
    }).encode())
    mirror = {"session_id": sid, "total_input_tokens": 175_000, "context_window_size": 1_000_000, "ts": 1.0}

    a = cp.assess(mirror, str(tmp_path / "proj"))
    assert (a["point"], a["source"]) == (200_000, "launch flag")
    assert a["band"] == "checkpoint"  # 175K sits inside the last band above the 200K trigger
    assert "launch flag" in cp.checkpoint_text(a) or "launch flag" in cp.headsup_text(a, 0)

    plain = dict(mirror, session_id="00000000-0000-4000-8000-000000000009")
    b = cp.assess(plain, str(tmp_path / "proj"))
    assert (b["point"], b["source"], b["band"]) == (967_000, "model-default", "clear")

    (job / "state.json").write_bytes(json.dumps({"sessionId": sid, "respawnFlags": ["--model", "claude-fable-5-1"]}).encode())
    c = cp.assess(mirror, str(tmp_path / "proj"))
    assert (c["point"], c["source"]) == (967_000, "model-default")

    (job / "state.json").write_bytes(json.dumps({"sessionId": sid, "respawnFlags": ["--autocompact", "200k"]}).encode())
    monkeypatch.setattr(cp, "read_mirror", lambda _sid: mirror if _sid == sid else None)
    assert "200K launch flag setting" in cp.rhythm_text({}, sid, str(tmp_path / "proj"))


def test_one_compaction_opens_one_cycle_whichever_hook_runs_first():
    state: dict = {}
    cp.note_compaction(state)
    cp.note_compaction(state)
    assert cp.pressure_state(state)["cycle"] == 1
    cp.pressure_state(state)["compacted_at"] -= 600
    cp.note_compaction(state)
    assert cp.pressure_state(state)["cycle"] == 2
    cp.note_restored(state, {"kind": "manual", "task_id": "task_1", "summary": "step 1 banked"})
    assert cp.pressure_state(state)["compactions"][-1]["restored"]["task_id"] == "task_1"


def test_the_measured_auto_compaction_replayed(monkeypatch):
    """Replay of a 2026-09-10 auto compaction (1M window, autoCompactWindow
    750K): the heads-up fired at 651K, the old checkpoint band (720K) never
    did because Claude Code compacted at 717,578."""
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "750000")
    sid = "s-replay-0910"
    state: dict = {"last_session_start": time.time()}
    cp.record_statusline(_payload(sid, 651_000))
    assert "Compaction #1 is coming" in cp.nudge(state, sid)[0]
    cp.record_statusline(_payload(sid, 700_000))
    assert cp.nudge(state, sid)[0].startswith("<windvane-context>CHECKPOINT NOW")
    cp.record_statusline(_payload(sid, 717_578))
    a = cp.current_assessment(state, sid)
    assert a["band"] == "checkpoint" and a["checkpoint_at"] < 717_578
    assert cp.nudge(state, sid)[0] == ""  # once per cycle


def test_a_mod_written_mirror_is_read_like_the_statuslines(tmp_path: Path, monkeypatch):
    store = tmp_path / "store"
    (store / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "750000")
    sid = "aaaaaaaa-0000-4000-8000-00000000000a"
    rec = {
        "session_id": sid, "ts": 1.0, "source": "mod", "plugin": "windvane",
        "total_input_tokens": 660_000, "context_window_size": 1_000_000, "used_percentage": 66,
        "model_id": "claude-x", "model_name": "claude-x", "total_cost_usd": 1.5,
        "five_hour_pct": 9, "five_hour_resets_at": 1_790_000_000.0,
    }
    (store / "sessions" / f"{sid}.ctx.json").write_bytes(json.dumps(rec).encode())
    (store / "sessions" / f"{sid}.mod").write_bytes(b'{"plugin": "windvane"}')

    a = cp.assess(cp.read_mirror(sid), str(tmp_path / "proj"))
    assert a["band"] == "headsup" and a["point"] == 750_000 and a["mirror_source"] == "mod"
    assert cp.mod_present(sid) and not cp.mod_present("")
    # No statusLine anywhere, yet the banner stays quiet: the mod is the signal.
    assert cp.session_start_text(str(tmp_path / "proj"), sid) == ""
    assert "no statusLine" in cp.session_start_text(str(tmp_path / "proj"), "bbbbbbbb-0000-4000-8000-00000000000b")
    # While the marker is fresh, the statusline leaves the mirror alone; once
    # it is stale, the statusline writes again.
    cp.record_statusline({"session_id": sid, "context_window": {"total_input_tokens": 1, "context_window_size": 1_000_000}})
    assert json.loads((store / "sessions" / f"{sid}.ctx.json").read_bytes())["total_input_tokens"] == 660_000
    old = time.time() - cp.MOD_MARKER_FRESH_SECS - 5
    os.utime(store / "sessions" / f"{sid}.mod", (old, old))
    cp.record_statusline({"session_id": sid, "context_window": {"total_input_tokens": 1, "context_window_size": 1_000_000}})
    assert json.loads((store / "sessions" / f"{sid}.ctx.json").read_bytes())["total_input_tokens"] == 1


def test_the_brief_marker_is_fresh_for_two_minutes(tmp_path: Path):
    (tmp_path / "store" / "sessions").mkdir(parents=True)
    sid = "aaaaaaaa-0000-4000-8000-00000000000c"
    assert not cp.compaction_briefed(sid)
    marker = cp.brief_marker_path(sid)
    marker.write_bytes(b'{"plugin": "windvane", "ts": 1.0}')
    assert cp.compaction_briefed(sid) and not cp.compaction_briefed("")
    old = time.time() - cp.BRIEF_MARKER_FRESH_SECS - 5
    os.utime(marker, (old, old))
    assert not cp.compaction_briefed(sid)


def test_checkpoint_text_and_cadence_say_the_recorder_drafted_the_record():
    a = {"trigger_at": 168_000, "used": 149_000, "point": 200_000}
    for text in (cp.checkpoint_text(a), cp.cadence_text(60)):
        assert "the checkpoint tool with operation save and no other argument accepts it" in text
        assert "one field amends that field" in text and "TaskUpdate" in text and "what is next" in text
        assert "task_description," not in text and "handoff_summary" not in text
    assert cp.checkpoint_text(a).startswith(
        "<windvane-context>CHECKPOINT NOW: 19K tokens to the auto-compaction trigger (~168K; the 200K setting"
    )
    assert "60 turns" in cp.cadence_text(60)


def test_setpoint_notice_names_the_number_for_this_model(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "750000")
    a = cp.assess({"total_input_tokens": 10_000, "context_window_size": 200_000})
    assert cp.setpoint_mismatch(a) and a["capped"]
    assert "`/autocompact 150k`" in cp.setpoint_text(a)
    monkeypatch.delenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    b = cp.assess({"total_input_tokens": 10_000, "context_window_size": 200_000})
    assert cp.setpoint_mismatch(b) and "no autoCompactWindow is set" in cp.setpoint_text(b)
    c = cp.assess({"total_input_tokens": 10_000, "context_window_size": 1_000_000})
    assert not cp.setpoint_mismatch(c)


# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------

_CLAIMS = [
    "Phase 1 is built, verified, and committed locally.",
    "Step 3 done. Next is the run report.",
    "All 60 checks pass.",
    "60/60 pass, pyright clean.",
    "That closes part A of the plan.",
    "The migration landed and tests are green.",
    "Finished the refactor of the scorer module.",
    "Phase 2 wrapped up; moving to phase 3.",
    "Milestone reached: the report module is in place.",
    "Goal met: done.txt contains ok and check.py prints PASS.",
    "Run 3 of 3 done and the loop is stopped.",
    "Phase 1 is built and verified.",
    "The Phase 2 delta audit is done.",
    "Batch 2 has landed: B is now `5686345a`.",
    "The docs agent's first round is complete: ten commits, whole tree green at 9,754.",
    "I completed step 3 of the plan.",
    "All four follow-up branches are merged on the source line.",
]
_NOT_CLAIMS = [
    "Is phase 1 done?",
    "Phase 2 is not done yet.",
    "Step 4 is done so far, but the tests aren't.",
    "When step 3 is complete I'll move on.",
    "This will be done once the tests pass.",
    "I'm still working on step 2.",
    "Let me read the file.",
    "Reading the plan section now.",
    "Here is the plan for phase 2: build the report.",
    "Done.",
    "The feature is half done.",
    "Waiting on you for the push.",
    "Committed as f3a61b0.",
    "I'll mark the task complete after you confirm.",
    "The step needs to be verified before it is done.",
    "The goal is not met yet; two benches still fail.",
    # Talk ABOUT completion (an instruction, a prediction, a quote of the
    # prediction), not claims of it.
    "Exit after it says the goal is met.",
    "After the third line lands, the next verdict should say met, and the goal clears itself.",
    'The second nudge fired on another of my sentences, "the next verdict should say met, and the goal clears itself," which is a prediction.',
    "Run the suite until phase 2 is green.",
    "Your step 3 is done when the file exists.",
    "Run 2 of 3 done; one more to go.",
    # Restated history with a unit noun hiding in a hyphenated compound, and
    # a quoted sentence discussed under a bold header.
    "Checkpoint restored. It matches where we are, with one commit behind: it names `8748488` as latest, and `ef0bb83` (the pushback and follow-plan rules) landed after it was saved.",
    '**Milestone classifier: restated history reads as a fresh claim.** "X landed after it was saved" fired a nudge this turn.',
    "Phase 1 was already built last session; today is phase 2.",
    "The migration had landed before this run started.",
    "As of the previous commit the step is complete, nothing new here.",
    'The harness printed "step 2 is done", which is the quote we discuss.',
    # Shapes that are not a close: a table row, a sentence in motion,
    # reported speech, a rule, a price, a count, a participle adjective, an
    # instruction, a partial.
    "| Options round | Merged.",
    "B is at `8b4420d8` and its whole test tree is about 18% through, with 15 minutes to go.",
    "It repeats that the rung is stopped and Part 1 is done.",
    "So the first row passes only if momentum beats its benchmark.",
    "NVDA closed at 217.55 on the expiry, so the tracked strikes fall on both sides.",
    "The last whole-tree run took 357 s (8 workers, 11,132 passed).",
    "The merged Phase 2 tree exceeded the ten-minute window and is running in the background.",
    "Say which packages, and I brief them.",
    "The daily-only store closes part of that.",
]


@pytest.mark.parametrize("text", _CLAIMS)
def test_a_close_is_a_claim(text):
    s, q = ms.classify_completion(text, use_semantic=False)
    assert s >= ms.THRESHOLD and q
    assert ms.is_completion_claim(text, use_semantic=False)[0] is True


@pytest.mark.parametrize("text", _NOT_CLAIMS)
def test_talk_about_a_close_is_not_a_claim(text):
    assert ms.classify_completion(text, use_semantic=False)[0] < ms.THRESHOLD
    assert ms.is_completion_claim(text, use_semantic=False)[0] is False


def test_a_claim_inside_a_long_message_is_quoted_and_a_commit_is_not_one():
    long = "I read three files and ran the bench.\n\nStep 2 is complete.\n\nNext I will look at the docs."
    s, q = ms.classify_completion(long, use_semantic=False)
    assert s >= ms.THRESHOLD and q == "Step 2 is complete."
    assert ms.classify_completion("Committed locally as f3a61b0, tree clean.", use_semantic=False)[0] < ms.THRESHOLD


def test_a_weak_match_needs_a_positive_semantic_margin(monkeypatch):
    weak = (
        "The plan we agreed on this morning, with every one of its sub-items "
        "and the extra tests the reviewer wanted, is I think done."
    )
    assert ms._regex_tier(weak) == ms.WEAK
    assert ms.classify_completion(weak, use_semantic=False)[0] < ms.THRESHOLD
    n_c, n_n = len(ms._COMPLETION_TEMPLATES), len(ms._NON_COMPLETION_TEMPLATES)
    monkeypatch.setattr(ms, "_embed_batch", lambda _t: [[1.0, 0.0]] + [[1.0, 0.1]] * n_c + [[0.0, 1.0]] * n_n)
    assert ms.classify_completion(weak)[0] >= ms.THRESHOLD
    monkeypatch.setattr(ms, "_embed_batch", lambda _t: [[1.0, 0.0]] + [[0.0, 1.0]] * n_c + [[1.0, 0.0]] * n_n)
    assert ms.classify_completion(weak)[0] < ms.THRESHOLD
    monkeypatch.setattr(ms, "_embed_batch", lambda _t: [])
    assert ms.classify_completion(weak)[0] < ms.THRESHOLD  # daemon down: weak stays weak


def test_milestone_and_plan_texts():
    t = ms.milestone_text("Phase 1 built")
    assert t.startswith("<windvane-context>Last turn you closed a step") and "checkpoint tool" in t and "operation save" in t
    assert "You marked a task done" in ms.milestone_text("X", "task")
    r = ms.milestone_text("B6 card done.", "claim_remember")
    assert "reads checkpoints only" in r and "operation save" in r
    assert "pending_steps" in ms.plan_text()


def test_a_prose_claim_needs_a_turn_effect_and_bullets_never_count():
    ok = cp._turn_corroborates_a_close
    assert ok({"stall": {"turn": {"effects": ["commit"], "delegated": False}}}, "Track B is built and merged.") is True
    assert ok({"stall": {"turn": {"effects": [], "delegated": True}}}, "Phase 1 built.") is True
    assert ok({"stall": {"turn": {"effects": [], "delegated": False}}}, "Track B is built and merged.") is False
    assert ok({"stall": {"turn": {"effects": ["file"]}}}, "- **The torch kind**: built") is False
    assert ok({}, "step 3 done") is False


def test_nudge_delivery_is_capped_to_one_an_hour(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(cp, "_ring_manual_after", lambda *_a, **_k: False)
    state: dict = {}
    cp.stage_milestone(state, "step one done", "claim", since=time.time() - 10)
    assert "closed a step" in cp.nudge(state, "s-cap-test", str(tmp_path))[0]
    cp.stage_milestone(state, "step two done", "claim", since=time.time() - 5)
    assert "closed a step" not in cp.nudge(state, "s-cap-test", str(tmp_path))[0]  # within the hour
    # A task-tool close is structural and never rate-limited, but it waits
    # for the turn to end: the save usually follows the TaskUpdate.
    cp.stage_milestone(state, "task X", "task", since=time.time() - 5)
    assert cp.nudge(state, "s-cap-test", str(tmp_path))[0] == ""
    assert isinstance(state["pressure"]["milestone_pending"], dict)
    time.sleep(0.01)
    cp.note_stop(state)
    assert "marked a task done" in cp.nudge(state, "s-cap-test", str(tmp_path))[0]
    # A save in the same turn as the TaskUpdate answers it before any nudge.
    cp.stage_milestone(state, "task Y", "task")
    time.sleep(0.01)
    cp.note_manual_checkpoint(state)
    time.sleep(0.01)
    cp.note_stop(state)
    assert cp.nudge(state, "s-cap-test", str(tmp_path))[0] == ""


def test_a_claim_is_staged_at_stop_and_delivered_once(monkeypatch):
    sid = "s-milestone"
    state: dict = {"last_session_start": time.time()}
    cp.record_statusline(_payload(sid, 100_000))
    cp.note_stop(state, "Looking into the failing bench now.")
    assert cp.nudge(state, sid)[0] == "" and state["pressure"]["milestone_pending"] is None
    # A claim in a turn that changed nothing is a status line, not a close.
    cp.note_stop(state, "Phase 1 is built and verified. Next is the run report.")
    assert state["pressure"]["milestone_pending"] is None
    state["stall"] = {"turn": {"effects": ["file"], "tools": 2}}
    cp.note_stop(state, "Phase 1 is built and verified. Next is the run report.")
    assert isinstance(state["pressure"]["milestone_pending"], dict)
    assert state["pressure"]["stops_since_checkpoint"] == 0
    t, ch = cp.nudge(state, sid)
    assert "Last turn you closed a step" in t and "Phase 1 is built" in t and ch
    assert "checkpoint tool" in t and "operation save" in t
    assert cp.nudge(state, sid)[0] == ""
    # One prose nudge an hour: a second claim inside the window is swallowed.
    cp.note_stop(state, "Step 1b is done as well.")
    assert cp.nudge(state, sid)[0] == "" and state["pressure"]["milestone_pending"] is None
    monkeypatch.setattr(cp, "MILESTONE_NUDGE_GAP_SECS", 0)  # the rest tests delivery, not the cap
    time.sleep(0.01)
    cp.note_manual_checkpoint(state)
    cp.note_stop(state, "Step 2 done, checkpoint saved.")
    assert state["pressure"]["milestone_pending"] is None  # banked this turn
    time.sleep(0.01)
    cp.note_stop(state, "Step 3 complete.")
    assert isinstance(state["pressure"]["milestone_pending"], dict)
    time.sleep(0.01)
    cp.note_manual_checkpoint(state)
    assert cp.nudge(state, sid)[0] == ""  # a checkpoint after the claim answers it silently
    cp.stage_milestone(state, "Implement user authentication", "task")
    time.sleep(0.01)
    cp.note_stop(state)  # a task close is delivered once its turn has ended
    t, _ = cp.nudge(state, sid)
    assert "You marked a task done" in t and "Implement user authentication" in t
    # Pressure band outranks the milestone in the same slot; the milestone waits.
    monkeypatch.setenv("CLAUDE_CODE_AUTO_COMPACT_WINDOW", "750000")
    cp.record_statusline(_payload(sid, 660_000))
    cp.stage_milestone(state, "Round 4 closed", "claim")
    t, _ = cp.nudge(state, sid)
    assert "Context pressure" in t and "Round 4" not in t
    assert "Round 4 closed" in cp.nudge(state, sid)[0]


def test_a_save_from_another_process_answers_the_nudge_through_the_ring(tmp_path: Path, monkeypatch):
    common = pytest.importorskip("windvane.events.common", reason="windvane.events.common is another agent's module")
    monkeypatch.setattr(cp, "MILESTONE_NUDGE_GAP_SECS", 0)
    proj = str(tmp_path / "proj")
    sid = "s-ring"
    state: dict = {"last_session_start": time.time(), "stall": {"turn": {"effects": ["file"]}}}
    cp.record_statusline(_payload(sid, 100_000))

    def claim(text):
        time.sleep(0.01)
        cp.note_stop(state)  # a plain turn first, so the claim is a fresh turn
        time.sleep(0.01)
        cp.note_stop(state, text)
        assert isinstance(state["pressure"]["milestone_pending"], dict)

    claim("Step 4 complete.")
    monkeypatch.setattr(common, "get_handoff_data", lambda p="": {"kind": "manual", "created": time.time() + 1.0})
    assert cp.nudge(state, sid, proj)[0] == ""  # a newer manual ring entry answers it
    claim("Step 5 complete.")
    monkeypatch.setattr(common, "get_handoff_data", lambda p="": {"kind": "auto", "created": time.time() + 1.0})
    assert "Step 5 complete" in cp.nudge(state, sid, proj)[0]  # only a deliberate save counts
    # Save first, closing sentence last, inside one turn: the save answers it.
    time.sleep(0.01)
    cp.note_stop(state)
    time.sleep(0.01)
    saved_at = time.time()
    time.sleep(0.01)
    cp.note_stop(state, "Step 6 complete.")
    assert state["pressure"]["milestone_pending"]["since"] > 0
    monkeypatch.setattr(common, "get_handoff_data", lambda p="": {"kind": "manual", "created": saved_at})
    assert cp.nudge(state, sid, proj)[0] == ""
    time.sleep(0.01)
    cp.note_stop(state)
    turn_start = time.time()
    time.sleep(0.01)
    cp.note_stop(state, "Step 7 complete.")
    monkeypatch.setattr(common, "get_handoff_data", lambda p="": {"kind": "manual", "created": turn_start - 5})
    assert "Step 7 complete" in cp.nudge(state, sid, proj)[0]  # a save from before the turn does not


def test_a_remember_in_place_of_a_checkpoint_gets_the_sharper_nudge(tmp_path: Path, monkeypatch):
    stall = pytest.importorskip("windvane.stall", reason="windvane.stall is another agent's module")
    monkeypatch.setattr(cp, "_ring_manual_after", lambda *_a, **_k: False)
    state: dict = {}
    cp.pressure_state(state)["last_stop_at"] = time.time() - 30
    stall.note_tool(state, "mcp__windvane__memory", {"operation": "remember", "content": "State banked: B6 card written; next step is the B3 card"})
    assert state["stall"]["turn"]["records"] == ["memory:remember"] and state["stall"]["turn"].get("remember_state") is True
    cp.note_stop(state, "Banked the state; continuing with B3.")
    mp = cp.pressure_state(state)["milestone_pending"]
    assert mp and mp["kind"] == "claim_remember"
    text, _ = cp.nudge(state, "s-remember", str(tmp_path))
    assert "reads checkpoints only" in text and "checkpoint tool" in text and "operation save" in text
    # Both together are fine: a fact and the resume state are two records.
    state2: dict = {}
    cp.pressure_state(state2)["last_stop_at"] = time.time() - 30
    stall.note_tool(state2, "mcp__windvane__memory", {"operation": "remember", "content": "The pace is 0.15 because of the fair-access policy"})
    stall.note_tool(state2, "mcp__windvane__checkpoint", {"operation": "save", "task_description": "B6 card"})
    cp.note_stop(state2, "B6 card done.")
    assert cp.pressure_state(state2)["milestone_pending"] is None
    state3: dict = {}
    cp.pressure_state(state3)["last_stop_at"] = time.time() - 30
    stall.note_tool(state3, "mcp__windvane__memory", {"operation": "remember", "content": "The pace is 0.15 because of the fair-access policy"})
    cp.note_stop(state3, "Noted. Reading the loader next.")
    assert cp.pressure_state(state3)["milestone_pending"] is None


def test_the_remember_variant_reads_the_turn_records_directly():
    # The same rule as above without the stall module: the records stall.py
    # writes per turn decide it.
    assert cp._turn_banked_with_remember({"stall": {"turn": {"records": ["memory:remember"]}}}, True)
    assert cp._turn_banked_with_remember({"stall": {"turn": {"records": ["memory:remember"], "remember_state": True}}}, False)
    assert not cp._turn_banked_with_remember({"stall": {"turn": {"records": ["memory:remember"]}}}, False)
    assert not cp._turn_banked_with_remember(
        {"stall": {"turn": {"records": ["memory:remember", "checkpoint:save"]}}}, True)
    assert not cp._turn_banked_with_remember({}, True)


# ---------------------------------------------------------------------------
# The usage budget
# ---------------------------------------------------------------------------


def test_the_five_hour_and_seven_day_windows(tmp_path: Path, monkeypatch):
    sid = "s-budget"
    now = time.time()
    proj = str(tmp_path / "no-proj")
    payload = {
        "session_id": sid,
        "context_window": {"total_input_tokens": 50_000, "context_window_size": 1_000_000},
        "rate_limits": {
            "five_hour": {"used_percentage": 93, "resets_at": int(now + 41 * 60)},
            "seven_day": {"used_percentage": 40, "resets_at": int(now + 3 * 86400)},
        },
    }
    cp.record_statusline(payload)
    m = cp.read_mirror(sid)
    assert m["five_hour_pct"] == 93 and m["seven_day_pct"] == 40 and m["five_hour_resets_at"] == int(now + 41 * 60)
    state: dict = {}
    t, changed = cp.nudge(state, sid, proj)
    assert changed and "5-hour window is at 93%" in t and "resets" in t and "in 41 min" in t
    assert "ScheduleWakeup" in t and "checkpoint tool" in t and "operation save" in t
    assert "7-day" not in t
    assert "Usage budget" not in cp.nudge(state, sid, proj)[0]  # same window: not again
    payload["rate_limits"]["five_hour"]["used_percentage"] = 97
    cp.record_statusline(payload)
    assert "Usage budget" not in cp.nudge(state, sid, proj)[0]  # rising inside the same window
    payload["rate_limits"]["five_hour"] = {"used_percentage": 91, "resets_at": int(now + 5 * 3600 + 41 * 60)}
    cp.record_statusline(payload)
    assert "5-hour window is at 91%" in cp.nudge(state, sid, proj)[0]  # a new window fires again
    payload["rate_limits"]["seven_day"] = {"used_percentage": 96, "resets_at": int(now + 2 * 86400)}
    cp.record_statusline(payload)
    t5, _ = cp.nudge(state, sid, proj)
    assert "7-day window is at 96%" in t5 and "resets" in t5 and "unattended" in t5
    assert "7-day" not in cp.nudge(state, sid, proj)[0]
    monkeypatch.setenv("WINDVANE_BUDGET_FIVE_HOUR_PCT", "50")
    payload["rate_limits"]["five_hour"] = {"used_percentage": 55, "resets_at": int(now + 9 * 3600)}
    cp.record_statusline(payload)
    assert "5-hour window is at 55%" in cp.nudge(state, sid, proj)[0]  # threshold is tunable
    monkeypatch.delenv("WINDVANE_BUDGET_FIVE_HOUR_PCT")
    cp.record_statusline({"session_id": "s-apikey", "context_window": {"total_input_tokens": 1, "context_window_size": 200_000}})
    assert "Usage budget" not in cp.nudge({}, "s-apikey", proj)[0]  # an API-key session never fires
    assert "5h 93%" in cp._fmt_statusline(dict(payload, rate_limits={"five_hour": {"used_percentage": 93}}))
    assert "passed" in cp._fmt_reset(now - 10)
    assert " d)" in cp._fmt_reset(now + 3 * 86400)
    assert cp._fmt_reset(None) == "" and cp._fmt_reset("x") == ""


def test_every_rate_limit_window_kind_is_mirrored_and_nudged(tmp_path: Path, monkeypatch):
    sid = "s-kinds"
    now = time.time()
    proj = str(tmp_path / "no-proj")
    payload = {
        "session_id": sid,
        "context_window": {"total_input_tokens": 50_000, "context_window_size": 1_000_000},
        "rate_limits": {
            "five_hour": {"used_percentage": 10, "resets_at": int(now + 3600)},
            "seven_day": {"used_percentage": 20, "resets_at": int(now + 3 * 86400)},
            "seven_day_opus": {"used_percentage": 96, "resets_at": int(now + 2 * 86400)},
            "broken": "not a window",
        },
    }
    cp.record_statusline(payload)
    m = cp.read_mirror(sid)
    # Every kind under rate_limits; the two old kinds keep their flat keys.
    assert set(m["rate_limits"]) == {"five_hour", "seven_day", "seven_day_opus"}
    assert m["rate_limits"]["seven_day_opus"] == {"pct": 96, "resets_at": int(now + 2 * 86400)}
    assert m["five_hour_pct"] == 10 and m["seven_day_pct"] == 20 and "seven_day_opus_pct" not in m
    state: dict = {}
    t, changed = cp.nudge(state, sid, proj)
    assert changed and "the 7-day opus window is at 96%" in t and "resets" in t
    assert "checkpoint tool" in t and "operation save" in t and "5-hour" not in t
    assert state["pressure"]["budget_noticed_reset"]["seven_day_opus"] == int(now + 2 * 86400)
    assert "Usage budget" not in cp.nudge(state, sid, proj)[0]  # one latch per kind and window
    payload["rate_limits"]["seven_day_opus"] = {"used_percentage": 97, "resets_at": int(now + 9 * 86400)}
    cp.record_statusline(payload)
    assert "7-day opus window is at 97%" in cp.nudge(state, sid, proj)[0]  # a new window fires again
    # Other kinds read budget_pct.
    monkeypatch.setenv("WINDVANE_BUDGET_PCT", "99")
    payload["rate_limits"]["seven_day_opus"] = {"used_percentage": 98, "resets_at": int(now + 16 * 86400)}
    cp.record_statusline(payload)
    assert "Usage budget" not in cp.nudge(state, sid, proj)[0]
    monkeypatch.delenv("WINDVANE_BUDGET_PCT")
    # A kind with no reset stamp still says it once.
    payload["rate_limits"]["daily_tokens"] = {"used_percentage": 92}
    cp.record_statusline(payload)
    t, _ = cp.nudge(state, sid, proj)
    assert "the daily tokens window is at 92%" in t
    assert "Usage budget" not in cp.nudge(state, sid, proj)[0]


def test_an_older_mirror_with_flat_keys_only_still_nudges():
    sid = "s-flat"
    store = Path(os.environ["WINDVANE_DIR"]) / "sessions"
    store.mkdir(parents=True)
    rec = {"session_id": sid, "ts": time.time(), "total_input_tokens": 1, "context_window_size": 1_000_000,
           "five_hour_pct": 95, "five_hour_resets_at": int(time.time() + 600)}
    (store / f"{sid}.ctx.json").write_bytes(json.dumps(rec).encode())
    state: dict = {}
    assert "5-hour window is at 95%" in cp.nudge(state, sid)[0]
    assert state["pressure"]["budget_5h_noticed_reset"] == rec["five_hour_resets_at"]
    assert cp.budget_windows(rec) == [("five_hour", 95, rec["five_hour_resets_at"])]
    assert cp.budget_windows(None) == []


@pytest.mark.parametrize(
    "kind,words",
    [("five_hour", "5-hour"), ("seven_day", "7-day"), ("seven_day_opus", "7-day opus"),
     ("seven_day_sonnet", "7-day sonnet"), ("daily_tokens", "daily tokens"), ("30_day", "30-day"), ("", "usage")],
)
def test_a_window_kind_in_words(kind, words):
    assert cp.kind_words(kind) == words


# ---------------------------------------------------------------------------
# Repo state
# ---------------------------------------------------------------------------

_GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}


def _git_repo(tmp_path: Path) -> tuple[Path, "callable"]:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    env = {**os.environ, **_GIT_ENV}

    def git(*a):
        return subprocess.run(["git", *a], cwd=str(repo), check=True, capture_output=True, text=True,
                              env=env, stdin=subprocess.DEVNULL).stdout.strip()

    git("init", "-q")
    (repo / "src" / "sync.py").write_text("PACE = 0.5\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "sync: first cut")
    (repo / "src" / "sync.py").write_text("PACE = 0.15  # seconds between requests\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "sync: pace 0.15")
    return repo, git


def test_since_counts_commits_on_other_local_branches(tmp_path: Path):
    repo, git = _git_repo(tmp_path)
    first = git("rev-list", "--max-parents=0", "HEAD")
    head = git("rev-parse", "HEAD")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    git("checkout", "-q", "-b", "wt")
    (repo / "src" / "other.py").write_text("X = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "wt: a commit on a worktree branch")
    git("checkout", "-q", branch)
    info = repo_state.since(first, str(repo), files=["src/sync.py"])
    assert info and info["commits"] == 1 and info["other_branches"] == 1 and info["touched"] == ["sync.py"]
    assert "1 commit on other local branches" in repo_state.since_text(info)
    info2 = repo_state.since(head, str(repo))
    assert info2 and info2["commits"] == 0 and info2["other_branches"] == 1
    assert repo_state.since_text(info2).startswith("Since this checkpoint: no commits on this branch; 1 commit on other")
    assert repo_state.head(str(repo)) == head[:len(repo_state.head(str(repo)))]
    missing = repo_state.since("0" * 40, str(repo))
    assert missing and missing["missing"] and "not in this history" in repo_state.since_text(missing)
    assert repo_state.since(head, str(tmp_path / "nowhere")) is None and repo_state.head("") == ""


def test_the_branch_count_is_bounded_by_the_checkpoint_time(tmp_path: Path):
    repo, git = _git_repo(tmp_path)
    first = git("rev-list", "--max-parents=0", "HEAD")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    git("checkout", "-q", "-b", "old-feature", first)
    (repo / "old.py").write_text("X = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "an old branch commit")
    git("checkout", "-q", branch)
    head = git("rev-parse", "HEAD")
    unbounded = repo_state.since(head, str(repo))
    bounded = repo_state.since(head, str(repo), saved_at=time.time() + 60)  # newer than every commit
    assert unbounded and unbounded["other_branches"] == 1
    assert bounded and bounded["other_branches"] == 0


def test_the_git_trace_knob_logs_every_call(tmp_path: Path, monkeypatch):
    repo, _git = _git_repo(tmp_path)
    trace = tmp_path / "git.log"
    monkeypatch.setenv("WINDVANE_GIT_TRACE", str(trace))
    assert repo_state.head(str(repo))
    assert "git rev-parse --short HEAD" in trace.read_text(encoding="utf-8")


def test_the_goal_a_checkpoint_carries(tmp_path: Path):
    assert repo_state.goal_for_session({"run": {"goal": "  ship it  "}}) == "ship it"
    assert repo_state.goal_for_session({}) == ""
    assert repo_state.goal_for_session({"run": {"transcript_path": str(tmp_path / "missing.jsonl")}}) == ""


def test_the_goal_from_the_transcript_when_none_is_recorded(tmp_path: Path):
    pytest.importorskip("windvane.goal", reason="windvane.goal is another agent's module")
    t = tmp_path / "t.jsonl"
    sentinel = {"type": "attachment", "timestamp": "2026-09-10T22:45:54.205Z",
                "attachment": {"type": "goal_status", "met": False, "sentinel": True, "condition": "tests  pass"}}
    t.write_text(json.dumps(sentinel) + "\n", encoding="utf-8")
    assert repo_state.goal_for_session({"run": {"transcript_path": str(t)}}) == "tests pass"


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


def test_alert_command_comes_from_config(tmp_path: Path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".windvane").mkdir(parents=True)
    assert alerts.alert_command(str(proj)) == ""
    (proj / ".windvane" / "config.json").write_text(json.dumps({"alert_command": " notify {message} "}), encoding="utf-8")
    assert alerts.alert_command(str(proj)) == "notify {message}"
    monkeypatch.setenv("WINDVANE_ALERT_COMMAND", "from-env")
    assert alerts.alert_command(str(proj)) == "from-env"


def test_an_alert_is_recorded_whether_sent_or_not(tmp_path: Path):
    state: dict = {}
    rec = alerts.send("halted:   needs a person", str(tmp_path), kind="halt", state=state)
    assert rec["sent"] is False and rec["detail"] == "no alert_command configured"
    assert rec["message"] == "halted: needs a person"
    assert alerts.summary(state) == [rec]
    assert len(alerts.send("x" * 500, state=state)["message"]) == 200
    for _ in range(alerts.KEEP + 5):
        alerts.send("m", state=state)
    assert len(state["alerts"]) == alerts.KEEP


def test_an_alert_reaches_the_command_by_placeholder_or_stdin(tmp_path: Path):
    out = tmp_path / "out.txt"
    argv_script = tmp_path / "argv.py"
    argv_script.write_text(f"import sys\nopen({str(out)!r}, 'w').write(' '.join(sys.argv[1:]))\n", encoding="utf-8")
    stdin_script = tmp_path / "stdin.py"
    stdin_script.write_text(f"import sys\nopen({str(out)!r}, 'w').write(sys.stdin.read())\n", encoding="utf-8")
    rec = alerts.send("run halted at turn 9", command=f"{sys.executable} {argv_script} {{message}}")
    assert rec["sent"] is True and rec["detail"] == "ok"
    assert out.read_text(encoding="utf-8") == "run halted at turn 9"
    rec = alerts.send("limit hit", command=f"{sys.executable} {stdin_script}")
    assert rec["sent"] is True and out.read_text(encoding="utf-8") == "limit hit"
    rec = alerts.send("x", command=f'{sys.executable} -c "import sys; sys.exit(3)"')
    assert rec["sent"] is False


# ---------------------------------------------------------------------------
# The process census
# ---------------------------------------------------------------------------


def test_the_census_warns_only_when_two_daemons_share_a_store():
    a = {"pid": 1, "role": "daemon", "rss_mb": 1200, "commit_mb": 3000, "age_min": 5.0, "store": "default"}
    b = dict(a, pid=2, store=str(Path("/tmp/pytest-x/store")))
    assert not any("WARNING" in line for line in procs.census_lines([a, b]))
    assert any("WARNING" in line for line in procs.census_lines([a, dict(a, pid=3)]))
    assert any("pytest-x" in line for line in procs.census_lines([a, b]))
    m = dict(a, role="miner")
    assert any("WARNING: 2 miners" in line for line in procs.census_lines([m, dict(m, pid=4)]))
    assert procs.census_lines([]) == ["Processes: none found (or psutil unavailable)"]


def test_roles_by_command_line():
    assert procs._role("python -S -m windvane.daemon_client post_tool_json") == "hook"
    assert procs._role("python windvane/daemon_client.py stop_json") == "hook"
    assert procs._role("python -m windvane.daemon") == "daemon"
    assert procs._role("python /opt/x/windvane/daemon.py") == "daemon"
    assert procs._role("python -m windvane.mining.background --project p") == "miner"
    assert procs._role("python -m windvane.semantic.worker in.json out.npy") == "embed worker"
    assert procs._role("python -m windvane.migrate") == "migration"
    assert procs._role("python -m windvane.events session_start_json") == "hook"
    assert procs._role("python -m windvane.report") == "other"
    assert procs._role("python -m somethingelse") == ""


def test_the_process_census_names_windvane_processes_by_role():
    psutil = pytest.importorskip("psutil")
    # The child outlives any census: a full process walk can take a minute
    # under antivirus load. It is killed in the finally either way.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)", "windvane.mining.background"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.0)
        # a venv's python.exe is a launcher stub; the census reports its child
        pids = {child.pid} | {c.pid for c in psutil.Process(child.pid).children(recursive=True)}
        mine = [r for r in procs.census() if r["pid"] in pids]
        assert mine and mine[0]["role"] == "miner" and mine[0]["rss_mb"] >= 0
    finally:
        for p in psutil.Process(child.pid).children(recursive=True):
            p.kill()
        child.kill()


def test_every_subprocess_detaches_stdin():
    # A child that inherits a stdio server's stdin stalled every git call by
    # the full timeout and hung the server for minutes (2026-09-10).
    offenders = []
    for mod in (cp, ms, rr, repo_state, alerts, procs):
        p = Path(mod.__file__)
        src = p.read_text(encoding="utf-8")
        for m in re.finditer(r"subprocess\.(run|Popen|check_output)\(", src):
            head = src[m.start(): m.start() + 900].split("\n\n", 1)[0]
            if "stdin" not in head and "**kwargs" not in head and "input=" not in head:
                offenders.append(f"{p.name}:{src[:m.start()].count(chr(10)) + 1}")
    assert offenders == [], offenders


# ---------------------------------------------------------------------------
# The run report
# ---------------------------------------------------------------------------


def _msg(mtype, ts, **kw):
    d = {"type": mtype, "timestamp": ts, "sessionId": kw.pop("sid", "S1"), "gitBranch": "feat/x"}
    d.update(kw)
    return d


def _report_transcript(path: Path, sid: str) -> Path:
    goal = "make all benches pass or stop after 20 turns"
    lines = [
        _msg("user", "2026-09-09T10:00:00Z", sid=sid, message={"role": "user", "content": f"<command-name>/goal</command-name>\n<command-message>goal</command-message>\n<command-args>{goal}</command-args>"}),
        # The verified shape from a real /goal run (2.1.267): sentinel on set,
        # then one goal_status attachment per evaluator verdict.
        {"type": "attachment", "timestamp": "2026-09-09T10:00:01Z", "sessionId": sid, "attachment": {"type": "goal_status", "met": False, "sentinel": True, "condition": goal}},
        {"type": "attachment", "timestamp": "2026-09-09T10:30:00Z", "sessionId": sid, "attachment": {"type": "goal_status", "met": False, "condition": goal, "reason": "Two benches still fail.", "iterations": 1, "durationMs": 4000, "tokens": 900}},
        {"type": "attachment", "timestamp": "2026-09-09T11:58:00Z", "sessionId": sid, "attachment": {"type": "goal_status", "met": True, "condition": goal, "reason": "All benches pass in the transcript.", "iterations": 2, "durationMs": 5000, "tokens": 1200}},
        _msg("user", "2026-09-09T10:00:05Z", sid=sid, message={"role": "user", "content": "start"}),
        _msg("assistant", "2026-09-09T10:00:10Z", sid=sid, message={"role": "assistant", "model": "claude-fable-5-1", "content": [{"type": "text", "text": "working"}, {"type": "tool_use", "id": "e1", "name": "Edit", "input": {"file_path": "C:\\w\\proj\\a.py"}}, {"type": "tool_use", "id": "e2", "name": "Write", "input": {"file_path": "C:/w/proj/c.py"}}]}),
        _msg("assistant", "2026-09-09T10:00:12Z", sid=sid, message={"role": "assistant", "model": "<synthetic>", "content": [{"type": "text", "text": "injected"}]}),
        _msg("assistant", "2026-09-09T10:00:14Z", sid=sid, message={"role": "assistant", "model": "claude-fable-5-1", "content": [{"type": "tool_use", "id": "c1", "name": "CronCreate", "input": {"cron": "*/2 * * * *", "prompt": "append", "recurring": True}}, {"type": "tool_use", "id": "w1", "name": "ScheduleWakeup", "input": {"delaySeconds": 120, "prompt": "x"}}, {"type": "tool_use", "id": "w2", "name": "ScheduleWakeup", "input": {"stop": True}}, {"type": "tool_use", "id": "c2", "name": "CronDelete", "input": {"id": "c1"}}]}),
        _msg("user", "2026-09-09T10:01:00Z", sid=sid, message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "ModuleNotFoundError: No module named 'foo'"}]}),
        _msg("user", "2026-09-09T10:02:00Z", sid=sid, message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "is_error": True, "content": "ModuleNotFoundError: No module named 'foo'"}]}),
        _msg("user", "2026-09-09T10:03:00Z", sid=sid, toolUseResult="Error: File does not exist.", message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t3", "is_error": True, "content": "Error: File does not exist."}]}),
        {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-09-09T11:00:00Z", "sessionId": sid, "content": "Conversation compacted", "compactMetadata": {"trigger": "auto", "preTokens": 489107, "postTokens": 28251, "cumulativeDroppedTokens": 460856, "durationMs": 120559}},
        _msg("assistant", "2026-09-09T11:00:30Z", sid=sid, message={"role": "assistant", "model": "claude-fable-5-1", "content": [{"type": "text", "text": "continuing"}]}),
        _msg("user", "2026-09-09T11:30:00Z", sid="OTHER", message={"role": "user", "content": "not this session"}),
        _msg("assistant", "2026-09-09T12:00:00Z", sid=sid, message={"role": "assistant", "model": "claude-fable-5-1", "content": [{"type": "text", "text": "Phase 1 done."}]}),
    ]
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


def _report_state(transcript: Path, started: float) -> dict:
    state: dict = {
        "last_session_start": started,
        "prompts_this_session": 3,
        "files_edited_this_session": ["C:/w/proj/a.py", "C:/w/proj/b.py"],
        "loop": {
            "edit_counts": {"C:/w/proj/a.py": 4, "c:/w/proj/B.py": 1},
            "test_results": [{"timestamp": started + 10, "passed": False}, {"timestamp": started + 500, "passed": True}],
        },
        "test_runs_this_session": 2,
        "last_test_passed": True,
        "run": {"start_commit": "abc1234", "permission_mode": "auto", "transcript_path": str(transcript)},
    }
    cp.note_stop(state, "Working.")
    cp.note_stop(state, "More.")
    cp.note_compaction(state)
    cp.note_restored(state, {"kind": "manual", "task_id": "task_1", "summary": "step 1 banked"})
    # The hook record sits 20 s after the transcript's boundary (the boundary
    # is written, then PostCompact runs).
    state["pressure"]["compactions"][0]["at"] = rr._parse_iso("2026-09-09T11:00:20Z")
    return state


def test_the_report_reads_the_transcript_and_the_hook_state(tmp_path: Path, monkeypatch):
    jr = pytest.importorskip("windvane.mining.jsonl_reader", reason="windvane.mining.jsonl_reader is another agent's module")
    monkeypatch.setattr(jr, "_get_claude_projects_dir", lambda: tmp_path / "claude_projects")
    sid = "sess-1234-abcd"
    project = tmp_path / "proj"
    project.mkdir()
    transcript = _report_transcript(tmp_path / "transcript.jsonl", sid)
    started = time.time() - 7200
    state = _report_state(transcript, started)
    cp.record_statusline({"session_id": sid, "model": {"id": "claude-fable-5-1", "display_name": "Fable 5.1"},
                          "context_window": {"total_input_tokens": 300000, "context_window_size": 1000000}, "cost": {"total_cost_usd": 4.2}})

    r = rr.collect(sid, str(project), state)
    assert r["run_id"].endswith("-sess-123") and r["run_id"][:2] == "20"
    assert r["goal_text"] == "make all benches pass or stop after 20 turns" and r["goal_set_at"] == "2026-09-09T10:00:01Z"
    vs = r["goal_verdicts"]
    assert len(vs) == 2 and vs[0]["met"] is False and vs[1]["met"] is True
    assert vs[1]["reason"].startswith("All benches") and vs[1]["iterations"] == 2 and vs[1]["duration_ms"] == 5000 and vs[1]["tokens"] == 1200
    assert r["goal_outcome"] == "met"
    assert r["model"] == "claude-fable-5-1" and r["models"] == ["claude-fable-5-1"]  # <synthetic> is not a model
    assert r["branch"] == "feat/x"  # the transcript's, when the project has no git
    assert r["permission_mode"] == "auto" and r["start_commit"] == "abc1234"
    assert r["turns"] == 2 and r["prompts"] == 1
    assert 7100 <= r["wall_seconds"] <= 7300
    assert r["tokens"]["final_input"] == 300000 and r["tokens"]["cost_usd"] == 4.2
    c = r["compactions"]
    assert len(c) == 1 and c[0]["trigger"] == "auto" and c[0]["pre_tokens"] == 489107 and c[0]["post_tokens"] == 28251
    assert (c[0].get("restored") or {}).get("task_id") == "task_1"
    f = r["files"]
    assert sorted(x["path"][-4:] for x in f) == ["a.py", "b.py", "c.py"]
    assert {x["path"][-4:]: x["edits"] for x in f} == {"a.py": 4, "b.py": 1, "c.py": 1}
    assert f[0]["path"].endswith("a.py")
    assert r["scheduled"] == {"cron_created": 1, "cron_deleted": 1, "wakeups": 1}
    t = r["tests"]
    assert t["runs"] == 2 and t["first"] is False and t["last"] is True and t["failures"] == 1
    e = r["errors"]
    assert e["count"] == 3 and e["distinct"] == 2 and e["recurring"] == 1
    assert e["top"][0]["count"] == 2 and "ModuleNotFoundError" in e["top"][0]["text"]
    assert not any("verdict" in n for n in r["not_measured"])

    md = rr.render_md(r)
    for sect in ("# Run ", "## Compactions (1)", "## Files touched (3)", "## Tests", "## Errors (3;", "## Checkpoints (", "## Not measured"):
        assert sect in md, sect
    assert "**Scheduled work:** cron jobs created 1, deleted 1, self-paced wakeups 1" in md
    assert "**Goal:** make all benches pass" in md and "**met**" in md
    assert "## Goal (2 evaluator verdicts)" in md and "| yes | 2 | All benches pass" in md
    assert "489K → 28K" in md and "manual task_1" in md
    assert "by windvane report schema" in md

    p = rr.write_report(sid, str(project), state)
    assert p is not None and p.suffix == ".md" and p.parent == project / ".windvane" / "runs"
    js = p.with_suffix(".json")
    assert js.is_file() and json.loads(js.read_text(encoding="utf-8"))["schema"] == rr.SCHEMA
    assert rr.write_report(sid, str(project), state) == p  # idempotent

    # A session started from a workspace root lives under THAT projects dir:
    # found by session id, not by project.
    (tmp_path / "claude_projects" / "w-root").mkdir(parents=True)
    (tmp_path / "claude_projects" / "w-root" / f"{sid}.jsonl").write_bytes(transcript.read_bytes())
    found = rr.find_transcript(sid, str(tmp_path / "elsewhere"), "")
    assert found is not None and found.name == f"{sid}.jsonl"
    assert rr.find_transcript(sid, str(project), str(transcript)) == transcript

    # Join by time: a hook record 2 minutes from the boundary attaches; one an
    # hour away does not, whatever the order.
    st2 = {
        "run": {"transcript_path": str(transcript), "started_at": started},
        "pressure": {"compactions": [
            {"at": rr._parse_iso("2026-09-09T09:00:00Z"), "restored": {"kind": "auto", "task_id": "far"}},
            {"at": rr._parse_iso("2026-09-09T11:02:00Z"), "restored": {"kind": "manual", "task_id": "near"}},
        ]},
    }
    r3 = rr.collect(sid, str(project), st2)
    assert (r3["compactions"][0].get("restored") or {}).get("task_id") == "near"
    assert r3["started_at"] == rr._iso(started)

    # A session id resumed for months: the report reads from the run's start.
    old = tmp_path / f"{sid}-old.jsonl"
    june = {"type": "system", "subtype": "compact_boundary", "timestamp": "2026-06-10T23:32:39Z", "sessionId": sid,
            "content": "Conversation compacted", "compactMetadata": {"trigger": "manual", "preTokens": 555421, "postTokens": 17000}}
    old.write_text(json.dumps(june) + "\n" + transcript.read_text(encoding="utf-8"), encoding="utf-8")
    assert len(rr._read_transcript(old, sid)["compactions"]) == 2
    tr_since = rr._read_transcript(old, sid, since=rr._parse_iso("2026-09-01T00:00:00Z"))
    assert len(tr_since["compactions"]) == 1 and tr_since["compactions"][0]["pre_tokens"] == 489107
    r_old = rr.collect(sid, str(project), {"run": {"transcript_path": str(old), "started_at": rr._parse_iso("2026-09-09T09:30:00Z")},
                                           "pressure": st2["pressure"]})
    assert len(r_old["compactions"]) == 1 and (r_old["compactions"][0].get("restored") or {}).get("task_id") == "near"
    r_skew = rr.collect(sid, str(project), {"run": {"transcript_path": str(old), "started_at": time.time()}, "pressure": st2["pressure"]})
    assert len(r_skew["compactions"]) == 2  # a clock mismatch never empties the report

    # A goal set but never judged, and one judged impossible.
    unjudged = tmp_path / "unjudged.jsonl"
    unjudged.write_text(json.dumps({"type": "attachment", "timestamp": "2026-09-09T10:00:01Z", "sessionId": "u1",
                                    "attachment": {"type": "goal_status", "met": False, "sentinel": True, "condition": "finish it"}}) + "\n", encoding="utf-8")
    r4 = rr.collect("u1", str(project), {"last_session_start": time.time() - 5, "run": {"transcript_path": str(unjudged)}})
    assert r4["goal_outcome"] == "set, no verdict recorded"
    failed = tmp_path / "failed.jsonl"
    failed.write_text(
        json.dumps({"type": "attachment", "timestamp": "2026-09-09T10:00:01Z", "sessionId": "f1", "attachment": {"type": "goal_status", "met": False, "sentinel": True, "condition": "prove 2 is odd"}}) + "\n"
        + json.dumps({"type": "attachment", "timestamp": "2026-09-09T10:01:00Z", "sessionId": "f1", "attachment": {"type": "goal_status", "met": False, "failed": True, "condition": "prove 2 is odd", "reason": "2 is even; the assistant refused to fabricate a proof.", "iterations": 1, "durationMs": 54988, "tokens": 3574}}) + "\n",
        encoding="utf-8",
    )
    r5 = rr.collect("f1", str(project), {"last_session_start": time.time() - 5, "run": {"transcript_path": str(failed)}})
    assert r5["goal_outcome"] == "failed" and r5["goal_verdicts"][0]["flags"] == {"failed": True}
    assert "| failed | 1 | 2 is even" in rr.render_md(r5)


def test_the_report_lists_this_sessions_checkpoints_and_known_errors(tmp_path: Path, monkeypatch):
    jr = pytest.importorskip("windvane.mining.jsonl_reader", reason="windvane.mining.jsonl_reader is another agent's module")
    hs = pytest.importorskip("windvane.checkpoints", reason="windvane.checkpoints is another agent's module")
    common = pytest.importorskip("windvane.events.common", reason="windvane.events.common is another agent's module")
    monkeypatch.setattr(jr, "_get_claude_projects_dir", lambda: tmp_path / "claude_projects")
    sid = "sess-1234-abcd"
    project = tmp_path / "proj"
    project.mkdir()
    transcript = _report_transcript(tmp_path / "transcript.jsonl", sid)
    ring_dir = tmp_path / "projects" / "hash1"
    monkeypatch.setattr(common, "_project_hash_dir", lambda p: ring_dir)
    monkeypatch.setattr(common, "_handoff_candidate_dirs", lambda p="": [ring_dir, tmp_path / "checkpoints"])
    hs.write_handoff({"kind": "manual", "created": time.time() - 3000, "summary": "step 1 banked", "task_id": "task_1", "session_id": sid, "next_steps": ["x"]}, [ring_dir])
    hs.write_handoff({"kind": "auto", "created": time.time() - 100, "summary": "Session stopped. 2 files edited.", "session_id": sid, "files_in_progress": ["a.py"], "decisions": ["d"]}, [ring_dir])
    hs.write_handoff({"kind": "manual", "created": time.time() - 50, "summary": "someone else's", "task_id": "task_9", "session_id": "OTHER"}, [ring_dir])
    (ring_dir / "patterns.json").write_text(json.dumps({"recurring_errors": [{"error_type": "ModuleNotFoundError", "session_count": 4}]}), encoding="utf-8")

    r = rr.collect(sid, str(project), _report_state(transcript, time.time() - 7200))
    cps = r["checkpoints"]
    assert any(x["task_id"] == "task_1" and x["kind"] == "manual" for x in cps)
    assert all(x["task_id"] != "task_9" for x in cps)
    assert r["checkpoint_counts"]["deliberate"] == 1
    assert "## Checkpoints (1 deliberate," in rr.render_md(r)
    e = r["errors"]
    assert e["top"][0]["known_before"] is True and e["top"][1]["known_before"] is False


def test_the_report_measures_stalls_and_compliance_when_their_modules_are_there(tmp_path: Path, monkeypatch):
    for mod in ("windvane.stall", "windvane.compliance", "windvane.storage"):
        pytest.importorskip(mod, reason=f"{mod} is another agent's module")
    r = rr.collect("s-measured", str(tmp_path), {"last_session_start": time.time() - 10})
    assert isinstance(r["stalls"], dict) and isinstance(r["compliance"], dict)
    assert not [n for n in r["not_measured"] if "stall" in n or "compliance (state" in n]


def test_a_report_with_no_transcript_and_no_mirror_names_what_is_missing(tmp_path: Path, monkeypatch):
    try:
        from windvane.mining import jsonl_reader as jr

        monkeypatch.setattr(jr, "_get_claude_projects_dir", lambda: tmp_path / "claude_projects")
    except ImportError:
        pass
    project = tmp_path / "proj"
    project.mkdir()
    r = rr.collect("nosess", str(project), {"last_session_start": time.time() - 10, "files_edited_this_session": ["x.py"]})
    assert r["files"][0]["path"] == "x.py"
    assert any("transcript" in n for n in r["not_measured"]) and any("mirror" in n for n in r["not_measured"])
    assert r["goal_text"] is None and r["goal_verdicts"] == [] and r["goal_outcome"] == ""
    assert r["run_id"].endswith("-nosess")


def test_the_report_says_how_a_run_ended_on_an_api_failure(tmp_path: Path):
    now = time.time()
    state = {"last_session_start": now - 60, "run": {"end_reason": "other", "failures": [
        {"at": now - 30, "error_type": "rate_limit", "error": "You've hit your session limit",
         "five_hour_pct": 100, "five_hour_resets_at": int(now + 1800)},
    ]}}
    md = rr.render_md(rr.collect("s-stopfail", str(tmp_path), state))
    assert "API failure at" in md and "rate_limit" in md and "5-hour window at 100%" in md and "resets" in md
    assert "- **Ended:** other" in md


def test_the_goal_run_section_renders_the_brackets_summary():
    r = {"run_id": "2026-10-04-abcd1234", "project": "/w/p", "session_id": "abcd1234", "generated_at": "", "schema": 1,
         "autorun": {"status": "met", "goal": "tests pass", "turns": 4, "max_turns": 150, "duration_s": 60, "verdicts": 1, "why": "met"}}
    md = rr.render_md(r)
    assert "## Goal run (met)" in md and "windvane's bracket" in md and "4 of the 150 cap · 60 s" in md


def test_the_automatic_write_is_gated_on_substance(tmp_path: Path):
    t = tmp_path / "goal.jsonl"
    t.write_text(json.dumps({"type": "attachment", "attachment": {"type": "goal_status", "sentinel": True}}) + "\n", encoding="utf-8")
    assert rr.substantial({"files_edited_this_session": ["a"]})
    assert rr.substantial({"pressure": {"cycle": 1}})
    assert rr.substantial({"prompts_this_session": 5})
    assert not rr.substantial({"prompts_this_session": 2})
    assert rr.substantial({"run": {"transcript_path": str(t)}})  # a goal, even with no edits
    assert rr.substantial({"test_runs_this_session": 1})
    assert rr.substantial({"pressure": {"stops_total": 3}})
    assert not rr.substantial({"pressure": {"stops_total": 2}})
    assert not rr.goal_seen(str(tmp_path / "nope.jsonl")) and not rr.goal_seen("")


def test_the_report_cli(tmp_path: Path, monkeypatch):
    env = dict(os.environ, PYTHONPATH=str(ROOT), CLAUDE_CODE_SESSION_ID="")
    res = subprocess.run([sys.executable, "-m", "windvane.report"], capture_output=True, text=True,
                         cwd=str(ROOT), env=env, timeout=60, stdin=subprocess.DEVNULL)
    assert res.returncode == 2 and "python -m windvane.report" in res.stdout
    pytest.importorskip("windvane.events.common", reason="--stdout loads the session state through windvane.events.common")
    from windvane.events import common

    monkeypatch.setattr(common, "_session_id", "s-cli-report", raising=False)
    common.save_state({"last_session_start": time.time() - 10, "files_edited_this_session": ["a.py"]})
    res = subprocess.run([sys.executable, "-m", "windvane.report", "--session", "s-cli-report", "--project", str(tmp_path), "--stdout"],
                         capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=60, stdin=subprocess.DEVNULL)
    assert res.returncode == 0 and "# Run " in res.stdout and "a.py" in res.stdout

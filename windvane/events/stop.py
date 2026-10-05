"""Stop, StopFailure, Notification and SessionEnd.

- Stop (``stop_json``): the per-turn auto handoff, the turn's bookkeeping
  (the milestone read, the stall ladder's judgment, the halt at the strike
  cap in autonomy mode), the drafted checkpoint banked when the turn edited
  and banked nothing deliberate (at most once per ten minutes), the /goal
  bracket, and the debounced live mining tick.
- StopFailure (``stop_failure_json``): the API failure recorded for the run
  report, with every rate-limit window the statusline mirror holds.
- Notification (``notification_json``): in autonomy mode, an alert when
  the run waits on a person.
- SessionEnd (``session_end_json``): the run report for a substantial
  session, the post-session miner.
"""

import json
import os
import time
from pathlib import Path

from windvane.events import common as c
from windvane.events.common import (
    _get_session_context_for_handoff,
    _global_handoff_dir,
    _goal_bracket,
    _project_hash_dir,
    _read_stdin_with_timeout,
    get_memory_counts,
    get_windvane_storage_dir,
    load_project_memory,
    load_state,
    mark_session_ended,
    save_state,
    session_project,
)

DRAFT_BANK_GAP_SECS = 600  # at most one Stop-time draft bank per ten minutes per session
_OFF = ("0", "off", "false", "no")


def _maybe_live_mine(project_dir: str) -> None:
    """Debounced live-miner spawn -- keeps the index fresh DURING long sessions.

    The Stop hook fires at the end of every assistant turn; most turns this
    is a no-op (marker newer than the interval, or a miner already holds the
    lock). When it does fire, the "live" mode runs only the incremental
    phases -- session index, extraction, search embeddings, two code
    indexes -- each of which is cursor/watermark-keyed, so a tick costs the
    new transcript tail, not a re-mine. Tune with the ``live_mine`` setting
    (seconds, default 300; 0/off disables)."""
    try:
        from windvane import config

        raw = os.environ.get("WINDVANE_LIVE_MINE", "").strip().lower()
        if raw in _OFF:
            return
        interval = config.knob_int("live_mine", project_dir)
        if interval <= 0:
            return
        interval = max(60, interval)
        marker = get_windvane_storage_dir() / "live_mine_last"
        try:
            if marker.exists() and time.time() - marker.stat().st_mtime < interval:
                return
        except OSError:
            pass
        from windvane.mining.background import start_mining_background

        if start_mining_background(project_dir, mode="live"):
            try:
                marker.write_text(str(time.time()))
            except OSError:
                pass
    except Exception:
        pass


def _turn_banked_deliberately(state: dict) -> bool:
    """Did the turn that is ending save a deliberate checkpoint (the
    checkpoint tool's save, or compact_now, which banks first)? Read from the
    stall ladder's per-turn record (``<tool>:<operation>`` tags) before the
    turn is closed."""
    try:
        from windvane import stall as _stall

        recs = _stall.stall_state(state).get("turn", {}).get("records") or []
    except Exception:
        return False
    for r in recs:
        r = str(r)
        if r in ("checkpoint:save", "checkpoint") or r.startswith("compact_now"):
            return True
    return False


def _transcript_of(state: dict, data: dict) -> str:
    _run = state.get("run")
    return str((data or {}).get("transcript_path") or "") or str(
        (_run if isinstance(_run, dict) else {}).get("transcript_path") or ""
    )


def _refresh_deliberate(state: dict, data: dict, project_dir: str, turn_saved: bool) -> bool:
    """A deliberate checkpoint saved during the turn that is ending, or
    before the session's latest edits, describes the turn so far; bring it
    up to the recorder's draft (``windvane.draft.refresh_deliberate``). True
    when a record was refreshed."""
    try:
        from windvane import draft as _draft

        transcript = _transcript_of(state, data)
        record, _ctx = _draft.draft_with_context(project_dir, c._session_id, transcript, state,
                                                 last_text=str((data or {}).get("last_assistant_message") or ""))
        return _draft.refresh_deliberate(record, project_dir, c._session_id, transcript, state, turn_saved=turn_saved) is not None
    except Exception:
        return False


def _maybe_bank_draft(state: dict, data: dict, project_dir: str, deliberate: bool) -> bool:
    """Bank the recorder's draft at Stop when the turn ended with no
    deliberate checkpoint and the session edited a file since the last bank;
    at most once per DRAFT_BANK_GAP_SECS per session. The record is the
    draft PreCompact banks (``windvane.draft``). A deliberate checkpoint
    resets the edit baseline; one saved before the turn's edits is brought
    up to the draft instead of banked over. Returns True when a draft was
    banked."""
    db = state.get("draft_bank")
    if not isinstance(db, dict):
        db = {}
        state["draft_bank"] = db
    try:
        edits = int(state.get("edits_total") or 0)
    except (TypeError, ValueError):
        edits = 0
    if deliberate:
        db["edits"] = edits
        # The save came in the middle of the turn: its summary is the
        # previous reply's, and an edit after it is not in it. The record a
        # compaction would restore is brought up to the turn's end.
        _refresh_deliberate(state, data, project_dir, turn_saved=True)
        return False
    try:
        baseline = int(db.get("edits") or 0)
        last_at = float(db.get("at") or 0.0)
    except (TypeError, ValueError):
        baseline, last_at = 0, 0.0
    if edits <= baseline or time.time() - last_at < DRAFT_BANK_GAP_SECS:
        return False
    try:
        from windvane import draft as _draft
    except Exception:
        return False
    try:
        transcript = _transcript_of(state, data)
        record, _ctx = _draft.draft_with_context(project_dir, c._session_id, transcript, state,
                                                 last_text=str((data or {}).get("last_assistant_message") or ""))
        # An earlier deliberate save the session has edited past is refreshed
        # from the same draft before the automatic entry is banked.
        _draft.refresh_deliberate(record, project_dir, c._session_id, transcript, state)
        _draft.bank(record, c._session_id, trigger="stop")
    except Exception:
        return False
    db["at"] = time.time()
    db["edits"] = edits
    return True


def _hook_stop(project_dir: str) -> None:
    """Stop: {last_assistant_message, stop_hook_active, ...}. Save what the
    session was doing and close the turn."""
    try:
        stdin_data = _read_stdin_with_timeout(0.5)
        if stdin_data:
            data = json.loads(stdin_data)
            last_message = data.get("last_assistant_message", "")

            state = load_state()
            files_edited = state.get("files_edited_this_session", [])

            # A subagent's stop must not write the parent project's ring:
            # its "N files edited" breadcrumb buried the main session's
            # handoff.
            if (files_edited or last_message) and not data.get("agent_id"):
                # The ring this auto belongs to is the sub-project the
                # session actually WORKED IN, not the cwd (the workspace root
                # under Claude Code).
                handoff_project = session_project(project_dir, state)
                ctx = _get_session_context_for_handoff(handoff_project)
                summary_parts = [f"Session stopped. {len(files_edited)} files edited."]
                if ctx["decisions"]:
                    summary_parts.append("Decisions: " + "; ".join(ctx["decisions"]))
                if ctx["prompts"]:
                    summary_parts.append(f"{ctx['prompts']} prompts, {ctx['tests']} tests.")
                if ctx["slow_tools"]:
                    summary_parts.append("Slow tools: " + "; ".join(ctx["slow_tools"]))

                handoff = {
                    "created": time.time(),
                    "kind": "auto",
                    "summary": " ".join(summary_parts),
                    "next_steps": ["Review what was in progress"],
                    "context_needed": [],
                    "warnings": [],
                    "project_path": handoff_project,
                    "session_id": c._session_id,
                    "files_in_progress": files_edited[:10],
                    "decisions": ctx["decisions"],
                    "mistakes": ctx["mistakes"],
                }

                # Durable store: MANUAL handoffs own the history ring; this
                # auto only contends for the latest pointer (and a trivial
                # one -- 0 files, 0 decisions -- is dropped entirely), so it
                # can never bury a manual checkpoint.
                from windvane import checkpoints as _hs

                _hs.write_handoff(
                    handoff,
                    [_project_hash_dir(handoff_project), _global_handoff_dir()],
                )

            # Also persist session files for next session context
            mark_session_ended()

            # One more turn for the checkpoint bookkeeping (windvane.pressure)
            # and the milestone read (windvane.milestones): the final message
            # is where the model declares a step done. Counted here, not at
            # UserPromptSubmit: an unattended /goal loop has no user prompts,
            # but every turn still ends with a Stop. Subagents excluded --
            # their "done" is not the session's.
            if not data.get("agent_id"):
                try:
                    from windvane import pressure as _cp

                    _st = load_state()
                    _deliberate = _turn_banked_deliberately(_st)
                    _cp.note_stop(_st, str(last_message or ""))
                    # Close the turn for the stall ladder: judged by effect
                    # (windvane.stall), strikes staged for the next injection
                    # point (delivered in autonomy mode only).
                    try:
                        from windvane import stall as _stall

                        _turn_no = int(_cp.pressure_state(_st).get("stops_total", 0))
                        _stall.close_turn(_st, _turn_no, project_dir)
                        # Autonomy mode: at the cap, arm the halt, stage its
                        # text for the next injection point, and alert out of
                        # the session.
                        if _stall.maybe_halt(_st, _turn_no, project_dir):
                            _stall.stall_state(_st)["pending_halt"] = True
                            try:
                                from windvane import alerts as _alerts

                                _alerts.send(
                                    f"run {c._session_id[:8]} halted: no progress "
                                    f"{_stall.stall_state(_st)['strikes']}x, every tool denied; "
                                    "release with python -m windvane.stall release",
                                    project_dir,
                                    kind="halt",
                                    state=_st,
                                )
                            except Exception:
                                pass
                    except Exception:
                        pass
                    # The recorder's draft, banked when the turn edited and
                    # saved nothing deliberate.
                    try:
                        _maybe_bank_draft(_st, data if isinstance(data, dict) else {}, project_dir, _deliberate)
                    except Exception:
                        pass
                    save_state(_st)
                    # The /goal bracket (windvane.goal): keep the run record
                    # in step with the transcript's goal. windvane never
                    # blocks a Stop of its own -- /goal is the loop.
                    _goal_bracket(_st, data if isinstance(data, dict) else {}, project_dir, turn=True)
                except Exception:
                    pass

        # Live freshness tick: debounced incremental mine so search,
        # extractions, and code indexes track the session as it runs
        # instead of waiting for SessionEnd.
        _maybe_live_mine(project_dir)
    except Exception:
        pass


def _hook_stop_failure(project_dir: str) -> None:
    """StopFailure: the turn ended on an API error (rate_limit, overloaded,
    billing_error, max_output_tokens, ...). Output is ignored by Claude Code;
    this is the record -- when, which error, and for a usage limit every
    window the statusline mirror holds with its reset, so the run report can
    say when a resume makes sense."""
    try:
        stdin_data = _read_stdin_with_timeout(0.5)
        if not stdin_data:
            return
        data = json.loads(stdin_data)
        state = load_state()
        run = state.get("run")
        if not isinstance(run, dict):
            run = {}
            state["run"] = run
        rec = {
            "at": time.time(),
            "error_type": str(data.get("error_type") or "unknown"),
            "error": str(data.get("error") or "")[:300],
            "permission_mode": str(data.get("permission_mode") or ""),
        }
        try:
            from windvane import pressure as _cp

            mirror = _cp.read_mirror(c._session_id) or {}
            for key in ("five_hour_pct", "five_hour_resets_at", "seven_day_pct", "seven_day_resets_at"):
                if mirror.get(key) is not None:
                    rec[key] = mirror[key]
            windows = {}
            for kind, pct, resets_at in _cp.budget_windows(mirror):
                windows[str(kind)] = {"pct": pct, "resets_at": resets_at}
                if kind in ("five_hour", "seven_day"):
                    rec.setdefault(f"{kind}_pct", pct)
                    if resets_at is not None:
                        rec.setdefault(f"{kind}_resets_at", resets_at)
            if windows:
                rec["rate_limits"] = windows
        except Exception:
            pass
        run["failures"] = (list(run.get("failures") or []) + [rec])[-20:]
        run["last_failure"] = rec
        # Autonomy mode: the session may be dead after this; say so out of
        # band.
        try:
            from windvane import stall as _stall

            if _stall.autonomy_on(state):
                from windvane import alerts as _alerts

                reset = ""
                if rec.get("error_type") == "rate_limit" and rec.get("five_hour_resets_at"):
                    reset = " -- 5h window resets " + time.strftime(
                        "%H:%M", time.localtime(float(rec["five_hour_resets_at"]))
                    )
                _alerts.send(
                    f"run {c._session_id[:8]} stopped: {rec['error_type']}{reset}",
                    project_dir,
                    kind="failure",
                    state=state,
                )
        except Exception:
            pass
        save_state(state)
    except Exception:
        pass


def _hook_notification(project_dir: str) -> None:
    """Notification: in autonomy mode, a run waiting on a person is the
    alert that saves a night -- permission prompt, needs input, idle."""
    try:
        stdin_data = _read_stdin_with_timeout(0.5)
        if not stdin_data:
            return
        data = json.loads(stdin_data)
        from windvane import stall as _stall

        state = load_state()
        if not _stall.autonomy_on(state):
            return
        kind = str(data.get("notification_type") or data.get("type") or "").strip()
        if kind not in ("agent_needs_input", "permission_prompt", "idle_prompt"):
            return
        from windvane import alerts as _alerts

        msg = str(data.get("message") or "")[:120]
        text = f"run {c._session_id[:8]} waiting on you: {kind}" + (f" -- {msg}" if msg else "")
        _alerts.send(text, project_dir, kind="needs_input", state=state)
        save_state(state)
    except Exception:
        pass


def _hook_session_end(project_dir: str) -> None:
    """SessionEnd: {reason: 'clear'|'resume'|'logout'|'prompt_input_exit'|
    'other'}. The run report, the session's end, the post-session miner."""
    try:
        stdin_data = _read_stdin_with_timeout(0.5)
        reason = "other"
        data = None
        if stdin_data:
            data = json.loads(stdin_data)
            reason = data.get("end_reason", data.get("reason", "other"))

        # Gather session summary before clearing state
        state = load_state()
        files_edited = state.get("files_edited_this_session", [])

        # The run report: one auditable artifact per substantial session,
        # written into the project the session actually worked in
        # (<project>/.windvane/runs/). Before mark_session_ended so the
        # state it reads is the live one; the end reason is recorded first.
        try:
            from windvane import report as _rr

            _run_v = state.get("run")
            _run: dict = _run_v if isinstance(_run_v, dict) else {}
            _run["end_reason"] = str(reason)
            state["run"] = _run
            # A verdict that landed after the last Stop closes the goal run
            # here (windvane.goal).
            _goal_bracket(state, data if isinstance(data, dict) else {}, project_dir, turn=False)
            state["last_session_end"] = time.time()
            save_state(state)
            if _rr.substantial(state):
                _rr.write_report(
                    c._session_id,
                    session_project(project_dir, state),
                    state,
                )
        except Exception:
            pass

        mark_session_ended()

        # The end summary (kept for a caller that prints it; Claude Code
        # shows SessionEnd output to nobody).
        lines = [f"windvane session ended ({reason})."]
        if files_edited:
            lines.append(f"Files edited: {len(files_edited)}")
            for f in files_edited[:5]:
                lines.append(f"  - {Path(f).name}")
        project_memory = load_project_memory(project_dir)
        counts = get_memory_counts(project_memory)
        if counts.get("total", 0) > 0:
            lines.append(
                f"Memories: {counts['total']} total ({counts.get('mistake', 0)} mistakes, {counts.get('rule', 0)} rules)"
            )

        # Spawn background session miner (fire-and-forget, ~50ms)
        try:
            from windvane.mining.background import start_mining_background

            start_mining_background(project_dir, mode="post_session")
        except Exception:
            pass  # Mining not available -- skip silently
    except Exception:
        # Even if summary fails, make sure session state is saved
        try:
            mark_session_ended()
        except Exception:
            pass

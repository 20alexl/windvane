"""PreCompact and PostCompact.

- PreCompact (``pre_compact_json``): bank the recorder's draft of the whole
  checkpoint to the session project's ring (kind ``auto``), then index the
  transcript in the background while the detail is still in it.
- PostCompact (``post_compact_json``): bookkeeping only. Claude Code's hook
  output schema has no PostCompact entry (a hookSpecificOutput with that
  event name is rejected), and plain stdout never reaches the model, so the
  SessionStart(compact) banner carries everything the model must see. This
  hook opens the new pressure cycle (idempotent with the banner, whichever
  runs first) and pins what the compaction restored for the run report.
"""

import json
from pathlib import Path

from windvane.events import common as c
from windvane.events.common import (
    _get_session_context_for_handoff,
    _global_handoff_dir,
    _project_hash_dir,
    _read_stdin_with_timeout,
    get_handoff_data,
    load_state,
    save_state,
    session_project,
)


def _precompact_handoff(project_dir: str, state: dict, handoff_project: str, trigger: str, data: dict) -> dict:
    """The ring entry PreCompact banks: the recorder's draft of the whole
    checkpoint (windvane.draft), as an auto entry. The summary falls back to
    a count of the session's files, decisions and errors only when the draft
    has no handoff."""
    _run = state.get("run")
    transcript = str((data or {}).get("transcript_path") or "") or str(
        (_run if isinstance(_run, dict) else {}).get("transcript_path") or ""
    )
    from windvane import draft as _cd

    record, ctx = _cd.draft_with_context(project_dir, c._session_id, transcript, state)
    # A deliberate save made during the last turn (the Stop hook may not have
    # run yet when windvane compacts at the turn boundary), or one the
    # session has edited past, is brought up to this draft first: the brief
    # after the compaction restores the deliberate record, not the automatic
    # entry banked below.
    try:
        from windvane import pressure as _p

        _ps = _p.pressure_state(state)
        turn_saved = float(_ps.get("last_manual_checkpoint_at") or 0.0) > float(_ps.get("last_stop_at") or 0.0)
    except Exception:
        turn_saved = False
    if _cd.refresh_deliberate(record, project_dir, c._session_id, transcript, state, turn_saved=turn_saved) is not None:
        state["draft_refreshed"] = True
    if not ctx:
        ctx = _get_session_context_for_handoff(handoff_project)
    entry = _cd.ring_record(record, c._session_id, trigger)
    entry["project_path"] = handoff_project
    entry["decisions"] = entry["decisions"] or list(ctx.get("decisions") or [])
    entry["mistakes"] = list(ctx.get("mistakes") or [])
    if not entry["summary"]:
        files = entry["files_in_progress"]
        parts = [f"Context compacted ({trigger})."]
        if files:
            parts.append(f"{len(files)} files being edited: " + ", ".join(Path(f).name for f in files[:5]))
        if ctx.get("decisions"):
            parts.append("Decisions this session: " + "; ".join(ctx["decisions"]))
        if ctx.get("mistakes"):
            parts.append("Errors hit: " + "; ".join(ctx["mistakes"]))
        if ctx.get("prompts"):
            parts.append(f"{ctx['prompts']} prompts, {ctx.get('tests', 0)} tests this session.")
        if ctx.get("slow_tools"):
            parts.append("Slow tools: " + "; ".join(ctx["slow_tools"]))
        entry["summary"] = " ".join(parts)
    return entry


def _hook_pre_compact(project_dir: str) -> None:
    """PreCompact: bank the draft before context compaction."""
    try:
        stdin_data = _read_stdin_with_timeout(0.5)
        trigger = "auto"
        data = {}
        if stdin_data:
            data = json.loads(stdin_data)
            trigger = data.get("trigger", "auto")

        state = load_state()

        # Target the ring of the sub-project this session worked in, not
        # the cwd (same resolution the stop hook uses).
        handoff_project = session_project(project_dir, state)
        handoff = _precompact_handoff(project_dir, state, handoff_project, trigger, data)

        # Durable store: guarded latest pointer (autos never enter the
        # history ring; a fresh manual keeps the pointer and the
        # post-compact restore correctly anchors on it).
        from windvane import checkpoints as _hs

        _hs.write_handoff(
            handoff,
            [_project_hash_dir(handoff_project), _global_handoff_dir()],
        )
        # The refresh moved the state's edit mark; keep it.
        if state.pop("draft_refreshed", None):
            try:
                save_state(state)
            except Exception:
                pass

        # Compaction is the one point where mid-session mining pays off: the
        # detail about to be dropped from the context window is still in the
        # session JSONL, so index it now (background, lock-guarded) to keep
        # it searchable after the window shrinks.
        try:
            from windvane.mining.background import (
                is_mining_running,
                start_mining_background,
            )

            if not is_mining_running():
                start_mining_background(project_dir, mode="post_session")
        except Exception:
            pass
    except Exception:
        pass


def _hook_post_compact(project_dir: str) -> None:
    """PostCompact: open a new context-pressure cycle and record what the
    compaction restored. No output (see the module docstring)."""
    try:
        stdin_data = _read_stdin_with_timeout(0.5)
        if stdin_data:
            json.loads(stdin_data)  # compact_summary is not used

        try:
            from windvane import pressure as _cp

            _st = load_state()
            _cp.note_compaction(_st)
            save_state(_st)
        except Exception:
            pass

        # For the run report: which entry this compaction restored.
        handoff = get_handoff_data(project_dir)
        if handoff:
            try:
                from windvane import pressure as _cp2

                _st2 = load_state()
                _cp2.note_restored(_st2, handoff)
                save_state(_st2)
            except Exception:
                pass
    except Exception:
        pass

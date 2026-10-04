"""SessionStart: start the session and print the orientation banner.

The banner: the rules, the mistake count, the restored checkpoint (whole
after a compaction or on resume, a teaser on a fresh start), the last
session's activity, the recurring errors and the last targeted test that
passed (on resume; deferred to the first edit on a fresh start), the
compaction rhythm after a compaction, and the autonomy announcement. On a
fresh start the rule pack is seeded (and the project scaffold created only
when ``structure`` is on).
"""

import json
from pathlib import Path

from windvane.hooks import common as c
from windvane.hooks.common import (
    _banner_checkpoint,
    _banner_work_project,
    _compaction_briefed,
    _format_restored_context,
    _format_restored_full,
    _normalize_path,
    _project_label,
    _read_stdin_with_timeout,
    _recurring_lines,
    _rules_block,
    get_past_mistakes,
    get_windvane_storage_dir,
    load_project_memory,
    load_state,
    mark_session_started,
    save_state,
)


def _pack_lines(project_dir: str) -> list:
    """The default pack on a fresh start: rules seeded once per project
    (the strict tier too when ``strict_pack`` is on), the scaffold
    (CLAUDE.md, .learnings/, session-logs/) only when ``structure`` is on."""
    try:
        from windvane import rules as _rules

        return list(_rules.run_at_session_start(project_dir) or [])
    except Exception:
        return []


def _autonomy_line(project_dir: str) -> str:
    """Autonomy mode is announced, never silent: the halt is armed and
    alerts go where the owner pointed them (or nowhere)."""
    try:
        from windvane import alerts as _alerts
        from windvane import stall as _stall

        if not _stall.autonomy_on(load_state()):
            return ""
        _cap = _stall.strike_cap(project_dir)
        _where = "configured" if _alerts.alert_command(project_dir) else "NOT configured (recorded only)"
        return (
            f"AUTONOMY MODE: halt armed at strike {_cap} (every tool denied until released); "
            f"alert command {_where}. Park on Monitor/ScheduleWakeup rather than polling."
        )
    except Exception:
        return ""


def _last_session_lines(project_dir: str, work_project: str, resume_files: list) -> list:
    """Session mining: the last session's context (read-only, no building),
    the bootstrap of a project with history but no index, and the schema
    canary."""
    lines: list = []
    try:
        from windvane.mining.session_index import get_or_create_index

        # Resolve project hash dir (same logic as the memory store)
        _norm = str(Path(project_dir).resolve()).replace("\\", "/")
        if len(_norm) >= 2 and _norm[1] == ":":
            _norm = _norm[0].lower() + _norm[1:]
        # The store WINDVANE_DIR names, like every other read here.
        _storage = get_windvane_storage_dir()
        _manifest_path = _storage / "manifest.json"
        _hash_dir = None
        if _manifest_path.exists():
            _manifest = json.loads(_manifest_path.read_text())
            _proj_info = _manifest.get("projects", {}).get(_norm)
            if _proj_info:
                _hash_dir = _storage / "projects" / _proj_info["hash"]

        if _hash_dir and (_hash_dir / "session_index.json").exists():
            index = get_or_create_index(_hash_dir)
        else:
            index = None

            # Bootstrap: no index yet, but session JSONLs may exist
            if _hash_dir:
                try:
                    from windvane.mining.jsonl_reader import resolve_jsonl_dir

                    _jsonl_dir = resolve_jsonl_dir(project_dir)
                    if _jsonl_dir and any(_jsonl_dir.glob("*.jsonl")):
                        from windvane.mining.background import (
                            is_mining_running,
                            start_mining_background,
                        )

                        if not is_mining_running():
                            start_mining_background(project_dir, mode="bootstrap")
                            lines.append("Session mining: bootstrapping from history (background)...")
                except Exception:
                    pass

        if index and index.get_session_count() > 0:
            # An ancestor's index holds every sub-project's sessions:
            # ask for the latest one that touched THIS project.
            summary = index.get_latest_session_summary(
                work_project if _normalize_path(work_project) != _normalize_path(project_dir) else "",
                workspace_root=project_dir,
            )
            # A root-cwd start knows no sub-project yet, and the latest
            # session's files then spanned the whole workspace. The index
            # narrows a workspace-wide summary to the sub-project most of its
            # edits belong to and names it in project_label.
            _proj_label = str((summary or {}).get("project_label") or "")
            if summary and summary.get("file_count", 0) > 0:
                age = summary.get("age_str", "")
                branch = summary.get("branch", "")
                header = "Last session"
                if age or _proj_label:
                    header += " (" + ", ".join(p for p in (age, _proj_label and f"mostly {_proj_label}", branch and f"branch: {branch}") if p) + ")"
                lines.append(header + ":")
                files = summary.get("files_edited", [])
                if files:
                    lines.append(f"  Worked on: {', '.join(files[:8])}")
                    if summary["file_count"] > 8:
                        lines.append(f"  ...and {summary['file_count'] - 8} more files")
                errs = summary.get("error_count", 0)
                # prompt_count = real typed prompts; user_message_count
                # includes every tool result. Metas from before the field
                # show messages, labeled honestly.
                prompts = summary.get("prompt_count", 0)
                msgs = summary.get("user_message_count", 0)
                if errs or prompts or msgs:
                    parts = []
                    if prompts:
                        parts.append(f"{prompts} prompts")
                    elif msgs:
                        parts.append(f"{msgs} messages")
                    if errs:
                        parts.append(f"{errs} tool errors")
                    lines.append(f"  Activity: {', '.join(parts)}")

            # Recurring errors, struggles and the last targeted test are
            # scoped to the session's project. On a resume the transcript
            # names it; on a fresh start nothing does until the first edit,
            # so the block waits for the pre-edit hook
            # (state["patterns_deferred"]) instead of printing the cwd's
            # (the root's) errors.
            if resume_files:
                lines.extend(_recurring_lines(work_project, (summary or {}).get("files_edited_full", [])))
            else:
                try:
                    _dst = load_state()
                    _dst["patterns_deferred"] = True
                    save_state(_dst)
                except Exception:
                    pass

        # Schema canary: the miner flags when Claude Code's log format
        # stops being recognized (mining would degrade silently).
        try:
            status_path = get_windvane_storage_dir() / "mining_status.json"
            if status_path.exists():
                sdata = json.loads(status_path.read_text())
                warn = sdata.get("schema_warning", "")
                if warn:
                    lines.append(f"WARNING: {warn}")
        except Exception:
            pass
    except Exception:
        pass  # Mining not available or no sessions -- skip silently
    return lines


def _hook_session_start(project_dir: str) -> None:
    """SessionStart: start the session, the daemon, and print the
    orientation banner (rules, mistakes, restored checkpoint, last session,
    patterns, the last targeted test that passed)."""
    try:
        stdin_data = _read_stdin_with_timeout(0.5)
        source = "startup"
        _perm = ""
        _transcript = ""
        if stdin_data:
            data = json.loads(stdin_data)
            source = data.get("source", "startup")
            _perm = str(data.get("permission_mode", "") or "")
            _transcript = str(data.get("transcript_path", "") or "")

        # Capture the resuming session's OWN edited files BEFORE
        # mark_session_started wipes the per-session list below. They are
        # the only concurrency-safe signal for which sub-project this
        # session is about -- the pooled "last session" may belong to a
        # different concurrent session in a different project.
        work_project, resume_files = _banner_work_project(project_dir, source, _transcript)

        mark_session_started(
            project_dir,
            permission_mode=_perm,
            transcript_path=_transcript,
            source=str(source or "startup"),
        )

        # Start the daemon in the background (non-blocking; returns at once
        # when one is already running).
        try:
            from windvane.daemon import start_server_background

            start_server_background()
        except Exception:
            pass

        lines = []
        lines.append(f"windvane session started ({source})")
        # No statusLine means no context reading: say so here, once,
        # rather than let the pressure nudges be silently absent.
        try:
            from windvane import pressure as _cp

            _no_sl = _cp.session_start_text(project_dir, c._session_id)
            if _no_sl:
                lines.append(_no_sl)
            # After a compaction: open the new pressure cycle (idempotent
            # with the PostCompact hook, whichever ran first) and state the
            # rhythm here, the one hook output Claude Code accepts after a
            # compaction (its schema has no PostCompact channel).
            if source == "compact":
                _cst = load_state()
                _cp.note_compaction(_cst)
                save_state(_cst)
                lines.append(_cp.rhythm_text(_cst, c._session_id, project_dir))
            _auto = _autonomy_line(project_dir)
            if _auto:
                lines.append(_auto)
        except Exception:
            pass
        # The default pack on a fresh start only.
        if source == "startup":
            lines.extend(_pack_lines(project_dir))

        # Load and show key context (CLAUDE.md-covered rules skipped). The
        # store is the session's project, the same one the prompt hook
        # counts.
        project_memory = load_project_memory(work_project)
        mistakes = get_past_mistakes(project_memory, work_project)
        _label = _project_label(work_project)

        _briefed = _compaction_briefed(source, c._session_id)
        if not _briefed:
            lines.extend(_rules_block(work_project, project_memory))
        if mistakes:
            _own = sum(1 for m in mistakes if m.get("scope", 0) == 0)
            lines.append(
                f"Past mistakes: {len(mistakes)} tracked for {_label}"
                + (f" ({_own} its own, the rest pooled from ancestors)" if _own != len(mistakes) else "")
                + " (file-specific, shown before edits)"
            )

        # Restored context: checkpoint + handoff are one ring construct;
        # show it once. WHICH ring to read depends on how the session began:
        #   resume  -> this session's own files name its sub-project; read
        #              that ring (walk-up) so its manual handoff wins over
        #              the workspace root's per-turn autos.
        #   fresh   -> unknowable which sub-project comes next; prefer the
        #              newest MANUAL across the workspace subtree (a labeled
        #              breadcrumb), else the plain walk-up result.
        #   compact -> same as resume. PostCompact's plain stdout never
        #              reaches the model, so this banner is where the
        #              checkpoint the model just banked is shown to it.
        # On resume and compact THIS SESSION's own newest deliberate
        # checkpoint comes first (session_id on the ring record), skipping
        # one the user rewound past, and the record is shown whole. The
        # project's newest is the fallback for a session that has banked
        # nothing yet.
        restored, skipped = _banner_checkpoint(project_dir, work_project, source, bool(resume_files), _transcript)
        if restored and not _briefed:
            if source in ("resume", "compact"):
                lines.extend(_format_restored_full(restored, skipped))
            else:
                lines.extend(_format_restored_context(restored))

        lines.extend(_last_session_lines(project_dir, work_project, resume_files))

        # Never set hookSpecificOutput.sessionTitle here. The session name
        # belongs to Claude Code and the user's /rename; this hook also fires
        # on resume, and in a workspace the restored checkpoint can belong to
        # a different sub-project than the session it would rename.
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": "\n".join(lines),
                    }
                }
            )
        )
    except Exception:
        pass

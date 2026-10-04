"""UserPromptSubmit: decision capture, the /goal bracket, and the start
banner when no SessionStart ran.

A typed prompt is judged once by ``windvane.capture.capture_decision`` (the
same function the miner uses) and a decision is stored in the session's
project. The last prompt is kept for the compliance check (an approval the
destructive-command rule reads). The context-pressure nudges ride along.
"""

import hashlib
import json
import re
import time

from windvane.hooks.common import (
    _append_memory_entry,
    _append_memory_summary,
    _format_restored_context,
    _goal_bracket,
    _project_label,
    _read_stdin_with_timeout,
    _truncate,
    _with_pressure,
    check_session_active,
    emit_context,
    filter_rules_in_claude_md,
    get_checkpoint_data,
    get_handoff_data,
    get_past_mistakes,
    get_project_rules,
    load_project_memory,
    load_state,
    mark_session_started,
    save_state,
    session_project,
)


def should_show_full_reminder(project_dir: str, prompt: str = "") -> tuple[bool, str]:
    """
    Determine if we should show the full reminder block.

    Returns:
        (should_show, reason) - reason explains why we're showing/not showing

    CONTEXT-AWARE LOGIC:
    - Session not active: Auto-start and show welcome
    - First prompt of session: Show welcome
    - Checkpoint exists to restore: Remind once
    - Otherwise: SILENT (no injection)
    """
    state = load_state()
    session_active = check_session_active(project_dir)

    # CASE 1: Session not active - will auto-start, show welcome
    if not session_active:
        return (True, "auto_start_session")

    # Session IS active - track prompts within session
    state["prompts_this_session"] = state.get("prompts_this_session", 0) + 1
    prompts_this_session = state["prompts_this_session"]
    save_state(state)

    # CASE 2: First prompt of active session - show welcome
    if prompts_this_session == 1:
        return (True, "first_prompt_of_session")

    # CASE 3: Checkpoint exists and we haven't reminded yet
    checkpoint = get_checkpoint_data(project_dir)
    if checkpoint and not state.get("checkpoint_reminded", False):
        state["checkpoint_reminded"] = True
        save_state(state)
        return (True, "checkpoint_exists")

    # CASE 4: Otherwise - SILENT
    return (False, "no_reminder_needed")


def reminder_for_prompt(project_dir: str, prompt: str = "") -> str:
    """
    Generate the reminder for the UserPromptSubmit hook.

    CONTEXT-AWARE: shows something only when there's a good reason. A session
    that never got a SessionStart is started here and gets the welcome
    (restored checkpoint, rules, mistakes); an active session is silent.
    """
    # CHECK IF WE SHOULD SHOW ANYTHING AT ALL
    should_show, reason = should_show_full_reminder(project_dir, prompt)
    if not should_show:
        return ""  # SILENT - no injection

    session_active = check_session_active(project_dir)
    project_memory = load_project_memory(project_dir)

    lines = ["<windvane-reminder>"]

    if not session_active:
        # Start the session here: the SessionStart hook did not run.
        mark_session_started(project_dir)

        lines.append("windvane session auto-started")
        lines.append("")

        # AUTO-LOAD restored context (checkpoint + handoff are one ring construct)
        restored = get_handoff_data(project_dir)
        if restored:
            lines.append("---")
            lines.append("")
            lines.append("CONTEXT RESTORED FROM PREVIOUS SESSION")
            lines.append("")
            lines.extend(_format_restored_context(restored))
            lines.append("")
            lines.append("CONTINUE FROM WHERE YOU LEFT OFF")
            lines.append("")
            lines.append("---")
            lines.append("")

        # Show RULES first (always follow these) - with IDs for management.
        # Rules already present in CLAUDE.md are skipped: that file is in
        # context every turn, so re-injecting them is pure token waste.
        rules = filter_rules_in_claude_md(
            get_project_rules(project_memory), project_dir
        )
        if rules:
            lines.append(f"RULES ({len(rules)}, {_project_label(project_dir)}) - always follow:")
            for r in rules[:5]:  # Show top 5 rules
                lines.append(f"  [{r['id']}] {_truncate(r['content'], 120)}")
            lines.append("")

        # Show past mistakes (newest first) - with IDs for management
        mistakes = get_past_mistakes(project_memory, project_dir)
        if mistakes:
            # Only this project's own mistakes (or pooled ones that name a
            # file here) are listed; the workspace store's pool from every
            # sibling session is counted, not shown -- it surfaces before an
            # edit when it names the file being edited.
            own = [m for m in mistakes if m.get("scope", 0) == 0]
            pooled = len(mistakes) - len(own)
            if own:
                lines.append(f"PAST MISTAKES ({len(own)} of this project) - avoid repeating:")
                for m in own[:5]:  # newest first within the project
                    lines.append(f"  [{m['id']}] {_truncate(m['content'], 100)}")
                if pooled:
                    lines.append(f"  (+{pooled} pooled from the workspace store; shown before edits when they name the file)")
            else:
                lines.append(
                    f"PAST MISTAKES: none recorded for this project; {pooled} pooled from the workspace "
                    "store surface before edits when they name the file."
                )
            lines.append("")

        # Show memory summary and management hints
        _append_memory_summary(lines, project_memory, project_dir)

    else:
        # Session is active -- stay silent. Rules were shown at SessionStart.
        # Mistakes are file-specific (handled by PreToolUse Edit injection).
        return ""

    lines.append("</windvane-reminder>")
    return "\n".join(lines)


def _auto_capture_from_prompt(project_dir: str, prompt: str):
    """
    Auto-capture decisions from user prompts.

    The judgement is windvane.capture.capture_decision, the same function
    the session miner applies to a correction it finds in a transcript: the
    semantic tier when the daemon is reachable, the regex tier over each
    sentence, the capture threshold, then the shape gate. Only the prompt
    pre-filter (length, a slash command, pasted markup) lives here.
    Does NOT log the full prompt (privacy).
    """
    prompt_lower = prompt.lower().strip()

    if (
        len(prompt_lower) < 25
        or prompt_lower.startswith("/")
        or prompt_lower.startswith("<")
    ):
        return

    try:
        from windvane.capture import capture_decision

        best_text = capture_decision(prompt)
        if not best_text:
            return

        content = f"DECISION: (from user) {best_text}"
        entry_id = hashlib.md5(content.encode()).hexdigest()[:12]

        new_words = set(best_text.lower().split())

        def _near_duplicate_decision(existing_entries: list) -> bool:
            for e in existing_entries:
                if e.get("category") != "decision":
                    continue
                existing_words = set(e.get("content", "").lower().split())
                if existing_words and new_words:
                    overlap = len(new_words & existing_words) / len(
                        new_words | existing_words
                    )
                    if overlap > 0.7:
                        return True
            return False

        file_refs = re.findall(
            r"[\w/\\.-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|cpp|c|h|md|json|yaml|yml|toml)",
            best_text,
        )

        _append_memory_entry(
            project_dir,
            {
                "id": entry_id,
                "content": content,
                "category": "decision",
                "source": "auto-prompt",
                "relevance": 6,
                "created_at": time.time(),
                "last_accessed": time.time(),
                "access_count": 1,
                "tags": ["decision"],
                "related_files": file_refs[:5],
            },
            skip_if=_near_duplicate_decision,
        )
    except Exception:
        pass  # Silent failure -- auto-capture must never break the hook


def _hook_prompt(project_dir: str) -> None:
    """UserPromptSubmit: keep the last prompt, observe the /goal bracket,
    capture a decision, and deliver the reminder and the nudges."""
    try:
        stdin_data = _read_stdin_with_timeout(0.5)
        prompt_text = ""
        data = {}
        if stdin_data:
            data = json.loads(stdin_data)
            prompt_text = data.get("prompt", "")

        # Resolve project from recently edited files (not just cwd)
        state = load_state()
        # The last prompt is what the destructive-command rule reads for
        # approval ("approved", "delete it") -- the detector alone cannot
        # see that the person just said yes (windvane.compliance.rule_text).
        if prompt_text:
            state["last_prompt"] = str(prompt_text)[:600]
            save_state(state)
        # A goal set or ended since the last stop (windvane.goal).
        _goal_bracket(state, data if isinstance(data, dict) else {}, project_dir, turn=False)
        project_dir = session_project(project_dir, state)

        # Auto-capture decisions from user prompts
        if prompt_text and len(prompt_text) > 20:
            _auto_capture_from_prompt(project_dir, prompt_text)

        result = reminder_for_prompt(project_dir, prompt_text)
        result = _with_pressure(result, project_dir)
        emit_context("UserPromptSubmit", result)
    except Exception:
        pass

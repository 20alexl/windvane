"""PreToolUse: before an edit, a read, a shell command, and every tool.

- Edit/Write (``pre_edit_json``): the file's past mistakes, the loop
  warning, TODOs in the file, the scored memories for the file, the
  deferred recurring-errors block, the import precheck and the blast radius.
- Read (``pre_read_json``): the code-index orientation and the file's most
  relevant memories, once per file per session.
- Bash/PowerShell (``pre_bash_json``): rules with a detector are matched;
  a match is recorded and the rule injected before the command runs (or the
  call denied when the detector says so in autonomy mode).
- Every tool (``pre_tool_json``): the autonomy halt (a one-read no-op
  unless halted).

Each injection point also delivers the context-pressure nudges.
"""

import json
import os
import re
import time
from pathlib import Path

from windvane.hooks.common import (
    _compliance_check,
    _file_mistake_lines,
    _file_mistakes,
    _increment_tool_usage,
    _is_code_file,
    _read_stdin_with_timeout,
    _recurring_lines,
    _truncate,
    _with_pressure,
    check_session_active,
    emit_context,
    emit_deny,
    get_contextual_memories,
    get_loop_status,
    get_project_dir,
    load_project_memory,
    load_state,
    mark_session_started,
    save_state,
    session_project,
    under_non_project_dir,
)


# ============================================================================
# Edit / Write
# ============================================================================


def _auto_run_pre_edit_check(project_dir: str, file_path: str) -> dict:
    """
    Run the pre-edit check and return its results.

    Returns dict with:
    - past_mistakes: list of relevant mistakes
    - loop_warnings: list of loop detector warnings
    - suggestions: immediately useful suggestions
    - contextual_memories: the scored memories for the file (when any)
    """
    results = {
        "past_mistakes": [],
        "loop_warnings": [],
        "suggestions": [],
    }

    # Check memory for past mistakes
    project_memory = load_project_memory(project_dir)
    file_name = Path(file_path).name
    results["past_mistakes"] = _file_mistakes(project_memory, file_path)

    # Check loop detector -- test-aware (not just edit count)
    loop_status = get_loop_status()
    edits = loop_status.get("edit_counts", {})
    edit_count = edits.get(file_path, 0) or edits.get(file_name, 0)
    test_results = loop_status.get("recent_test_results", [])

    # Only warn with evidence of trouble. "Without running tests" is a code
    # signal: eight edits to a markdown, config or data file are a document
    # being written, and tests have nothing to say about it. A file under a
    # scratch or vendored tree is somebody's working notes, never a loop.
    # Latched: the warning speaks at the threshold and again every
    # threshold after it, not on every edit past it (a frontend file with
    # no tests drew ~400 of them in one session).
    if under_non_project_dir(file_path):
        pass
    elif test_results:
        last_failing = not test_results[-1].get("passed", True)
        if last_failing and edit_count >= 3 and edit_count % 3 == 0 and _is_code_file(file_path):
            results["loop_warnings"].append(
                f"{edit_count} edits to {file_name}, tests still failing"
            )
    elif edit_count >= 8 and edit_count % 8 == 0 and _is_code_file(file_path):
        results["loop_warnings"].append(
            f"{edit_count} edits to {file_name} without running its targeted tests"
        )

    try:
        file_obj = Path(file_path)

        # 1. TODOs/FIXMEs in the file
        if file_obj.exists() and file_obj.is_file():
            try:
                content = file_obj.read_text()
                todos = []
                for line_num, line in enumerate(content.split("\n")[:500], 1):  # First 500 lines
                    if (
                        "TODO" in line
                        or "FIXME" in line
                        or "XXX" in line
                        or "HACK" in line
                    ):
                        todos.append(f"L{line_num}: {line.strip()[:60]}")
                if todos:
                    results["suggestions"].append(
                        f"{len(todos)} TODO/FIXME in file: {', '.join(todos[:2])}"
                    )
            except Exception:
                pass

        # 2. Contextual memory injection -- reuses the entries loaded above
        # (one memory.json parse per hook, not two) and excludes mistakes
        # already shown in the past-mistakes section (the same mistake in two
        # sections of one banner is pure token waste).
        contextual_memories = get_contextual_memories(
            project_dir,
            file_path,
            entries=project_memory.get("entries", []),
            exclude=results["past_mistakes"],
        )
        if contextual_memories:
            results["contextual_memories"] = contextual_memories

    except Exception:
        # Fallback to basic suggestions if rich context fails
        if "test" in file_name.lower():
            results["suggestions"].append("Run the targeted tests after editing")
        if edit_count >= 2:
            results["suggestions"].append("Edited multiple times - review approach")

    # Mark that we ran the check
    state = load_state()
    state["last_pre_edit_check"] = time.time()
    state["last_pre_edit_file"] = file_path
    _increment_tool_usage(state, "work_pre_edit_check")
    save_state(state)

    return results


def reminder_for_edit(project_dir: str, file_path: str = "") -> str:
    """
    The PreToolUse (Edit/Write) block: runs the pre-edit check and shows its
    results (past mistakes, loop warnings, suggestions, memories). Starts
    the session when no SessionStart ran.
    """
    session_active = check_session_active(project_dir)

    lines = ["<windvane-edit-reminder>"]
    has_content = False

    auto_check_results = None
    if session_active and file_path:
        try:
            auto_check_results = _auto_run_pre_edit_check(project_dir, file_path)
        except Exception:
            pass

    if auto_check_results:
        if auto_check_results["past_mistakes"]:
            lines.extend(_file_mistake_lines(auto_check_results["past_mistakes"]))
            lines.append("")
            has_content = True

        if auto_check_results["loop_warnings"]:
            lines.append("AUTO-CHECK: Loop detection:")
            for w in auto_check_results["loop_warnings"]:
                lines.append(f"  • {w}")
            lines.append("")
            has_content = True

        if auto_check_results["suggestions"]:
            lines.append("AUTO-CHECK: Suggestions:")
            for s in auto_check_results["suggestions"]:
                lines.append(f"  • {s}")
            lines.append("")
            has_content = True

        if auto_check_results.get("contextual_memories"):
            lines.append("Relevant memories for this file:")
            for m in auto_check_results["contextual_memories"]:
                lines.append(f"  • {m}")
            lines.append("")
            has_content = True

    if not session_active:
        # Start silently -- the SessionStart hook handles this, but if it
        # didn't fire, start without nagging.
        mark_session_started(project_dir)

    # Edit tracking happens post-edit (PostToolUse) -- recording here would
    # count edits that were denied or failed before they ever ran.

    lines.append("</windvane-edit-reminder>")

    if has_content:
        return "\n".join(lines)
    return ""


def _proposed_content(data) -> str:
    """The text an Edit/Write/MultiEdit is about to put in the file."""
    ti = data.get("tool_input", {}) if isinstance(data, dict) else {}
    proposed = ti.get("new_string") or ti.get("content") or ""
    if not proposed and isinstance(ti.get("edits"), list):
        proposed = "\n".join(
            e.get("new_string", "") for e in ti["edits"] if isinstance(e, dict)
        )
    return proposed


def _hook_pre_edit(project_dir: str) -> None:
    """PreToolUse(Edit|Write): the pre-edit block, the deferred recurring
    errors, the import precheck and the blast radius."""
    try:
        stdin_data = _read_stdin_with_timeout(0.5)
        file_path = ""
        agent_id = ""
        # Bound up front: `data` is read again further down (the pre-edit
        # import check); on an empty stdin it must not be unbound.
        data = None
        if stdin_data:
            data = json.loads(stdin_data)
            file_path = data.get("tool_input", {}).get("file_path", "")
            agent_id = data.get("agent_id", "")

        # Skip memory injection for subagents -- they have limited context
        if agent_id or not file_path:
            return
        # Resolve sub-project from the file being edited
        project_dir = get_project_dir(file_path)
        result = reminder_for_edit(project_dir, file_path)
        # A fresh start deferred the recurring-errors block until the
        # project was known; the first edit names it.
        try:
            _dst = load_state()
            if _dst.get("patterns_deferred"):
                _dst["patterns_deferred"] = False
                save_state(_dst)
                _pl = _recurring_lines(session_project(project_dir, _dst), [])
                if _pl:
                    result = "\n".join(_pl) + ("\n" + result if result else "")
        except Exception:
            pass

        # Pre-edit import/export verification off the code index. Reads the
        # PROPOSED content; advisory, conservative, silent on anything it
        # can't verify with high confidence.
        try:
            proposed = _proposed_content(data)
            if proposed:
                from windvane.precheck import precheck_edit

                pc = precheck_edit(file_path, proposed, project_dir)
                if pc:
                    result = (result or "") + "\n" + pc
        except Exception:
            pass

        # Blast radius: how many project modules import this one (reads the
        # cached reverse-edges; silent for near-leaf modules).
        try:
            from windvane.precheck import blast_radius

            br = blast_radius(file_path, project_dir)
            if br:
                result = (result or "") + "\n" + br
        except Exception:
            pass

        result = _with_pressure(result, project_dir)
        emit_context("PreToolUse", result)
    except Exception:
        pass


# ============================================================================
# Read
# ============================================================================


def _hook_pre_read(project_dir: str = "") -> None:
    """PreToolUse(Read): orientation before Read -- code-index summary + the
    most relevant memories for the file, once per file per session -- plus
    the context-pressure nudge when one is due."""
    try:
        stdin_data = _read_stdin_with_timeout(0.5)
        if not stdin_data:
            return
        data = json.loads(stdin_data)
        file_path = data.get("tool_input", {}).get("file_path", "")

        # Optional statusline integration: mirror the last-read file to a
        # plain text file (replaces a separate user hook = one less spawn).
        if file_path:
            lf = os.environ.get("WINDVANE_LAST_FILE_PATH", "")
            if lf:
                try:
                    Path(lf).expanduser().write_text(file_path, encoding="utf-8")
                except Exception:
                    pass

        # Subagents: no injection (preserve their context budget)
        if data.get("agent_id") or not file_path:
            return

        # Once per file per session -- re-reads shouldn't re-pay the tokens
        state = load_state()
        seen = state.get("read_injected", [])
        norm = file_path.replace("\\", "/").lower()
        if norm in seen:
            return
        seen.append(norm)
        state["read_injected"] = seen[-50:]
        save_state(state)

        project_dir = get_project_dir(file_path)
        lines = []
        try:
            from windvane.precheck import read_context

            rc = read_context(file_path, project_dir)
            if rc:
                lines.append(rc)
        except Exception:
            pass
        for mem in get_contextual_memories(project_dir, file_path)[:2]:
            lines.append(f"- {mem}")

        result = ""
        if lines:
            result = "<windvane-read-context>\n" + "\n".join(lines) + "\n</windvane-read-context>"
        result = _with_pressure(result, project_dir)
        emit_context("PreToolUse", result)
    except Exception:
        pass


# ============================================================================
# Bash / PowerShell: the compliance trail's live half
# ============================================================================


_APPROVAL = re.compile(
    r"\b(?:approved?|go ahead|proceed|yes,? (?:do|delete|remove|trash|rm|go)|"
    r"(?:delete|remove|trash|rm|kill|drop) (?:it|them|that|those|the \w+)|do it)\b",
    re.IGNORECASE,
)


def _rule_context(state: dict, tool_input) -> str:
    """What the session knows that the detector cannot: the last prompt
    reads as approval; the targets are paths this session created."""
    notes = []
    prompt = str(state.get("last_prompt") or "")
    if prompt and _APPROVAL.search(prompt):
        notes.append(f'The last prompt reads as approval: "{_truncate(prompt.strip(), 80)}".')
    created = [str(x).lower() for x in state.get("created_paths") or []]
    cmd = str((tool_input or {}).get("command") or "") if isinstance(tool_input, dict) else ""
    if created and cmd:
        targets = [t for t in re.findall(r"(?:\"([^\"]+)\"|'([^']+)'|(\S+))", cmd)]
        targets = [next(x for x in t if x) for t in targets]
        paths = [t.replace("\\", "/").lower() for t in targets[1:] if not t.startswith("-") and ("/" in t or "\\" in t or "." in t)]
        if paths and all(any(p == cr or p.startswith(cr.rstrip("/") + "/") or p.endswith("/" + cr) or p == cr.rsplit("/", 1)[-1] for cr in created) for p in paths):
            notes.append("Every target is a path this session created.")
    return " ".join(notes)


def _hook_pre_bash(project_dir: str) -> None:
    """PreToolUse on Bash / PowerShell: the compliance trail's live half.
    A command that matches a rule's detector is recorded and the rule is
    injected before it runs -- the reminder at the moment it matters."""
    try:
        stdin_data = _read_stdin_with_timeout(0.5)
        if not stdin_data:
            return
        data = json.loads(stdin_data)
        tool_name = str(data.get("tool_name") or "Bash")
        project_dir = session_project(project_dir)  # the rules in scope are the session's project's
        new, state = _compliance_check(project_dir, data, tool_name, data.get("tool_input"))
        from windvane import compliance as _cpl

        # Autonomy mode: an ask-first rule cannot be asked, so a detector
        # marked deny refuses the call (a deny ends nothing but this call;
        # the reason tells the model to record what it needs and go on).
        denied = _cpl.should_deny(new, str(data.get("permission_mode") or ""), state) if new else []
        if denied and state is not None:
            for m in state.get("compliance", {}).get("matches", [])[-len(new):]:
                if m.get("rule_id") in {d["rule_id"] for d in denied}:
                    m["verdict"] = "denied"
        if state is not None:
            save_state(state)
        if denied:
            emit_deny(_cpl.deny_text(denied))
            return
        if data.get("agent_id"):
            return  # recorded (flagged as a subagent's), never nudged

        _ctx = _rule_context(state or {}, data.get("tool_input")) if new else ""
        result = _cpl.rule_text(new, str(data.get("permission_mode") or ""), _ctx) if new else ""
        result = _with_pressure(result, project_dir)
        emit_context("PreToolUse", result)
    except Exception:
        pass


# ============================================================================
# Every tool: the halt
# ============================================================================


def _hook_pre_tool(project_dir: str) -> None:
    """PreToolUse on every tool: the halt. Fast path when nothing is halted
    (one state read, no output). Halted: deny everything but the calls that
    let the model leave a record; the reason is shown to the model."""
    try:
        stdin_data = _read_stdin_with_timeout(0.5)
        if not stdin_data:
            return
        data = json.loads(stdin_data)
        from windvane import stall as _stall

        state = load_state()
        if not _stall.halted(state):
            return
        tool_name = str(data.get("tool_name") or "")
        if tool_name in _stall.HALT_ALLOWED_TOOLS:
            return
        if data.get("agent_id"):
            # A subagent shares the session id. The halt starves the MAIN
            # loop (Agent itself is denied there); an agent already
            # dispatched finishes and reports. Denying it left it with no
            # way to say so (its SendMessage to the parent included).
            return
        _stall.note_denied(state)
        save_state(state)
        emit_deny(_stall.deny_reason(state, tool_name))
    except Exception:
        pass

"""Shared pieces of the hook events.

Every hook event module (``session_start``, ``prompt``, ``pre_tool``,
``post_tool``, ``stop``, ``compact``) imports from here: the per-call stdin
cache and session id, the per-session hook state, session scoping
(``session_project``), the checkpoint banner pieces, the test-run
classifier, and the nudge delivery (``_with_pressure``) every injection
point shares. The store-side modules (``windvane.brief``,
``windvane.draft``, ``windvane.checkpoints``, ``windvane.tools``) and the
daemon read the same names.

The two module globals ``_stdin_cache`` and ``_session_id`` are per call:
a hook process sets them once; the daemon, which runs many calls in one
process, sets and resets them around each (``windvane.hooks.dispatch``).
Read them as ``common._session_id``, never through ``from ... import``,
which would copy the value at import time.
"""

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

# Cached once per hook invocation: the raw stdin payload and the Claude Code
# session id parsed from it. State is keyed by this id so two sessions from the
# same workspace don't clobber each other's working state (see get_state_file).
_stdin_cache: "str | None" = None
_session_id: str = ""


def _read_stdin_with_timeout(timeout_secs: float = 0.5) -> str:
    """
    Cross-platform stdin reader with timeout. Works on Windows and Unix.

    Memoized: a hook invocation delivers a single JSON payload on stdin and
    several code paths want it, so the first read is cached and reused.
    """
    global _stdin_cache
    if _stdin_cache is not None:
        return _stdin_cache

    result = {"data": ""}

    def read_stdin():
        try:
            result["data"] = sys.stdin.read()
        except Exception:
            pass

    thread = threading.Thread(target=read_stdin)
    thread.daemon = True
    thread.start()
    thread.join(timeout=timeout_secs)

    _stdin_cache = result["data"]
    return _stdin_cache


def _init_session_id() -> None:
    """Parse session_id from the (cached) hook stdin so per-session state files
    don't collide between concurrent sessions. No-op without stdin (e.g. a
    plugin tool call, which calls adopt_env_session_id instead)."""
    global _session_id
    try:
        raw = _read_stdin_with_timeout(0.5)
        if raw:
            sid = json.loads(raw).get("session_id", "")
            if sid:
                _session_id = str(sid)
    except Exception:
        pass


def adopt_env_session_id() -> str:
    """Adopt the session id Claude Code exports to its subprocesses.

    Claude Code puts CLAUDE_CODE_SESSION_ID in the environment of stdio MCP
    servers, hooks and Bash. A caller with no hook stdin to parse (a plugin
    tool call, a CLI run from the session) would otherwise fall through to the
    shared hook_state.json -- a file the per-session hooks never write -- and
    report a stale session's stats.

    Deliberately NOT folded into get_state_file(): the daemon serves many
    sessions from one long-lived process and clears _session_id between
    requests, so an ambient env fallback there would resurrect exactly the
    cross-session leak that reset exists to prevent. Only callers whose
    process maps 1:1 to a session opt in.

    Fills in only when no id is already known: a stdin-derived id is per-call
    truth, while the environment is process-level and outlives any one payload,
    so stdin always wins. That also makes this idempotent and order-independent.

    Returns the effective id, or "" when the variable is unset (older Claude
    Code) -- in which case the shared-file fallback still applies."""
    global _session_id
    if _session_id:
        return _session_id
    try:
        sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
        if sid:
            _session_id = sid
    except Exception:
        pass
    return _session_id


# ---------------------------------------------------------------------------
# Project paths, manifest, and per-project memory loading live in
# windvane.paths and windvane.storage. Re-exported here so every hook module
# and every caller of ``windvane.hooks.common`` resolves them in one place.
# ---------------------------------------------------------------------------
from windvane.paths import (  # noqa: E402,F401
    _GENERIC_BASENAMES,
    _PROJECT_MARKERS,
    _get_manifest,
    _global_handoff_dir,
    _handoff_candidate_dirs,
    _normalize_path,
    _project_dir_cache,
    _project_hash_dir,
    get_memory_file,
    get_project_dir,
    get_project_memory_dir,
    get_windvane_storage_dir,
    resolve_project_for_file,
    under_non_project_dir,
)
from windvane.storage import (  # noqa: E402,F401
    _load_project_data_from_dir,
    _load_project_entries_from_dir,
    filter_rules_in_claude_md,
    get_memory_counts,
    get_past_mistakes,
    get_project_rules,
    load_project_memory,
)


# ============================================================================
# State Tracking - the per-session hook state
# ============================================================================


def get_state_file() -> Path:
    """Path to the per-session hook-state file.

    Keyed by the Claude Code session_id so two sessions launched from the same
    workspace don't clobber each other's working state. Hooks learn the id from
    stdin (_init_session_id); other callers adopt it from the environment
    (adopt_env_session_id). Falls back to the shared hook_state.json only when
    no id is available at all -- an older Claude Code, or an out-of-band caller.
    Honors WINDVANE_DIR (the test-isolation seam) like every other storage
    path."""
    base = get_windvane_storage_dir()
    if _session_id:
        return base / "sessions" / f"{_session_id}.json"
    return base / "hook_state.json"


def _prune_session_states(max_age_days: float = 7.0):
    """Delete per-session hook-state files older than ``max_age_days``. They
    accumulate one per session under <store>/sessions/; each session keys its
    own, so a stale one is never read again -- pure clutter."""
    sessions_dir = get_windvane_storage_dir() / "sessions"
    if not sessions_dir.is_dir():
        return
    cutoff = time.time() - max_age_days * 86400
    for f in sessions_dir.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def load_state() -> dict:
    """Load hook state."""
    state_file = get_state_file()
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except Exception:
            pass
    return {
        "prompts_without_session": 0,
        "prompts_this_session": 0,  # Track prompts within active session
        "edits_without_session": 0,
        "edits_without_pre_check": 0,
        "edits_without_loop_record": 0,
        "tests_without_record": 0,
        "checkpoint_reminded": False,  # Track if we've shown checkpoint reminder
        "last_session_start": None,
        "last_session_end": None,
        "last_pre_edit_check": None,
        "last_loop_record": None,
        "last_test_record": None,
        "last_mistake_log": None,
        "files_edited_this_session": [],
        "last_session_files": [],  # Files from previous session (for curated context)
        "ignored_warnings": 0,
        "active_project": "",
        # Test state tracking - only remind on meaningful test runs
        "last_test_passed": None,  # None = unknown, True = passed, False = failed
        "test_runs_this_session": 0,
        # Tool usage tracking - helps identify underused tools
        "tool_usage": {
            "session_start": 0,
            "memory_remember": 0,
            "memory_recall": 0,
            "work_log_mistake": 0,
            "work_log_decision": 0,
            "work_pre_edit_check": 0,
            "loop_record_edit": 0,
            "loop_record_test": 0,
            "impact_analyze": 0,
            "context_checkpoint_save": 0,
        },
    }


def save_state(state: dict):
    """Save hook state atomically (temp file + atomic replace).

    Two Claude sessions launched from the same workspace write hook state
    concurrently; a plain write can be read half-finished or interleaved.
    A pid-tagged temp file + os.replace makes each write all-or-nothing.
    """
    state_file = get_state_file()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp = state_file.parent / f"{state_file.name}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(state_file)
    except Exception:
        pass


def _increment_tool_usage(state: dict, tool_name: str):
    """Increment usage count for a tool."""
    if "tool_usage" not in state:
        state["tool_usage"] = {}
    state["tool_usage"][tool_name] = state["tool_usage"].get(tool_name, 0) + 1


def _track_tool_duration(state: dict, tool_name: str, duration_ms: int):
    """Track tool execution duration for session diagnostics."""
    if "tool_durations" not in state:
        state["tool_durations"] = {}
    durations = state["tool_durations"]
    if tool_name not in durations:
        durations[tool_name] = {"count": 0, "total_ms": 0, "max_ms": 0}
    d = durations[tool_name]
    d["count"] += 1
    d["total_ms"] += duration_ms
    d["max_ms"] = max(d["max_ms"], duration_ms)


def mark_session_started(
    project_dir: str,
    permission_mode: str = "",
    transcript_path: str = "",
    source: str = "startup",
):
    """Mark that the session started - resets some counters, and records
    the run block the run report reads (start commit, permission mode, the
    transcript path every hook input carries).

    A compaction- or resume-triggered SessionStart is the SAME session
    continuing (state is keyed by session id, so on resume this file is that
    session's own): its edited-file list, prompt count and tool timings are
    kept. Wiping them there made a long unattended run's report lose
    everything before its last compaction, and resume events can fire
    mid-session (device follow-along), which reset the list unnoticed."""
    state = load_state()
    continuing = source in ("compact", "resume")
    state["prompts_without_session"] = 0
    state["edits_without_session"] = 0
    state["checkpoint_reminded"] = False  # Reset checkpoint reminder flag
    state["last_session_start"] = time.time()
    state["active_project"] = project_dir
    if not continuing:
        state["prompts_this_session"] = 0  # Reset session prompt counter
        state["files_edited_this_session"] = []
        state["tool_durations"] = {}
    _run_v = state.get("run")
    run: dict = _run_v if isinstance(_run_v, dict) else {}
    run.setdefault("started_at", state["last_session_start"])
    if permission_mode:
        run["permission_mode"] = permission_mode
    if transcript_path:
        run["transcript_path"] = transcript_path
    if not run.get("start_commit"):
        try:
            import subprocess as _sp

            r = _sp.run(
                ["git", "--no-optional-locks", "-C", project_dir, "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=3,
                stdin=_sp.DEVNULL,
            )
            run["start_commit"] = r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            run["start_commit"] = ""
    state["run"] = run
    _increment_tool_usage(state, "session_start")
    save_state(state)

    # Prune stale per-session state files (one accumulates per session).
    try:
        _prune_session_states()
    except Exception:
        pass

    # Also create the marker file in the store (honors WINDVANE_DIR like
    # every store path).
    marker = get_windvane_storage_dir() / "session_active"
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(project_dir)
    except Exception:
        pass


def mark_session_ended():
    """Mark that the session ended - preserve files for next session's context."""
    state = load_state()

    # Save current session's files as last_session_files for curated context
    files_edited = state.get("files_edited_this_session", [])
    if files_edited:
        state["last_session_files"] = files_edited.copy()

    # Stamp when the session ended (start is stamped in mark_session_started)
    # so clean termination is auditable.
    state["last_session_end"] = time.time()

    # Reset the per-turn file list. The test status stays: this runs at
    # EVERY Stop, and clearing it there made every test run after a turn
    # "PASS Test tracked (baseline established)" -- 110 of them in eight
    # days of one session, never one flip. The state file is one per
    # session, so the status needs no reset.
    state["files_edited_this_session"] = []
    state["active_project"] = ""

    save_state(state)


def get_last_session_files() -> list[str]:
    """Get files edited in the last session for curated context."""
    state = load_state()
    return state.get("last_session_files", [])


def mark_pre_edit_check_done(file_path: str):
    """Mark that pre_edit_check was called."""
    state = load_state()
    state["last_pre_edit_check"] = time.time()
    state["last_pre_edit_file"] = file_path
    state["edits_without_pre_check"] = 0
    _increment_tool_usage(state, "work_pre_edit_check")
    save_state(state)


def mark_mistake_logged():
    """Mark that a mistake was logged by hand."""
    state = load_state()
    state["last_mistake_log"] = time.time()
    _increment_tool_usage(state, "work_log_mistake")
    save_state(state)


def record_file_edit(file_path: str):
    """Record that a file was edited. Also saves to last_session_files
    continuously, and counts every edit of the session (``edits_total``,
    which the Stop hook's draft bank compares against its last bank)."""
    state = load_state()
    files = state.get("files_edited_this_session", [])
    if file_path not in files:
        files.append(file_path)
    state["files_edited_this_session"] = files[-50:]  # Keep last 50
    # Save continuously so a session end isn't required for curated context
    state["last_session_files"] = files[-50:]
    try:
        state["edits_total"] = int(state.get("edits_total") or 0) + 1
    except (TypeError, ValueError):
        state["edits_total"] = 1
    save_state(state)


# ============================================================================
# Small text helpers
# ============================================================================


def _truncate(text: str, max_len: int) -> str:
    """Truncate text with ellipsis if too long."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _project_label(project_dir: str) -> str:
    """The project a count belongs to, by folder name. Two hooks in one
    session said 35 and 40 rules (root store vs the sub-project's, which
    inherits the root's) with nothing telling them apart."""
    name = Path(str(project_dir or "")).name
    return name or "workspace"


def _pluralize(count: int, singular: str) -> str:
    """Pluralize a category name correctly."""
    if count == 1:
        return f"{count} {singular}"
    # Handle irregular plurals
    if singular == "discovery":
        return f"{count} discoveries"
    if singular == "memory":
        return f"{count} memories"
    return f"{count} {singular}s"


def _append_memory_summary(lines: list, project_memory: dict, project_dir: str):
    """Append memory summary and management hints to hook output."""
    counts = get_memory_counts(project_memory)
    total = counts.get("total", 0)

    if total == 0:
        return

    # Build compact summary line with correct plurals
    parts = [_pluralize(total, "memory")]
    for cat in ["rule", "mistake", "discovery", "decision", "context"]:
        n = counts.get(cat, 0)
        if n > 0:
            parts.append(_pluralize(n, cat))
    lines.append(f"Memory: {', '.join(parts)}")

    lines.append("")


def _cut_words(s: str, n: int) -> str:
    """Truncate at a word boundary with an ellipsis (windvane.capture owns it;
    re-exported for the handoff context)."""
    from windvane.capture import _cut_words as _cw

    return _cw(s, n)


def emit_context(event: str, text: str) -> None:
    """Print a hook's additional context in Claude Code's output schema."""
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))


def emit_deny(reason: str) -> None:
    """Print a PreToolUse deny with the reason the model is shown."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def read_payload() -> "dict | None":
    """The hook's stdin JSON, or None when there is none."""
    stdin_data = _read_stdin_with_timeout(0.5)
    if not stdin_data:
        return None
    data = json.loads(stdin_data)
    return data if isinstance(data, dict) else {}


# ============================================================================
# Session scoping
# ============================================================================


def check_session_active(project_dir: str) -> bool:
    """Check if a windvane session is active."""
    state = load_state()

    # The state is keyed by the Claude Code session id, so one session is one
    # session whichever sub-project its last edit resolved to. A
    # project-equality gate here re-ran the full start banner (the restored
    # checkpoint, the rules, the mistakes) every time the resolved project
    # flipped between the workspace root and a sub-project.
    last_start = state.get("last_session_start")
    if last_start:
        # Started once is started: the state is this session's own. A
        # four-hour window re-ran the full banner mid-session after any quiet
        # stretch.
        return True

    # Fallback to the marker file in the store.
    marker = get_windvane_storage_dir() / "session_active"
    if marker.exists():
        try:
            active_project = marker.read_text().strip()
            return (
                active_project == project_dir
                or Path(active_project).name == Path(project_dir).name
            )
        except Exception:
            pass
    return False


def get_loop_status() -> dict:
    """Loop-detection data (edit counts + recent test results) for this session.

    Lives inside the per-session hook state: a shared file let two concurrent
    sessions cross-contaminate each other's edit counts and test results."""
    return load_state().get("loop", {})


def _get_session_context_for_handoff(project_dir: str) -> dict:
    """Pull recent decisions and mistakes from memory for richer handoffs."""
    context = {
        "decisions": [],
        "mistakes": [],
        "prompts": 0,
        "tests": 0,
        "slow_tools": [],
    }
    try:
        state = load_state()
        context["prompts"] = state.get("prompts_this_session", 0)
        context["tests"] = state.get("test_runs_this_session", 0)

        # Surface slow tools (>5s avg or >30s max)
        for tool, d in state.get("tool_durations", {}).items():
            avg = d["total_ms"] / d["count"] if d["count"] else 0
            if avg > 5000 or d["max_ms"] > 30000:
                context["slow_tools"].append(
                    f"{tool}: {avg/1000:.1f}s avg, {d['max_ms']/1000:.1f}s max ({d['count']}x)"
                )

        session_start = state.get("last_session_start", 0)
        if not session_start:
            return context

        norm = _normalize_path(project_dir)
        manifest = _get_manifest()
        proj_info = manifest.get("projects", {}).get(norm)
        if not proj_info:
            return context

        pdir = get_windvane_storage_dir() / "projects" / proj_info["hash"]
        mem_file = pdir / "memory.json"
        if not mem_file.exists():
            return context

        memories = json.loads(mem_file.read_text()).get("entries", [])
        for m in memories:
            created = m.get("created_at", 0)
            if created < session_start:
                continue
            cat = m.get("category", "")
            content = _cut_words(m.get("content", ""), 200)
            if cat == "decision" and len(context["decisions"]) < 5:
                context["decisions"].append(content)
            elif cat == "mistake" and len(context["mistakes"]) < 3:
                context["mistakes"].append(content)
    except Exception:
        pass
    return context


def get_checkpoint_data(project_dir: str = "") -> dict:
    """Checkpoints and handoffs are one construct, stored in the ring buffer.
    Return the latest ring entry, falling back to any older single-slot
    latest_checkpoint.json so an imported store still surfaces it."""
    data = get_handoff_data(project_dir)
    if data:
        return data

    storage = get_windvane_storage_dir()
    candidates = []
    if project_dir:
        proj_info = (
            _get_manifest().get("projects", {}).get(_normalize_path(project_dir))
        )
        if proj_info:
            candidates.append(storage / "projects" / proj_info["hash"] / "latest_checkpoint.json")
    candidates.append(storage / "checkpoints" / "latest_checkpoint.json")
    for checkpoint_file in candidates:
        try:
            if checkpoint_file.exists():
                data = json.loads(checkpoint_file.read_text())
                if (time.time() - data.get("timestamp", 0)) / 3600 < 48:
                    return data
        except Exception:
            continue
    return {}


def get_handoff_data(project_dir: str = "") -> dict:
    """Load the latest handoff -- nearest project first, then ancestor projects,
    then the global slot. Uses the durable ring-buffer store with the walk-up
    resolver so a sub-project's own handoff is never shadowed by the shared
    global slot (which any project's stop/compact hook may have last written)."""
    try:
        from windvane import checkpoints as _hs

        data = _hs.read_latest(_handoff_candidate_dirs(project_dir), max_age_hours=48)
        return data or {}
    except Exception:
        return {}


def _session_edit_files(state: dict) -> list:
    """The files this SESSION edited, for project resolution. Every Stop
    calls mark_session_ended(), which moves ``files_edited_this_session``
    into ``last_session_files`` and clears it, so at a Stop the live list
    holds only the turn that just ended and is empty after a turn with no
    edits (seen live: the /goal bracket filed a run under the workspace
    root because the turn that set the goal edited nothing). Fall back to
    the previous turn's files, then to every absolute path the loop tracker
    counted this session."""
    # The transcript Claude Code writes is the primary record: every Edit
    # and Write the session made, in order, whatever the hook state did.
    try:
        _run = state.get("run")
        tp = str((_run if isinstance(_run, dict) else {}).get("transcript_path") or "")
        if tp:
            from windvane.goal import recent_edit_files

            files = recent_edit_files(tp)
            if files:
                return files
    except Exception:
        pass
    live = list(state.get("files_edited_this_session") or [])
    if live:
        return live
    prev = list(state.get("last_session_files") or [])
    if prev:
        return prev
    _loop = state.get("loop")
    counts = (_loop if isinstance(_loop, dict) else {}).get("edit_counts") or {}
    return [f for f in counts if isinstance(f, str) and os.path.isabs(f)]


def session_project(project_dir: str, state: "dict | None" = None) -> str:
    """THE project this session is about, for every hook that files, reads
    or scopes anything by project. One loader, used everywhere the cwd used
    to stand in: the cwd under Claude Code is the workspace root (or a
    worktree) more often than the project, and each place that used it
    grew its own bug (the wrong ring teased, the root's errors, a run
    filed under the root). Order: the transcript's own Edit/Write calls,
    then the hook state's lists, then the cwd mapped to its repository
    (``canonical_project_root``). Cached in the session state against the
    transcript's size, so a hook pays the tail read only when the
    transcript grew."""
    st = state if isinstance(state, dict) else load_state()
    root = _normalize_path(project_dir) if project_dir else ""
    _run = st.get("run")
    tp = str((_run if isinstance(_run, dict) else {}).get("transcript_path") or "")
    size = 0
    if tp:
        try:
            size = os.path.getsize(tp)
        except OSError:
            size = 0
    cache = st.get("session_project_cache")
    if (
        isinstance(cache, dict)
        and cache.get("root") == root
        and cache.get("value")
        and abs(int(cache.get("size") or 0) - size) < 65536
    ):
        return str(cache["value"])
    value = ""
    try:
        files = _session_edit_files(st)
        if files:
            value = _resolve_session_project(root, files)
    except Exception:
        value = ""
    if not value:
        from windvane.paths import canonical_project_root

        value = canonical_project_root(root) if root else root
    st["session_project_cache"] = {"root": root, "size": size, "value": value}
    return value


def _resolve_session_project(project_dir: str, files: list) -> str:
    """Dominant sub-project of a session's edited files (majority vote over
    the most recent ten). Falls back to ``project_dir`` (the cwd) when there
    are no files -- the cwd is the workspace root under Claude Code, which is
    exactly how per-turn auto handoffs ended up in the wrong ring, so the
    files win whenever they exist."""
    counts: dict = {}
    root_l = _normalize_path(project_dir).lower().rstrip("/") + "/" if project_dir else ""
    for f in files[-10:]:
        try:
            # A file outside the root (the memory dir under ~/.claude, a
            # temp file) says nothing about which project this is; it
            # used to vote for the root. Skip it.
            if root_l and not _normalize_path(str(f)).lower().startswith(root_l):
                continue
            # Pass the root explicitly -- the resolver's cwd default matches
            # production hooks, but being explicit keeps this correct from
            # any process (tests, a plugin tool, the daemon).
            p = _normalize_path(resolve_project_for_file(f, project_dir))
            counts[p] = counts.get(p, 0) + 1
        except Exception:
            continue
    if not counts:
        return project_dir
    return max(counts.items(), key=lambda kv: kv[1])[0]


# ============================================================================
# Checkpoint restore pieces (the banner, the brief, the plugin tools)
# ============================================================================


def _subtree_manual_handoff(project_dir: str) -> dict:
    """Newest MANUAL handoff across the workspace SUBTREE (descendant project
    rings). The walk-up resolver only looks at ancestors, so a fresh session
    opened at the workspace root could only ever see the root ring -- while
    the real handoff sat in a sub-project's ring. Under concurrency this is
    a labeled breadcrumb, not an auto-restore: the label carries the project
    and task_id so a session for a different sub-project can ignore it.
    Manuals get the same 14-day freshness bound as read_latest. Returns {}
    when no descendant manual exists."""
    try:
        from windvane import checkpoints as _hs

        storage = get_windvane_storage_dir()
        norm = _normalize_path(project_dir)
        dirs = []
        for path, info in _get_manifest().get("projects", {}).items():
            if path == norm or path.startswith(norm + "/"):
                dirs.append(storage / "projects" / info["hash"])
        if not dirs:
            return {}
        hist = _hs.read_history(dirs)
        now = time.time()
        for h in hist:  # newest first
            if h.get("kind") != "manual":
                continue
            ts = h.get("created", h.get("created_at", 0)) or 0
            if (now - ts) / 3600 <= 14 * 24:
                return h
        return {}
    except Exception:
        return {}


def _own_session_checkpoint(dirs: list, session_id: str, transcript_path: str) -> tuple:
    """This session's newest deliberate checkpoint that is still on the
    transcript's live branch, and the newer ones of its own it skipped
    because the user rewound past them. (None, []) when the session has no
    manual checkpoint in the given rings. A rewind fires no hook and leaves
    no record; the ring kept returning the abandoned branch's checkpoint
    under this session's id (windvane.transcript)."""
    try:
        from windvane import checkpoints as _hs
        from windvane.transcript import TAIL_BYTES, branch_checkpoints

        sid = str(session_id or "")
        if not sid:
            return None, []
        mine = [h for h in _hs.read_history(dirs)  # newest first
                if h.get("kind") == "manual" and str(h.get("session_id") or "") == sid]
        if not mine:
            return None, []
        # One read of the transcript's tail for every candidate (a hook has
        # 1-2 s; the tail is 8 MB at most).
        on_branch: set = set()
        everywhere: set = set()
        if transcript_path:
            try:
                on_branch, everywhere = branch_checkpoints(transcript_path, TAIL_BYTES)
            except Exception:
                on_branch, everywhere = set(), set()
        skipped = []
        for h in mine:
            tid = str(h.get("task_id") or "")
            if tid and tid in everywhere and tid not in on_branch:
                skipped.append(h)  # saved, then rewound past
                continue
            return h, skipped
        return None, skipped
    except Exception:
        return None, []


def _format_restored_full(entry: dict, skipped: "list | None" = None) -> list[str]:
    """The whole checkpoint for the banner after a compaction or on resume:
    every completed and pending step, every file, warning and context note,
    the handoff summary, the goal and the repo's movement since. The teaser
    (`_format_restored_context`) cuts the task at 100 chars, the step at 60
    and shows three next steps; after a compaction the model would have to
    call the restore to read what it had banked. A checkpoint of this session
    that sits on a rewound branch is named so the model knows the ring holds a
    future it no longer remembers."""
    if not entry:
        return []
    out = list(_format_restored_context(entry)[:1])  # the self-identifying header line
    task = entry.get("task_description") or ""
    if task and task != (entry.get("summary") or ""):
        out.append(f"  Task: {task}")
    step = entry.get("current_step")
    if step and step != "Context was compacted":
        out.append(f"  Current step: {step}")
    done = [s for s in (entry.get("completed_steps") or []) if s]
    if done:
        out.append(f"  Completed ({len(done)}):")
        out.extend(f"    - {s}" for s in done)
    _trivial = {"review what was in progress", "continue work from before compaction"}
    pending = [s for s in (entry.get("pending_steps") or entry.get("next_steps") or []) if s and s.strip().lower() not in _trivial]
    if pending:
        out.append(f"  Pending ({len(pending)}):")
        out.extend(f"    - {s}" for s in pending)
    files = entry.get("files_in_progress") or entry.get("files_involved") or []
    if files:
        out.append("  Files: " + ", ".join(str(f) for f in files))
    for key, label in (("warnings", "Warnings"), ("handoff_warnings", "Warnings"),
                       ("context_needed", "Context needed"), ("handoff_context_needed", "Context needed")):
        items = [s for s in (entry.get(key) or []) if s]
        if items and not any(line.startswith(f"  {label}:") for line in out):
            out.append(f"  {label}:")
            out.extend(f"    ! {s}" for s in items)
    handoff = entry.get("handoff_summary") or entry.get("summary") or ""
    if handoff and handoff != task and handoff != step:
        out.append(f"  Handoff note: {handoff}")
    if entry.get("goal"):
        out.append(f"  Goal: {entry['goal']}")
    for s in (skipped or []):
        out.append(
            f"  Skipped: {s.get('task_id', '?')} \"{_truncate(str(s.get('task_description') or s.get('summary') or ''), 80)}\""
            " sits on a rewound branch of this conversation (saved, then rewound past); not restored"
        )
    return out


def _format_restored_context(entry: dict) -> list[str]:
    """Render a restored checkpoint for the session-start banner. Checkpoints and
    handoffs are one ring construct, so this shows whichever fields the entry
    carries -- both an auto-compaction save and a manual checkpoint read well.

    The sub-project is derived from the entry's own project_path (the files
    are the fallback for old autos) -- a restored context from another
    concurrent session is then obvious.
    """
    if not entry:
        return []
    created = entry.get("created", entry.get("timestamp", 0))
    age = (time.time() - created) / 3600 if created else 0.0
    files = entry.get("files_in_progress") or entry.get("files_involved") or []
    # The entry's own project_path is the truth about whose checkpoint this
    # is. Inferring it from the first edited file resolved AGAINST THE CWD,
    # so a foreign checkpoint served from the global ring into a brand-new
    # project (which has no ring yet) was labelled with the new project's
    # name. Files are the fallback for old autos only. Older manual saves
    # carried the path only under metadata.
    _pp = entry.get("project_path") or (entry.get("metadata") or {}).get(
        "project_path", ""
    )
    sub = Path(_pp).name if _pp else ""
    if not sub and files:
        try:
            sub = Path(resolve_project_for_file(files[0])).name
        except Exception:
            sub = ""
    label = f"{entry.get('kind', 'auto')}, {age:.1f}h ago"
    if sub:
        label += f", {sub}"
    # task_id makes the teaser self-identifying: the model can restore
    # (task_id=...) THIS entry instead of trusting that index 0 happens to
    # match what was teased.
    if entry.get("task_id"):
        label += f", {entry['task_id']}"
    headline = entry.get("summary") or entry.get("task_description") or "?"
    out = [f"CHECKPOINT [{label}]: {_truncate(headline, 100)}"]

    step = entry.get("current_step")
    if step and step != "Context was compacted":
        out.append(f"  Current step: {_truncate(step, 60)}")
    # Either vocabulary: a handoff-shaped entry carries only next_steps.
    _pending = entry.get("pending_steps") or entry.get("next_steps") or []
    if _pending:
        out.append(f"  Pending: {len(_pending)} steps")
    _trivial = {"review what was in progress", "continue work from before compaction"}
    real_next = [
        s
        for s in (entry.get("next_steps") or [])
        if s and s.strip().lower() not in _trivial
    ]
    if real_next:
        out.append("  Next: " + "; ".join(_truncate(s, 60) for s in real_next[:3]))
    if files:
        out.append(f"  Files: {', '.join(Path(f).name for f in files[:5])}")
    if entry.get("warnings"):
        out.append(
            "  Warnings: " + "; ".join(_truncate(w, 60) for w in entry["warnings"][:3])
        )
    if entry.get("goal"):
        out.append(f"  Goal: {_truncate(str(entry['goal']), 100)}")
    # Staleness (repo_state): the handoff carries the last session's framing;
    # say how far the repo moved since it was written.
    try:
        from windvane import repo_state as _rs

        try:
            _saved = float(entry.get("created") or entry.get("timestamp") or 0.0)
        except (TypeError, ValueError):
            _saved = 0.0
        _since = _rs.since_text(_rs.since(str(entry.get("commit") or ""), str(_pp), files, saved_at=_saved))
        if _since:
            out.append("  " + _since)
    except Exception:
        pass
    return out


# ============================================================================
# Memory read at the point of use
# ============================================================================


def _file_mistakes(project_memory: dict, file_path: str) -> list:
    """The stored mistakes that name this file (their text). For generic
    names (__init__.py, etc.) a bare filename match is meaningless across
    projects, so the full path must appear in the mistake; otherwise a
    service-a __init__.py mistake fires on every service-b __init__.py edit.
    Read-only; shared with windvane.brief."""
    out: list = []
    all_mistakes = get_past_mistakes(project_memory)
    file_name = Path(file_path).name
    if file_name.lower() in _GENERIC_BASENAMES:
        needle = file_path.replace("\\", "/").lower()
        for mistake in all_mistakes:
            if needle and needle in mistake["content"].replace("\\", "/").lower():
                out.append(mistake["content"])
    else:
        file_pattern = re.compile(
            r"(?:^|[\s/\\:])" + re.escape(file_name.lower()) + r"(?:[\s:,.]|$)"
        )
        for mistake in all_mistakes:
            if file_pattern.search(mistake["content"].lower()):
                out.append(mistake["content"])
    return out


def _file_mistake_lines(mistakes: list, label: str = "") -> list:
    """The pre-edit hook's past-mistakes lines (the first three). `label`
    names the file where several files share one block (the brief); the
    pre-edit hook passes none and reads "with this file"."""
    if not mistakes:
        return []
    out = [f"AUTO-CHECK: Past mistakes with {label or 'this file'}:"]
    out.extend(f"  - {_truncate(m, 80)}" for m in mistakes[:3])
    return out


def get_contextual_memories(
    project_dir: str,
    file_path: str,
    entries: "list[dict] | None" = None,
    exclude: "list[str] | None" = None,
) -> list[str]:
    """
    Get scored memories relevant to a file context.

    Uses relevance scoring (file match, tags, recency, importance) instead
    of naive filename substring matching. Returns top 3 most relevant.

    ``entries``: pre-loaded memory entries (with ancestors) -- pass them when
    the caller already parsed memory.json so the hook doesn't parse it twice.
    ``exclude``: content snippets already shown elsewhere in the banner
    (e.g. the past-mistakes section) -- duplicates are dropped.
    """
    try:
        from windvane.hot_reader import (
            HotMemoryReader,
            score_loaded_entries,
        )

        context = {
            "file_path": file_path,
            "tool_name": "Edit",
            "tags": [],
        }
        if entries is None:
            entries = HotMemoryReader().load_entries(project_dir)
        scored = score_loaded_entries(entries, context, limit=3)
        if exclude:
            keys = [str(x)[:60] for x in exclude if x]
            scored = [
                m
                for m in scored
                if not any(k in m.get("content", "") for k in keys)
            ]
        # Append each memory's age so staleness is visible at the point of use: a
        # code-pointer memory goes wrong after a refactor, and "(40d old)" is the
        # cue to verify before trusting it. Silent when no timestamp is stored.
        now = time.time()
        out = []
        for m in scored:
            # One line, cut at a sentence or a word, never mid-token: an
            # 80-character cut dropped the half of a note worth knowing.
            content = " ".join(str(m["content"]).split())
            if len(content) > 200:
                cut = content[:200]
                dot = max(cut.rfind(". "), cut.rfind("; "))
                content = (cut[: dot + 1] if dot > 80 else cut.rsplit(" ", 1)[0]) + "..."
            ts = m.get("created_at") or m.get("created") or m.get("timestamp") or 0
            try:
                days = int((now - float(ts)) / 86400) if ts else 0
            except (TypeError, ValueError):
                days = 0
            out.append(f"{content} ({days}d old)" if days >= 1 else content)
        return out
    except Exception:
        return []


_CODE_SUFFIXES = frozenset(
    {
        ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".rs", ".go",
        ".java", ".kt", ".kts", ".scala", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp",
        ".cs", ".rb", ".php", ".swift", ".m", ".mm", ".lua", ".luau", ".sh", ".bash",
        ".zsh", ".ps1", ".psm1", ".pl", ".r", ".jl", ".ex", ".exs", ".erl", ".hs",
        ".ml", ".clj", ".dart", ".vue", ".svelte", ".sql", ".proto", ".zig", ".nim",
    }
)


def _is_code_file(file_path: str) -> bool:
    """Tests speak to code. Markdown, config, data and notebooks are edited
    many times in a row as a matter of course."""
    return Path(file_path or "").suffix.lower() in _CODE_SUFFIXES


def _append_memory_entry(project_dir: str, entry: dict, skip_if=None) -> bool:
    """
    Append a memory entry to the project's per-project memory.json (atomic),
    embedding it to the pending file for the miner to fold in.

    Registers the project in the manifest when missing: an error or decision
    in a brand-new project must not be dropped on the floor. Registration is
    safe across processes because the hash is a deterministic md5 of the
    normalized path -- a lost manifest write just gets re-added later pointing
    at the same directory.

    ``skip_if(existing_entries)`` lets callers add extra dedup (e.g. the
    decision capturer's word-overlap check). Exact-content duplicates are
    always skipped. Returns True if the entry was written.
    """
    norm_dir = _normalize_path(project_dir)
    storage = get_windvane_storage_dir()
    manifest = _get_manifest()
    projects = manifest.setdefault("projects", {})
    if norm_dir not in projects:
        import hashlib as _hl

        projects[norm_dir] = {
            "hash": _hl.md5(norm_dir.encode()).hexdigest()[:8],
            "name": Path(norm_dir).name,
        }
        manifest.setdefault("version", 3)
        manifest_file = storage / "manifest.json"
        storage.mkdir(parents=True, exist_ok=True)
        tmp = manifest_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, indent=2))
        tmp.replace(manifest_file)

    pdir = storage / "projects" / projects[norm_dir]["hash"]
    mem_file = pdir / "memory.json"
    proj_data = {}
    if mem_file.exists():
        try:
            proj_data = json.loads(mem_file.read_text())
        except Exception:
            proj_data = {}

    existing = proj_data.get("entries", [])
    if any(e.get("content") == entry["content"] for e in existing):
        return False
    if skip_if and skip_if(existing):
        return False

    proj_data.setdefault("entries", []).append(entry)
    proj_data["last_updated"] = time.time()
    pdir.mkdir(parents=True, exist_ok=True)
    tmp = mem_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(proj_data, indent=2))
    tmp.replace(mem_file)

    # Embed to pending file (fast, no full store load); merged on next load.
    # Stamped with the embedding signature so a model change between write
    # and merge can't mix vector spaces (legacy flat files = legacy model).
    try:
        from windvane.daemon import embed_via_server
        from windvane.semantic.config import LEGACY_SIGNATURE, embed_signature

        emb = embed_via_server(entry["content"])
        if emb:
            sig = embed_signature()
            pending_file = pdir / "embeddings_pending.json"
            vectors = {}
            if pending_file.exists():
                raw = json.loads(pending_file.read_text())
                if isinstance(raw, dict) and "vectors" in raw:
                    if raw.get("model", LEGACY_SIGNATURE) == sig:
                        vectors = raw.get("vectors", {})
                elif isinstance(raw, dict) and LEGACY_SIGNATURE == sig:
                    vectors = raw
            vectors[entry["id"]] = emb
            etmp = pending_file.with_suffix(".json.tmp")
            etmp.write_text(json.dumps({"model": sig, "vectors": vectors}))
            etmp.replace(pending_file)
    except Exception:
        pass
    return True


# ============================================================================
# Test runs: which commands ran tests, and the last targeted one that passed
# ============================================================================


_TEST_RUNNER_PREFIXES = (
    "pytest",
    "jest",
    "mocha",
    "npm test",
    "yarn test",
    "make test",
    "make check",
    "cargo test",
    "go test",
    "python -m pytest",
    "python -m unittest",
)


_READ_ONLY_EXECUTABLES = frozenset(
    {
        "grep", "rg", "egrep", "fgrep", "cat", "head", "tail", "sed", "awk", "less",
        "more", "find", "ls", "dir", "echo", "printf", "wc", "sort", "uniq", "cut",
        "tr", "diff", "git", "gh", "type", "which", "where", "stat", "file", "tree",
        "jq", "curl", "wget", "select-string", "get-content", "get-childitem",
    }
)


_SHELL_NOISE = frozenset(
    {
        "export", "cd", "pwd", "set", "source", "unset", "true", "false", "sleep", "mkdir", "touch", "cp", "mv",
        "ln", "chmod", "date", "basename", "dirname", "realpath", "tee", "test", "[", "[[", "for", "do", "done",
        "if", "then", "else", "elif", "fi", "while", "until", "case", "esac", "in", "local", "declare", "read",
        "shift", "exit", "return", "break", "continue", "{", "}", "!", "trap", "wait", "kill", "rm", "rmdir",
    }
)
_WRAPPERS = frozenset({"timeout", "env", "nice", "sudo", "time", "nohup", "ssh", "uv", "poetry", "pipenv", "conda", "npx", "bunx"})
_NOT_RUNNER_SUBCOMMANDS = frozenset({"lock", "sync", "add", "remove", "pip", "venv", "init", "build", "publish", "install", "update", "tree", "export"})


def _segment_kind(seg: str) -> str:
    """What ONE shell segment is: ``run`` (could have run a test), ``read``
    (a read-only tool whose output may quote a test line: cat, tail, grep,
    git), ``noise`` (cd, export, echo, a loop keyword). Leading assignments
    and wrappers (`timeout 590 bash x.sh`, `uv run pytest`, `ssh box ...`)
    are peeled; a package manager's housekeeping (`uv lock`) is noise."""
    seg = (seg or "").strip()
    while True:
        m = re.match(r"^(?:[a-z_][a-z0-9_]*=\S*\s+)+", seg)
        if not m:
            break
        seg = seg[m.end() :]
    parts = seg.split()
    while parts:
        exe = parts[0].replace("\\", "/").rsplit("/", 1)[-1].lower().strip("'\"")
        if exe.endswith(".exe"):
            exe = exe[:-4]
        if exe in _WRAPPERS:
            rest = parts[1:]
            # `timeout 590 cmd`, `nice -n 5 cmd`, `ssh -p 22 host cmd`, `uv run cmd`
            while rest and re.fullmatch(r"-\S*|\d+[smh]?", rest[0]):
                rest = rest[1:]
            if exe == "ssh" and rest:
                rest = rest[1:]  # the host
            if exe in ("uv", "poetry", "pipenv", "conda") and rest:
                sub = rest[0].lower()
                if sub in _NOT_RUNNER_SUBCOMMANDS:
                    return "noise"
                if sub == "run":
                    rest = rest[1:]
            if not rest:
                return "noise"
            parts = rest
            continue
        if exe in _READ_ONLY_EXECUTABLES:
            return "read"
        if exe in _SHELL_NOISE or not exe:
            return "noise"
        return "run"
    return "noise"


def _output_has_test_markers(output: str) -> bool:
    """Does this OUTPUT read as a test verdict? pytest / unittest / jest
    shapes only. A bare "N errors" is not one: `uv lock`, a linter and a
    smoke script all print it without running a test."""
    low = (output or "").lower()
    return any(
        (
            re.search(r"\b\d+ passed\b", low),
            re.search(r"\b\d+ failed\b", low),
            re.search(r"\b\d+ (?:passed|failed|skipped|xfailed|deselected),? \d+ errors?\b", low),
            re.search(r"collected \d+ items?", low),
            re.search(r"\nok\s*$", low),
            "assertionerror" in low,
            "=== failures ===" in low,
            "test session starts" in low,
            re.search(r"\[pass\]|\[fail\]", low),
            re.search(r"^\s*ran \d+ tests?", low, re.M),
        )
    )


def _command_can_run_tests(command: str) -> bool:
    """Can this command's OUTPUT be a test verdict? The output markers
    ("3 passed", "collected 2 items") are trusted when the command names a
    test runner (`_is_test_invocation`), or when no segment of the chain
    merely reads. A chain that reads a log and does something else --
    `cat run.log; bash count.sh`, `tail -3 out.txt; date` -- is not
    trusted: the line the marker matched came from the read (29 of 110
    "Test tracked" lines in eight days of one session were such reads)."""
    if _is_test_invocation(command):
        return True
    low = (command or "").lower()
    kinds = {_segment_kind(seg) for seg in re.split(r"&&|\|\||;|\n|\|(?!\|)", low)}
    return "run" in kinds and "read" not in kinds


def _is_test_invocation(command: str) -> bool:
    """Is this bash command a test run? Used to suppress mistake auto-log:
    a failing test's error is already captured as the test result, and
    RED-phase TDD failures (deliberate ModuleNotFoundError/assertion fails
    before implementing) are not mistakes -- logging them drowns the
    banner's signal in noise."""
    # Every segment of a chain is judged (`cd proj && python -m pytest` is
    # a test run; its first segment alone is a cd), after stripping leading
    # environment assignments.
    for seg in re.split(r"&&|\|\||;|\$\(", (command or "").lower().strip()):
        seg = seg.strip()
        seg = re.sub(r"^(?:[a-z_][a-z0-9_]*=\S*\s+)+", "", seg)
        if any(seg.startswith(p) for p in _TEST_RUNNER_PREFIXES):
            return True
        # venv/...python -m pytest, python tests/bench_x.py and similar shapes
        if re.search(r"python[\w.\\/]*(\.exe)?\s+(-m\s+)?(pytest|unittest)\b", seg):
            return True
        if re.search(r"python[\w.\\/]*(\.exe)?\s+\S*tests?[\\/]", seg):
            return True
    return False


_TARGET_ARG = re.compile(r"\.(?:py|js|jsx|ts|tsx|mjs|cjs|rs|go|rb)\b|[\\/]|::")


def _is_targeted_test(command: str) -> bool:
    """Does this test command run chosen tests rather than the whole suite?
    A runner given a test file, a directory below the root, a node id
    (``::``) or a ``-k`` selection is targeted; ``pytest``, ``npm test`` or
    ``go test ./...`` alone is the suite."""
    if not _is_test_invocation(command):
        return False
    low = " ".join((command or "").lower().split())
    if re.search(r"(?:^|\s)-k\s", low):
        return True
    for seg in re.split(r"&&|\|\||;|\$\(", low):
        toks = seg.strip().split()
        for i, tok in enumerate(toks):
            if i == 0 or tok.startswith("-") or tok in ("./...", "..."):
                continue
            # The interpreter or runner path itself is not a target.
            if re.search(r"(?:^|[\\/])(?:python[\w.]*|pytest|jest|mocha)(?:\.exe)?$", tok):
                continue
            if "test" in tok and _TARGET_ARG.search(tok):
                return True
    return False


def _record_test_command(project_dir: str, command: str, passed: bool, files: "list | None" = None) -> None:
    """Track test invocations per project so session start can surface the
    last targeted command that passed and the files it covered. Only called
    when the bash handlers already classified the run as a test. Throwaway
    shapes (inline python -c, heredocs, scratch scripts, multiline
    contraptions) are skipped -- recall is for commands worth running again.

    ``files``: the code files edited in this session before the run (names);
    recorded with a passing targeted run as ``last_targeted``."""
    try:
        cmd = " ".join((command or "").split())
        if not cmd or len(cmd) > 160:
            return
        low = cmd.lower()
        if (
            "<<" in cmd
            or "\n" in (command or "")
            or ".scratch" in low
            or re.search(r"python[^|;&]*\s-c\s", low)
            or re.search(r"[\\/](?:tmp|temp)[\\/]", low)
        ):
            return
        pdir = get_project_memory_dir(project_dir)
        path = pdir / "test_commands.json"
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        cmds = data.setdefault("commands", {})
        rec = cmds.setdefault(
            cmd,
            {"pass_count": 0, "fail_count": 0, "last_pass": 0.0, "last_fail": 0.0},
        )
        now = time.time()
        if passed:
            rec["pass_count"] = int(rec.get("pass_count", 0)) + 1
            rec["last_pass"] = now
            if _is_targeted_test(cmd):
                rec["files"] = [str(f) for f in (files or [])][:8]
                data["last_targeted"] = {"command": cmd, "files": rec["files"], "at": now}
        else:
            rec["fail_count"] = int(rec.get("fail_count", 0)) + 1
            rec["last_fail"] = now
        # Bound the store: keep the 30 most recently exercised.
        if len(cmds) > 30:
            keep = sorted(
                cmds.items(),
                key=lambda kv: max(
                    kv[1].get("last_pass", 0), kv[1].get("last_fail", 0)
                ),
                reverse=True,
            )[:30]
            data["commands"] = dict(keep)
        pdir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _age_words(ts: float) -> str:
    """'12m ago', '3h ago', '2d ago'."""
    try:
        secs = max(0.0, time.time() - float(ts))
    except (TypeError, ValueError):
        return ""
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _last_targeted_test(project_dir: str) -> "dict | None":
    """The project's last targeted test command that passed and still passes
    (its last pass not older than its last fail): {command, files, at}.
    Walks ancestors so a sub-project session inherits a workspace-tracked
    command, but only one that names the sub-project (the workspace root
    pools every sibling's runs)."""
    try:
        own = _normalize_path(project_dir)
        own_name = Path(own).name.lower()
        check = own
        for _ in range(8):
            path = get_project_memory_dir(check) / "test_commands.json"
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                cmds = data.get("commands", {}) or {}
                inherited = check != own
                best = None
                for c, r in cmds.items():
                    if not (
                        r.get("pass_count", 0) >= 1
                        and r.get("last_pass", 0) >= r.get("last_fail", 0)
                        and _is_targeted_test(c)
                        and (not inherited or (own_name and own_name in c.lower()))
                    ):
                        continue
                    if best is None or r.get("last_pass", 0) > best[1].get("last_pass", 0):
                        best = (c, r)
                if best:
                    return {"command": best[0], "files": list(best[1].get("files") or []), "at": best[1].get("last_pass", 0)}
            parent = _normalize_path(str(Path(check).parent))
            if parent == check:
                break
            check = parent
    except Exception:
        pass
    return None


def _last_targeted_line(project_dir: str) -> str:
    """One banner line: the last targeted test command that passed, its
    files, and when. "" when there is none."""
    rec = _last_targeted_test(project_dir)
    if not rec:
        return ""
    files = ", ".join(Path(str(f)).name for f in rec.get("files") or [])
    when = _age_words(rec.get("at") or 0)
    tail = "; ".join(p for p in (files and f"files: {files}", when) if p)
    return f"Last targeted test that passed: {rec['command']}" + (f" ({tail})" if tail else "")


# ============================================================================
# Mined patterns (recurring errors and struggles)
# ============================================================================


def _find_patterns_report(project_dir: str) -> dict:
    """Locate the mined patterns.json for a project, walking ancestors.

    Mining pools at the workspace root, so a failure inside a sub-project
    usually finds its report on a parent. Returns {} when absent.
    """
    try:
        storage = get_windvane_storage_dir()
        projects = _get_manifest().get("projects", {})
        check = _normalize_path(project_dir)
        for _ in range(8):
            info = projects.get(check)
            if info:
                p = storage / "projects" / info["hash"] / "patterns.json"
                if p.exists():
                    return json.loads(p.read_text(encoding="utf-8"))
            parent = _normalize_path(str(Path(check).parent))
            if parent == check:
                break
            check = parent
    except Exception:
        pass
    return {}


def _recurring_lines(work_project: str, predicted_files: list) -> list:
    """Recurring struggles, recurring errors and the last targeted test that
    passed, for ``work_project``: the mined patterns report walked up the
    ancestors (mining pools at the workspace root) and filtered to this
    project. Printed at session start on a resume, and at the first edit of a
    fresh session, when the project is first known (the root's errors at a
    root-cwd start were noise)."""
    lines: list = []
    try:
        pdata = _find_patterns_report(work_project)
        if pdata:
            predicted = set()
            try:
                for f in predicted_files or []:
                    predicted.add(_normalize_path(resolve_project_for_file(f)))
            except Exception:
                predicted = set()
            norm_root = _normalize_path(work_project)
            if norm_root not in predicted:
                predicted.add(norm_root)
            _own_errors = Path(work_project).name.lower() == "windvane"

            def _in_scope(projs, example=""):
                # windvane's own failures (paths inside its store) are
                # nobody's recurring errors but windvane's.
                if not _own_errors and "/.windvane" in str(example or "").replace("\\", "/"):
                    return False
                # No attribution -> show (an older patterns.json has no
                # projects field). Otherwise the error must name this
                # project or one the last session touched.
                if not projs:
                    return True
                return bool(set(projs) & predicted)

            def _struggle_scope(s):
                try:
                    return _in_scope([_normalize_path(resolve_project_for_file(s.get("file_path", "")))])
                except Exception:
                    return True

            struggles = [s for s in pdata.get("struggles", []) if _struggle_scope(s)][:3]
            recurring = [
                e
                for e in pdata.get("recurring_errors", [])
                if _in_scope(e.get("projects") or [], e.get("example") or "")
            ][:3]
            if struggles:
                lines.append("Recurring struggles:")
                for s in struggles:
                    try:
                        proj = Path(resolve_project_for_file(s["file_path"])).name
                    except Exception:
                        proj = ""
                    loc = f"{proj}/{Path(s['file_path']).name}" if proj else s["file_path"]
                    lines.append(f"  - {loc} ({s['sessions_affected']} sessions, {s['errors_nearby']} errors)")
            if recurring:
                lines.append("Recurring errors:")
                for e in recurring:
                    # A concrete instance over the templated signature
                    # (which strips the identifiers that make it actionable).
                    label = e.get("example") or e.get("message_pattern") or e["error_type"]
                    lines.append(f"  - {label} ({e['session_count']} sessions)")
                    fix = e.get("fix")
                    if fix:
                        lines.append(f"    fix: {fix}")
    except Exception:
        pass
    # The last targeted test that passed here: verification starts from
    # what worked, not a guess, and never from the whole suite.
    try:
        line = _last_targeted_line(work_project)
        if line:
            lines.append(line)
    except Exception:
        pass
    return lines


# ============================================================================
# Nudge delivery, the /goal bracket, compliance (shared by several events)
# ============================================================================


def _stall_bearings(project_dir: str) -> list[str]:
    """Strike 2's re-injection: the latest checkpoint and the top rules, the
    same material a compaction restores. Empty when there is nothing."""
    lines: list[str] = []
    try:
        handoff = get_handoff_data(project_dir)
        if handoff:
            lines.extend(_format_restored_context(handoff))
    except Exception:
        pass
    try:
        project_memory = load_project_memory(project_dir)
        rules = filter_rules_in_claude_md(get_project_rules(project_memory), project_dir)
        if rules:
            lines.append(f"Rules ({len(rules)}, {_project_label(project_dir)}):")
            for r in rules[:5]:
                lines.append(f"  [{r['id']}] {_truncate(r['content'], 100)}")
    except Exception:
        pass
    return lines


def _with_pressure(result: str, project_dir: str) -> str:
    """Append a context-pressure nudge (heads-up / checkpoint-now / cadence)
    when one is due (windvane.pressure). State-latched, so each band fires
    once per compaction cycle no matter how many hook sites call this; every
    site that injects for the main session should, because in an unattended
    /goal loop there are no user prompts and PostToolUse is the only delivery
    point that fires every turn."""
    try:
        from windvane import pressure as _cp
        from windvane import stall as _stall

        state = load_state()
        # Every nudge that reads a ring or a rule set reads the session's
        # project, not the cwd (the strike-2 bearings once teased another
        # session's checkpoint).
        project_dir = session_project(project_dir, state)
        text, changed = _cp.nudge(state, _session_id, project_dir)
        # A staged stall strike (windvane.stall) rides the same delivery:
        # the Stop hook that judged the turn cannot add context itself. The
        # strike text is delivered in autonomy mode only (stall.nudge).
        bearings = None
        if isinstance(_stall.stall_state(state).get("pending"), dict):
            bearings = _stall_bearings(project_dir)
        s_text, s_changed = _stall.nudge(state, bearings, project_dir)
        if s_text:
            text = f"{text}\n{s_text}" if text else s_text
        # The halt's instructions, once, at the first injection point after
        # it armed (the Stop hook that armed it cannot inject).
        _sst = _stall.stall_state(state)
        if _sst.get("pending_halt") and _stall.halted(state):
            _sst["pending_halt"] = False
            s_changed = True
            h_text = _stall.halt_text(state, _session_id)
            text = f"{text}\n{h_text}" if text else h_text
        if changed or s_changed:
            save_state(state)
    except Exception:
        return result
    if not text:
        return result
    return f"{result}\n{text}" if result else text


def _goal_bracket(state: dict, data: dict, project_dir: str, turn: bool) -> None:
    """The /goal bracket (windvane.goal): bring the run record in step with
    the transcript's goal at Stop (turn=True), UserPromptSubmit and
    SessionEnd. A start writes the manifest; an end sends one alert and
    writes the run report. Saves the state when anything changed."""
    try:
        from windvane import goal as _ar

        tp = str((data or {}).get("transcript_path") or "")
        # The cwd under Claude Code is often the workspace root; the session's
        # edited files name the sub-project the run is about.
        try:
            project_dir = session_project(project_dir, state)
        except Exception:
            pass
        ev = _ar.observe(state, tp, project_dir, turn=turn)
        if turn or ev:
            save_state(state)
        if not ev:
            return
        a = ev.get("auto") or {}
        if ev.get("event") == "started":
            try:
                _ar.write_start_manifest(project_dir, _session_id, a)
            except Exception:
                pass
            return
        try:
            from windvane import alerts as _alerts

            _alerts.send(_ar.end_alert_text(a, _session_id), project_dir, kind=str(a.get("status") or "ended"), state=state)
            save_state(state)
        except Exception:
            pass
        try:
            from windvane import report as _rr

            _rr.write_report(_session_id, project_dir, state)
        except Exception:
            pass
    except Exception:
        pass


def _compliance_check(
    project_dir: str, data: dict, tool_name: str, tool_input, state: "dict | None" = None
) -> "tuple[list[dict], dict | None]":
    """Match one call against the rules' detectors and record it. Returns
    (new matches, the state that was loaded/mutated) -- the caller saves."""
    from windvane import compliance as _cpl

    if not _cpl.enabled(project_dir):
        return [], state
    rules = _cpl.rules_with_detectors(load_project_memory(project_dir))
    if not any(r.get("detector") for r in rules):
        return [], state
    hits = _cpl.match_call(rules, tool_name, tool_input)
    if state is None:
        state = load_state()
    turn = int((state.get("pressure") or {}).get("stops_total", 0)) + 1
    new = _cpl.record(
        state,
        rules,
        hits,
        tool_name,
        tool_input,
        tool_use_id=str(data.get("tool_use_id") or ""),
        turn=turn,
        permission_mode=str(data.get("permission_mode") or ""),
        agent_id=str(data.get("agent_id") or ""),
    )
    return new, state


# ============================================================================
# The session-start banner pieces (shared with windvane.brief)
# ============================================================================


def _banner_work_project(project_dir: str, source: str, transcript: str) -> tuple:
    """The project the banner is ABOUT, and the session's own edited files
    that named it. A session run from a workspace root that works on one
    sub-project got the root's recurring errors, test commands and "last
    session" (another project's files) in its banner. On resume/compact the
    session's own edits name the project; on a fresh start only the cwd is
    known. Shared with windvane.brief, which renders the same banner pieces
    for the mod."""
    resume_files: list = []
    if source in ("resume", "compact"):
        try:
            # The transcript is the record: every edit the resuming
            # session made. The hook state is the fallback (every Stop
            # moves its live list into last_session_files).
            from windvane.goal import recent_edit_files as _recent

            resume_files = _recent(transcript) if transcript else []
            if not resume_files:
                resume_files = _session_edit_files(load_state())
        except Exception:
            resume_files = []
    work_project = project_dir
    if resume_files:
        try:
            work_project = session_project(project_dir)
        except Exception:
            work_project = project_dir
    return work_project, resume_files


def _rules_block(work_project: str, project_memory: "dict | None" = None) -> list:
    """The banner's rules lines: the top five rules of the session's project
    (CLAUDE.md-covered ones skipped), with ids. Shared with windvane.brief so
    a subagent's brief reads exactly as the banner."""
    if project_memory is None:
        project_memory = load_project_memory(work_project)
    rules = filter_rules_in_claude_md(get_project_rules(project_memory), work_project)
    if not rules:
        return []
    out = [f"Rules ({len(rules)}, {_project_label(work_project)}):"]
    out.extend(f"  [{r['id']}] {_truncate(r['content'], 120)}" for r in rules[:5])
    return out


def _banner_checkpoint(project_dir: str, work_project: str, source: str, has_resume_files: bool, transcript: str) -> tuple:
    """(record, skipped) the banner restores. On resume and compact THIS
    session's own newest deliberate checkpoint comes first (session_id on
    the ring record), skipping one the user rewound past; the project's
    newest is the fallback. The banner renders it whole on resume/compact
    (_format_restored_full), as a teaser on a fresh start."""
    restored: "dict | None" = {}
    skipped: list = []
    if source in ("resume", "compact"):
        try:
            _dirs = _handoff_candidate_dirs(work_project)
            for _p, _info in _get_manifest().get("projects", {}).items():
                if _p.startswith(_normalize_path(project_dir) + "/") and _info.get("hash"):
                    _d = get_windvane_storage_dir() / "projects" / _info["hash"]
                    if _d not in _dirs:
                        _dirs.append(_d)
            restored, skipped = _own_session_checkpoint(_dirs, _session_id, transcript)
        except Exception:
            restored, skipped = {}, []
    if restored:
        return restored, skipped
    restored = {}
    if has_resume_files:
        restored = get_handoff_data(work_project)
    if not restored:
        restored = _subtree_manual_handoff(project_dir)
    if not restored:
        restored = get_handoff_data(project_dir)
    return restored or {}, skipped


def _compaction_briefed(source: str, session_id: str) -> bool:
    """After a compaction the windvane mod may have placed the rules and the
    checkpoint inside the compacted conversation itself (its session.compact
    hook writes sessions/<sid>.briefed). The banner then leaves both out
    instead of showing them twice."""
    if source != "compact":
        return False
    try:
        from windvane import pressure as _cp

        return _cp.compaction_briefed(session_id)
    except Exception:
        return False

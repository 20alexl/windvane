"""PostToolUse, PostToolUseFailure and PostToolBatch.

- Bash (``bash_json``): test tracking (the status speaks on the first run
  and each flip; a passing targeted run is remembered for the next banner),
  error auto-logging, the nearest code-index symbols after an empty search,
  and the effect accounting for the stall ladder.
- Edit/Write (``post_edit_json``): edit tracking (the count feeds the loop
  warning before the next edit).
- ExitPlanMode/TaskUpdate (``post_milestone_json``): a plan approval asks
  for the plan to be banked; a completed task stages a milestone nudge.
- Any failed tool (``tool_failure_json``): error deja-vu, the mistake
  auto-log, the failing test record.
- The batch (``post_batch_json``): every call accounted to the open turn,
  compliance for the non-shell calls, paths this session created.

Each injection point also delivers the context-pressure nudges.
"""

import hashlib
import json
import re
import time
from pathlib import Path

from windvane.events.common import (
    _append_memory_entry,
    _command_can_run_tests,
    _compliance_check,
    _find_patterns_report,
    _get_manifest,
    _increment_tool_usage,
    _is_code_file,
    _is_test_invocation,
    _normalize_path,
    _output_has_test_markers,
    _read_stdin_with_timeout,
    _record_test_command,
    _track_tool_duration,
    _with_pressure,
    emit_context,
    get_project_dir,
    get_project_memory_dir,
    load_state,
    record_file_edit,
    save_state,
    session_project,
)


# ============================================================================
# Loop tracker records
# ============================================================================


def _auto_record_edit(file_path: str, description: str = "auto-tracked") -> int:
    """
    Record an edit in the per-session loop tracker (invisible, post-edit).
    Returns the file's updated edit count so callers don't re-read state.
    """
    state = load_state()
    loop = state.setdefault("loop", {})
    counts = loop.setdefault("edit_counts", {})
    file_name = Path(file_path).name
    counts[file_path] = counts.get(file_path, 0) + 1
    counts[file_name] = counts.get(file_name, 0) + 1  # Track both full path and name
    state["last_loop_record"] = time.time()
    state["last_loop_file"] = file_path
    _increment_tool_usage(state, "loop_record_edit")
    save_state(state)
    return counts[file_path]


def _auto_record_test(passed: bool, error_message: str = "") -> str:
    """
    Record a test result in the per-session loop tracker (invisible).
    Returns confirmation message.
    """
    state = load_state()
    loop = state.setdefault("loop", {})

    # Link test result to recently-edited files
    recent_files = state.get("files_edited_this_session", [])[-5:]
    recent_file_names = [Path(f).name for f in recent_files]

    test_results = loop.setdefault("test_results", [])
    test_results.append(
        {
            "timestamp": time.time(),
            "passed": passed,
            "error_message": error_message[:200] if error_message else "",
            "files_since_last_test": recent_file_names,
        }
    )
    # Keep last 20 results
    loop["test_results"] = test_results[-20:]

    state["last_test_record"] = time.time()
    state["last_test_passed"] = passed
    state["test_runs_this_session"] = state.get("test_runs_this_session", 0) + 1
    _increment_tool_usage(state, "loop_record_test")
    save_state(state)

    return "PASSED" if passed else "FAILED"


def _recent_code_files(state: dict) -> list:
    """Names of the code files this session edited most recently (the
    turn's list, else the previous turn's): what a passing targeted run
    covered."""
    files = list(state.get("files_edited_this_session") or []) or list(state.get("last_session_files") or [])
    return [Path(f).name for f in files[-5:] if _is_code_file(f)]


# ============================================================================
# Mistake auto-log
# ============================================================================


def _auto_log_detected_mistake_with_files(
    project_dir: str, command: str, output: str, related_files: list[str]
) -> str:
    """Log a detected mistake with explicit related_files (from test failure linking)."""
    result = _auto_log_detected_mistake(project_dir, command, output)
    if result and related_files:
        # Patch the just-written entry with related_files
        try:
            norm_dir = _normalize_path(project_dir)
            manifest = _get_manifest()
            if manifest.get("projects") and norm_dir in manifest["projects"]:
                pdir = get_project_memory_dir(project_dir)
                mem_file = pdir / "memory.json"
                if mem_file.exists():
                    data = json.loads(mem_file.read_text())
                    entries = data.get("entries", [])
                    if entries:
                        # Patch the most recent entry (just added)
                        last = entries[-1]
                        existing = set(last.get("related_files", []))
                        existing.update(related_files)
                        last["related_files"] = sorted(existing)[:10]
                        data["entries"] = entries
                        temp = mem_file.with_suffix(".json.tmp")
                        temp.write_text(json.dumps(data, indent=2))
                        temp.replace(mem_file)
        except Exception:
            pass
    return result


def _auto_log_detected_mistake(project_dir: str, command: str, output: str) -> str:
    """
    Auto-detect and log common mistake patterns from command output.
    Returns description of logged mistake, or empty string if nothing detected.

    Called from PostToolUse (exit 0 commands) and PostToolUseFailure
    (exit != 0); PostToolUseFailure is the primary path for catching errors.
    """
    if not output or len(output) > 5000:
        return ""

    # Only log mistakes that reference actual project files in the traceback.
    # Errors in <string> (inline python), temp dirs, or pip packages are transient noise.
    has_project_file = bool(
        re.search(
            r'File "(?!<)(?!.*[\\/](?:site-packages|dist-packages|venv|\.venv|tmp|temp)[\\/])'
            r'[^"]+\.py"',
            output,
        )
    )
    if not has_project_file and ("Traceback" in output or "File " in output):
        return ""

    mistake_type = None
    how_to_avoid = None

    # Pattern 1: Import/Module errors
    if "ModuleNotFoundError" in output or "ImportError" in output:
        # Try "No module named 'X'" first
        match = re.search(r"No module named ['\"]([^'\"]+)['\"]", output)
        if match:
            module = match.group(1)
            mistake_type = f"Import error: Module '{module}' not found"
            how_to_avoid = f"Install missing module: pip install {module}"
        else:
            # Try "cannot import name 'X' from 'Y'"
            match2 = re.search(
                r"cannot import name ['\"]([^'\"]+)['\"] from ['\"]([^'\"]+)['\"]",
                output,
            )
            if match2:
                name, module = match2.group(1), match2.group(2)
                mistake_type = f"Import error: cannot import '{name}' from '{module}'"
                how_to_avoid = (
                    f"Check that '{name}' exists in {module}, or update the package"
                )
            # If neither regex matched, skip - don't log "unknown"

    # Pattern 2: Syntax errors
    elif "SyntaxError" in output:
        match = re.search(r"File ['\"]([^'\"]+)['\"], line (\d+)", output)
        if match:
            file_name = Path(match.group(1)).name
            line = match.group(2)
            mistake_type = f"Syntax error in {file_name}:{line}"
            how_to_avoid = "Check syntax before running - use linter or read the file"

    # Pattern 3: Type errors (only log if we can parse the actual message)
    elif "TypeError" in output:
        match = re.search(r"TypeError: (.+)", output)
        if match:
            error_msg = match.group(1)[:60]
            mistake_type = f"Type error: {error_msg}"
            how_to_avoid = "Check argument types and return values"

    # Pattern 4: Attribute errors (only log if we can parse the actual message)
    elif "AttributeError" in output:
        match = re.search(r"AttributeError: (.+)", output)
        if match:
            error_msg = match.group(1)[:60]
            mistake_type = f"Attribute error: {error_msg}"
            how_to_avoid = "Check object type and available attributes"

    # Pattern 5: Test failures -- DON'T auto-log. Running tests and finding
    # failures is expected behavior, not a mistake.

    # Pattern 6: Permission errors
    elif "PermissionError" in output or "Permission denied" in output:
        mistake_type = "Permission error"
        how_to_avoid = "Check file permissions or run with appropriate privileges"

    # Pattern 7: Connection errors
    elif "ConnectionError" in output or "Connection refused" in output:
        mistake_type = "Connection error - service may not be running"
        how_to_avoid = "Ensure the required service is running"

    if mistake_type:
        try:
            # Write directly to the per-project file (fast, no full store load)
            content = f"MISTAKE: {mistake_type}"
            if how_to_avoid:
                content += f" - Fix: {how_to_avoid}"

            # Extract entities: files, classes, functions from traceback
            tags = ["mistake", "bugfix"]
            # Extract ALL project files from traceback (not site-packages/venv)
            related_files = []
            seen_files = set()
            for fm in re.finditer(r'File ["\']([^"\']+\.py)["\']', output):
                fpath = fm.group(1)
                if fpath.startswith("<"):
                    continue
                if re.search(
                    r"[\\/](?:site-packages|dist-packages|venv|\.venv)[\\/]", fpath
                ):
                    continue
                fname = Path(fpath).name
                if fname not in seen_files:
                    seen_files.add(fname)
                    related_files.append(fname)
            # Extract class/object names from AttributeError
            obj_match = re.search(r"'(\w+)' object has no attribute", output)
            if obj_match:
                tags.append(obj_match.group(1).lower())
            # Extract function names
            func_match = re.search(r"in (\w+)\n", output)
            if func_match and func_match.group(1) not in ("__init__", "<module>"):
                tags.append(func_match.group(1))

            entry_id = hashlib.md5(content.encode()).hexdigest()[:12]

            _append_memory_entry(
                project_dir,
                {
                    "id": entry_id,
                    "content": content,
                    "category": "mistake",
                    "source": "auto-detected",
                    "relevance": 9,
                    "created_at": time.time(),
                    "last_accessed": time.time(),
                    "access_count": 1,
                    "tags": tags,
                    "related_files": related_files,
                },
            )

            return mistake_type
        except Exception:
            pass  # Silent failure

    return ""


# ============================================================================
# Error deja-vu (PostToolUseFailure)
# ============================================================================


_DEJAVU_ERR_LINE = re.compile(r"^(\w+(?:Error|Exception))\s*:\s*(.+)$", re.MULTILINE)


def _parse_error_line(error_output: str) -> "tuple[str, str]":
    """Pull the operative (class, message) out of a failure blob.

    The LAST `SomeError: message` line wins -- chained tracebacks end with
    the error that actually surfaced. Class-less output (Edit conflicts,
    CLI failures) falls back to its first non-empty line.
    """
    err_class, err_msg = "", ""
    for m in _DEJAVU_ERR_LINE.finditer(error_output):
        err_class, err_msg = m.group(1), m.group(2)
    if not err_msg:
        for ln in error_output.splitlines():
            if ln.strip():
                err_msg = ln
                break
    return err_class, " ".join(err_msg.split())[:300]


def _norm_err_class(label: str) -> str:
    """'AttributeError' and 'Attribute error' compare equal."""
    return re.sub(r"[^a-z]", "", label.lower())


def _prefix_match(a: str, b: str, min_len: int) -> bool:
    """Truncation-tolerant equality: stored mistakes clip messages (60-120
    chars, sometimes mid-identifier), so compare the shared prefix."""
    k = min(len(a), len(b))
    return k >= min_len and a[:k] == b[:k]


def _quoted_ids(s: str) -> set:
    """Quoted identifiers in an error message ('Foo', 'bar_attr')."""
    return set(re.findall(r"['\"]([^'\"]{1,60})['\"]", s))


def _word_overlap(a: str, b: str) -> float:
    """Overlap on the smaller word set -- class-less manual mistake
    descriptions never share an exact prefix with raw tool errors."""
    wa = set(re.findall(r"[a-z0-9_]+", a.lower()))
    wb = set(re.findall(r"[a-z0-9_]+", b.lower()))
    if len(wa) < 5 or len(wb) < 5:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def _error_dejavu(project_dir: str, error_output: str) -> str:
    """Match a fresh failure against past mistakes and surface the fix inline.

    Sources, in value order: mined recurring errors (patterns.json -- carries
    how-to-avoid text extracted from past conversations, plus cross-session
    counts), then hot memory.json mistakes (manual entries and auto-logs the
    miner hasn't folded in yet). Template matching reuses the miner's
    signature normalization so "same error, different identifier" still
    hits -- but a mined fix is only surfaced when the fresh error and the
    stored example share a quoted identifier, so an unrelated class doesn't
    inherit someone else's fix.

    Returns one line or "". Never raises -- this sits on the failure-hook path.
    """
    try:
        from windvane.mining.patterns import _normalize_error_msg

        err_class, err_msg = _parse_error_line(error_output)
        if len(err_msg) < 12:
            return ""
        norm_class = _norm_err_class(err_class)
        tmpl_msg = _normalize_error_msg(err_msg)

        # Tier 1: mined recurring errors -- fixes extracted from past
        # conversations, not canned advice.
        recurring_hit = None
        if norm_class:
            fresh_ids = _quoted_ids(err_msg)
            for e in _find_patterns_report(project_dir).get("recurring_errors", []):
                if _norm_err_class(e.get("error_type", "")) != norm_class:
                    continue
                pat = e.get("message_pattern", "")
                stored_tmpl = pat.split(": ", 1)[1] if ": " in pat else pat
                if not _prefix_match(tmpl_msg, stored_tmpl, 15):
                    continue
                stored_ids = _quoted_ids(e.get("example", ""))
                if fresh_ids and stored_ids and not (fresh_ids & stored_ids):
                    continue  # same shape, different subject -- fix won't transfer
                if e.get("fix"):
                    n = e.get("session_count", 0)
                    return (
                        f"Deja vu: {err_class} hit in {n} past session(s) - "
                        f"fix: {e['fix']}"
                    )[:300]
                if recurring_hit is None:
                    recurring_hit = e

        # Tier 2: hot mistake store -- exact-ish, truncation-tolerant.
        try:
            from windvane.hot_reader import HotMemoryReader

            entries = HotMemoryReader().load_entries(project_dir)
        except Exception:
            entries = []
        best = None  # (created_at, line)
        for m in entries:
            if m.get("category") != "mistake":
                continue
            content = m.get("content", "")
            desc = content[9:] if content.startswith("MISTAKE: ") else content
            fix = ""
            if " - Fix: " in desc:
                desc, fix = desc.split(" - Fix: ", 1)
            desc = " ".join(desc.split())
            label, sep, stored_msg = desc.partition(": ")
            same_class = (
                bool(sep) and norm_class and _norm_err_class(label) == norm_class
            )
            if same_class:
                hit = _prefix_match(err_msg, stored_msg, 20)
            else:
                hit = _word_overlap(err_msg, desc) >= 0.5
            if not hit:
                continue
            try:
                ts = float(m.get("created_at") or 0)
            except (TypeError, ValueError):
                ts = 0.0
            if best is None or ts > best[0]:
                if not ts:
                    when = "before"
                elif time.time() - ts < 86400:
                    when = "earlier today"
                else:
                    when = "on " + time.strftime("%Y-%m-%d", time.localtime(ts))
                line = f"Deja vu: you hit this {when}"
                if fix:
                    line += f" - fix: {fix}"
                best = (ts, line[:300])
        if best:
            return best[1]

        # Tier 3: recurrence without a recorded fix is still a wake-up call.
        if recurring_hit is not None:
            n = recurring_hit.get("session_count", 0)
            ex = recurring_hit.get("example", "")[:120]
            return f"Deja vu: {err_class} recurring ({n} sessions): {ex}"[:300]
    except Exception:
        pass
    return ""


# ============================================================================
# An empty search: the nearest symbols the code index knows
# ============================================================================


_SEARCH_EXES = frozenset({"grep", "egrep", "fgrep", "rg", "select-string", "sls", "findstr"})
# Flags of grep / rg / Select-String that take a value (the value is not the pattern).
_VALUE_FLAGS = frozenset(
    {
        "-f", "--file", "-g", "--glob", "--iglob", "-t", "--type", "-T", "--type-not", "-A", "-B", "-C",
        "--after-context", "--before-context", "--context", "-m", "--max-count", "--include", "--exclude",
        "--exclude-dir", "-d", "--max-depth", "-path", "-literalpath", "-encoding", "-context", "-include",
        "-exclude", "--color", "--colour", "-j", "--threads", "--sort", "-E", "--encoding",
    }
)
_PATTERN_FLAGS = frozenset({"-e", "--regexp", "-pattern"})
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\n|\|(?!\|)")


def _search_query(command: str) -> str:
    """The pattern of the search a command ran (grep, rg, git grep,
    Select-String), or "" when the command is not a search."""
    for seg in _SEGMENT_SPLIT.split(command or ""):
        toks = [next(t for t in m if t) if any(m) else "" for m in re.findall(r"\"([^\"]*)\"|'([^']*)'|(\S+)", seg)]
        toks = [t for t in toks if t]
        if not toks:
            continue
        exe = toks[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if exe.endswith(".exe"):
            exe = exe[:-4]
        if exe == "git" and len(toks) > 1 and toks[1].lower() == "grep":
            args = toks[2:]
        elif exe in _SEARCH_EXES:
            args = toks[1:]
        else:
            continue
        i = 0
        while i < len(args):
            a = args[i]
            low = a.lower()
            if low in _PATTERN_FLAGS and i + 1 < len(args):
                return args[i + 1]
            if low.startswith("--regexp="):
                return a.split("=", 1)[1]
            if low in _VALUE_FLAGS:
                i += 2
                continue
            if a.startswith("-"):
                i += 1
                continue
            return a
        return ""
    return ""


def _empty_search_hint(project_dir: str, command: str) -> str:
    """One line naming the code index's nearest symbols to the pattern of
    a search that found nothing. "" when the command was not a search, the
    project has no index, or nothing is near."""
    query = _search_query(command)
    if not query:
        return ""
    try:
        from windvane.code_index import nearest_symbols
    except Exception:
        return ""
    try:
        names = nearest_symbols(session_project(project_dir), query, n=5)
    except Exception:
        return ""
    if not names:
        return ""
    q = query if len(query) <= 60 else query[:57] + "..."
    return f'<windvane-search>No match for "{q}"; nearest symbols in the code index: {", ".join(names)}</windvane-search>'


def _failure_is_empty_search(error_msg: str) -> bool:
    """A failed Bash call that is only a search's "no match" exit: exit
    status 1 and no other output."""
    text = str(error_msg or "")
    if not re.search(r"(?i)exit code 1\b", text):
        return False
    return not re.sub(r"(?i)\bexit code \d+\b", "", text).strip()


# ============================================================================
# Bash
# ============================================================================


def reminder_for_bash(
    project_dir: str, command: str = "", exit_code: str = "", output: str = ""
) -> str:
    """
    The PostToolUse (Bash) block: a test run is recorded (spoken on the
    first run and each flip), a failing test's error is logged against the
    files it names, and a failed command's error is auto-logged.
    """
    lines = []
    has_content = False

    # Must check the FIRST command in a chain, not substrings in commit
    # messages/heredocs.
    command_lower = command.lower().strip() if command else ""
    first_cmd = re.split(r"&&|\|\||;|\$\(", command_lower)[0].strip()

    # Full suite patterns - must be the start of the first command
    full_suite_patterns = [
        "npm test",
        "yarn test",
        "make test",
        "cargo test",
        "go test ./...",
    ]
    is_full_suite = any(
        first_cmd.startswith(p) or first_cmd == p for p in full_suite_patterns
    )

    # Commands that might be full suite OR targeted - check first command only
    if not is_full_suite:
        test_commands = [
            "pytest",
            "jest",
            "mocha",
            "python -m unittest",
            "python -m pytest",
        ]
        for cmd in test_commands:
            if first_cmd.startswith(cmd):
                parts = first_cmd.split()
                cmd_parts = cmd.split()
                remaining = parts[len(cmd_parts):]
                has_path = any(
                    ".py" in arg or "/" in arg or "\\" in arg or "::" in arg
                    for arg in remaining
                    if not arg.startswith("-")
                )
                if not has_path:
                    is_full_suite = True
                break

    # Custom runners: make check, make tests, ./run_tests.sh, etc.
    if not is_full_suite:
        custom_runners = ["make check", "make tests", "./run_tests", "run_tests.sh"]
        if any(p in first_cmd for p in custom_runners):
            is_full_suite = True

    # Detect test runs by OUTPUT, not command: any command whose output has
    # test markers ("X passed", "collected N items", "Ran N tests", ...) when
    # the command could have run a test. Skips probes like
    # `python -c "print(...)"`. A bare "N errors" is not a marker: `uv lock`,
    # linters and smoke scripts print one without running a test.
    if not is_full_suite and output and _command_can_run_tests(command) and _output_has_test_markers(output):
        is_full_suite = True

    if is_full_suite:
        passed = exit_code == "0"
        state = load_state()
        last_passed = state.get("last_test_passed")

        # Check if state changed (for informative message)
        state_changed = last_passed is not None and last_passed != passed
        first_run = last_passed is None

        # AUTO-RECORD the test result (no manual call needed)
        error_snippet = output[:200] if output and not passed else ""
        result = _auto_record_test(passed, error_snippet)
        _record_test_command(project_dir, command, passed, _recent_code_files(state))

        # On test failure, link to recently-edited files in the mistake
        if not passed and error_snippet:
            # The files a failure attaches to: the ones the traceback names,
            # else the CODE files edited this session. A markdown file
            # edited near the failure cannot have raised it.
            traced = re.findall(r'File ["\']([^"\']+\.py)["\']', output or "")
            traced = [t for t in traced if not re.search(r"[\\/](?:site-packages|dist-packages|venv|\.venv)[\\/]", t)]
            error_project = get_project_dir(traced[0]) if traced else project_dir
            if traced:
                related = list(dict.fromkeys(traced))[:5]
            else:
                related = [
                    f for f in load_state().get("files_edited_this_session", [])[-5:]
                    if _is_code_file(f)
                ]
            if related:
                _auto_log_detected_mistake_with_files(
                    error_project, command, error_snippet, related[:5]
                )

        # Speak only when the status is news: the first run and every flip.
        # A same-verdict rerun is tracked in silence.
        status_word = "PASS" if passed else "FAIL"
        if state_changed or first_run:
            lines.append("<windvane-test-tracked>")
            if state_changed:
                if passed:
                    lines.append(f"{status_word} Test tracked: NOW PASSING (were failing)")
                else:
                    lines.append(f"{status_word} Test tracked: NOW FAILING (were passing)")
            else:
                lines.append(f"{status_word} Test tracked: {result} (baseline established)")
            lines.append("</windvane-test-tracked>")
            has_content = True

    # Check if command failed
    if exit_code and exit_code != "0":
        # Resolve sub-project from file paths in output
        error_project = project_dir
        if output:
            file_match = re.search(r'File ["\']([^"\']+\.py)["\']', output)
            if file_match:
                error_project = get_project_dir(file_match.group(1))

        # Auto-detect and auto-log common mistakes -- but NOT from test
        # invocations: a failing test is already tracked as a test result,
        # and TDD RED-phase errors are deliberate, not mistakes.
        auto_logged = ""
        if not (is_full_suite or _is_test_invocation(command)):
            auto_logged = _auto_log_detected_mistake(error_project, command, output)

        if auto_logged:
            lines.append(f"<windvane-error-tracked>{auto_logged}</windvane-error-tracked>")
            has_content = True

    if has_content:
        return "\n".join(lines)
    return ""


def _hook_bash(project_dir: str) -> None:
    """PostToolUse(Bash): stall accounting, durations, the loop reset on a
    commit, test tracking, the empty-search hint and the nudges."""
    try:
        stdin_data = _read_stdin_with_timeout(0.5)
        if not stdin_data:
            return
        data = json.loads(stdin_data)

        # Effect accounting for the stall ladder (windvane.stall); a
        # fallback for settings without the PostToolBatch hook.
        try:
            from windvane import stall as _stall

            _sst = load_state()
            _stall.note_tool(_sst, "Bash", data.get("tool_input"), data.get("tool_response"))
            save_state(_sst)
        except Exception:
            pass

        # Subagents: no output injection, no tracking (utility work, not user flow)
        if data.get("agent_id"):
            return

        duration_ms = data.get("duration_ms", 0)
        if duration_ms:
            state = load_state()
            _track_tool_duration(state, "Bash", duration_ms)
            save_state(state)
        command = data.get("tool_input", {}).get("command", "")
        # tool_response is an object with stdout/stderr fields
        tool_response = data.get("tool_response", {})
        if isinstance(tool_response, dict):
            stdout = tool_response.get("stdout", "")
            stderr = tool_response.get("stderr", "")
            response = f"{stdout}\n{stderr}".strip()
        else:
            response = str(tool_response)

        # Reset loop edit counts on git commit (edit cycle completed);
        # test history is kept -- it still describes the current state.
        if command and "git commit" in command:
            try:
                state = load_state()
                if state.get("loop", {}).get("edit_counts"):
                    state["loop"]["edit_counts"] = {}
                    save_state(state)
            except Exception:
                pass

        # This handler only fires for successful commands (exit 0).
        # Don't manufacture fake errors from output content.
        result = reminder_for_bash(project_dir, command, "0", output=response)

        # A search that found nothing: the nearest symbols the index knows.
        if command and not response.strip():
            hint = _empty_search_hint(project_dir, command)
            if hint:
                result = f"{result}\n{hint}" if result else hint
        result = _with_pressure(result, project_dir)
        emit_context("PostToolUse", result)
    except Exception:
        pass  # Silent failure


# ============================================================================
# Edit / Write
# ============================================================================


def _hook_post_edit(project_dir: str) -> None:
    """PostToolUse(Edit|Write): durations, stall accounting, edit tracking
    (including a subagent's edits), and the nudges for the main session."""
    try:
        stdin_data = _read_stdin_with_timeout(0.5)
        if not stdin_data:
            return
        data = json.loads(stdin_data)

        is_subagent = bool(data.get("agent_id"))
        duration_ms = data.get("duration_ms", 0)
        if duration_ms and not is_subagent:
            state = load_state()
            _track_tool_duration(state, "Edit", duration_ms)
            save_state(state)
        # Effect accounting for the stall ladder (windvane.stall).
        try:
            from windvane import stall as _stall

            _sst = load_state()
            _stall.note_tool(
                _sst,
                str(data.get("tool_name") or "Edit"),
                data.get("tool_input"),
                data.get("tool_response"),
            )
            save_state(_sst)
        except Exception:
            pass
        file_path = data.get("tool_input", {}).get("file_path", "")
        if file_path:
            project_dir = get_project_dir(file_path)
            _auto_record_edit(file_path, "auto-tracked")

            # Track files edited (including by subagents -- we want to know)
            record_file_edit(file_path)

            # No per-edit line: the count feeds the loop warning before the
            # next edit; only the pressure nudges ride this hook, and never
            # for a subagent.
            if not is_subagent:
                emit_context("PostToolUse", _with_pressure("", project_dir))
    except Exception:
        pass  # Silent failure


# ============================================================================
# ExitPlanMode / TaskUpdate
# ============================================================================


def _hook_post_milestone(project_dir: str = "") -> None:
    """PostToolUse(ExitPlanMode|TaskUpdate): the two structural "unit
    boundary" moments. A plan approval hands over the milestone list, so ask
    for it to be banked as a checkpoint; a task marked completed is the
    model's "step done" stated structurally. Injects at once -- PostToolUse
    can -- rather than staging for later."""
    try:
        stdin_data = _read_stdin_with_timeout(0.5)
        if not stdin_data:
            return
        data = json.loads(stdin_data)
        if data.get("agent_id"):
            return
        tool = str(data.get("tool_name", ""))
        tool_input = data.get("tool_input") or {}
        from windvane import milestones as _ms
        from windvane import pressure as _cp

        result = ""
        if tool == "ExitPlanMode":
            result = _ms.plan_text()
        elif tool == "TaskUpdate":
            status = str(tool_input.get("status", "")).lower()
            if status == "completed":
                subject = (
                    tool_input.get("subject")
                    or tool_input.get("task_subject")
                    or tool_input.get("taskId")
                    or tool_input.get("task_id")
                    or "a task"
                )
                state = load_state()
                _cp.stage_milestone(state, str(subject), "task")
                save_state(state)
        result = _with_pressure(result, project_dir=get_project_dir())
        emit_context("PostToolUse", result)
    except Exception:
        pass


# ============================================================================
# Any failed tool
# ============================================================================


def _hook_tool_failure(project_dir: str) -> None:
    """PostToolUseFailure for every tool: {tool_name, tool_input, error,
    is_interrupt, tool_use_id}. Deja vu first, then the auto-log, the
    failing test record, and the empty-search hint for a search that
    exited 1 with nothing."""
    try:
        stdin_data = _read_stdin_with_timeout(0.5)
        if not stdin_data:
            return
        data = json.loads(stdin_data)
        duration_ms = data.get("duration_ms", 0)
        tool_name = data.get("tool_name", "")
        if duration_ms and tool_name:
            state = load_state()
            _track_tool_duration(state, tool_name, duration_ms)
            save_state(state)
        tool_input = data.get("tool_input", {})
        error_msg = data.get("error", "")
        is_interrupt = data.get("is_interrupt", False)

        # Resolve sub-project from file_path if available
        if isinstance(tool_input, dict):
            fp = tool_input.get("file_path", "")
            if fp:
                project_dir = get_project_dir(fp)

        # Don't log user interrupts or subagent errors as mistakes
        if is_interrupt or data.get("agent_id"):
            return
        lines = []
        hint = ""

        if tool_name == "Bash":
            command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""

            # Try to resolve sub-project from file paths in error output
            if error_msg:
                file_match = re.search(r'File ["\']([^"\']+\.py)["\']', error_msg)
                if file_match:
                    project_dir = get_project_dir(file_match.group(1))

            # Error deja-vu: surface a past fix BEFORE auto-log writes the
            # fresh entry (it must not match itself).
            if error_msg:
                dejavu = _error_dejavu(project_dir, error_msg)
                if dejavu:
                    lines.append(dejavu)

            # Auto-log mistake from error output -- except for test
            # invocations: the failure is already tracked as a test result,
            # and TDD RED-phase errors are deliberate, not mistakes.
            if error_msg and not _is_test_invocation(command):
                logged = _auto_log_detected_mistake(project_dir, command, error_msg)
                if logged:
                    lines.append(f"Auto-logged: {logged}")

            # Auto-record failed test
            if command and _is_test_invocation(command):
                _auto_record_test(False, error_msg[:200])
                _record_test_command(project_dir, command, False)
                lines.append("FAIL Test tracked")

            # grep / rg exit 1 when nothing matched.
            if command and _failure_is_empty_search(error_msg):
                hint = _empty_search_hint(project_dir, command)

        elif tool_name in ("Edit", "Write"):
            file_path = tool_input.get("file_path", "") if isinstance(tool_input, dict) else ""
            if file_path and error_msg:
                file_name = Path(file_path).name
                dejavu = _error_dejavu(project_dir, error_msg)
                if dejavu:
                    lines.append(dejavu)
                # Auto-log edit failure with file context
                _auto_log_detected_mistake(
                    project_dir,
                    f"edit {file_name}",
                    f"Edit failed on {file_name}: {error_msg[:200]}",
                )
                lines.append(f"Edit failure on {file_name} tracked")

        # No nag, no counter -- if auto-log didn't capture it, it wasn't
        # worth tracking. Only emit the tag when something was tracked.
        result = "\n".join(lines).strip()
        out = f"<windvane-error>{result}</windvane-error>" if result else ""
        if hint:
            out = f"{out}\n{hint}" if out else hint
        emit_context("PostToolUseFailure", out)
    except Exception:
        pass


# ============================================================================
# The batch
# ============================================================================


def _note_created_paths(state: dict, calls: list) -> None:
    """Remember what this session created (Write targets, mkdir arguments)
    so a later deletion of the same path reads as cleanup, not as the
    destructive act the rule guards. Capped; paths normalized."""
    import os

    created = list(state.get("created_paths") or [])
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict):
            continue
        name = str(call.get("tool_name") or "")
        _ti = call.get("tool_input")
        ti: dict = _ti if isinstance(_ti, dict) else {}
        if name in ("Write", "NotebookEdit"):
            fp = str(ti.get("file_path") or ti.get("notebook_path") or "")
            if fp:
                created.append(_normalize_path(fp))
        elif name in ("Bash", "PowerShell"):
            cmd = str(ti.get("command") or "")
            for m in re.finditer(r"\bmkdir\b(?:\s+-\w+)*\s+((?:\"[^\"]+\"|'[^']+'|\S+)(?:\s+(?:\"[^\"]+\"|'[^']+'|\S+))*)", cmd):
                for tok in re.findall(r"\"([^\"]+)\"|'([^']+)'|(\S+)", m.group(1)):
                    p = next(t for t in tok if t)
                    if p.startswith("-"):
                        continue
                    created.append(_normalize_path(p) if os.path.isabs(p) else p.replace("\\", "/"))
    if created:
        state["created_paths"] = created[-60:]


def _hook_post_batch(project_dir: str) -> None:
    """PostToolBatch: account every call in the batch to the open turn
    (windvane.stall) and deliver whatever nudge is due. The batch payload
    carries all calls with their inputs and responses, matcher-free, so this
    is the one place that sees NotebookEdit, MCP writes and wait primitives
    without a PostToolUse entry each."""
    try:
        stdin_data = _read_stdin_with_timeout(0.5)
        if not stdin_data:
            return
        data = json.loads(stdin_data)
        calls = data.get("tool_calls") or []
        from windvane import stall as _stall

        state = load_state()
        project_dir = session_project(project_dir, state)
        _stall.note_batch(state, calls if isinstance(calls, list) else [])
        _note_created_paths(state, calls if isinstance(calls, list) else [])
        # Compliance on every non-shell call (path globs on edits, MCP tools
        # by name); shell calls were matched at PreToolUse and dedupe by
        # tool_use_id here.
        try:
            for call in calls if isinstance(calls, list) else []:
                if not isinstance(call, dict):
                    continue
                _tn = str(call.get("tool_name") or "")
                _payload = dict(data)
                _payload["tool_use_id"] = call.get("tool_use_id", "")
                _compliance_check(project_dir, _payload, _tn, call.get("tool_input"), state)
        except Exception:
            pass
        save_state(state)
        if data.get("agent_id"):
            return  # a subagent's batch: counted, never nudged
        emit_context("PostToolBatch", _with_pressure("", project_dir))
    except Exception:
        pass

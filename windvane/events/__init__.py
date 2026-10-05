"""The hook events: one module per Claude Code event, one dispatcher.

``python -m windvane.events <event>`` runs one event with the hook's JSON on
stdin. The plugin's classic hooks reach it through ``daemon_client.py``
(served warm by the daemon, or in-process when the daemon is down), and the
daemon calls ``dispatch(event, stdin_text)`` directly.

The event names are the wire names the mod's bridge and the hooks.json
commands send:

=====================  =================================  ======================
event                  Claude Code hook                    module
=====================  =================================  ======================
session_start_json     SessionStart                        session_start
prompt_json            UserPromptSubmit                    prompt
pre_edit_json          PreToolUse Edit|Write               pre_tool
pre_read_json          PreToolUse Read                     pre_tool
pre_bash_json          PreToolUse Bash|PowerShell          pre_tool
pre_tool_json          PreToolUse (every tool: the halt)   pre_tool
bash_json              PostToolUse Bash                    post_tool
post_edit_json         PostToolUse Edit|Write              post_tool
post_milestone_json    PostToolUse ExitPlanMode|TaskUpdate post_tool
tool_failure_json      PostToolUseFailure                  post_tool
post_batch_json        PostToolBatch                       post_tool
stop_json              Stop                                stop
stop_failure_json      StopFailure                         stop
notification_json      Notification                        stop
session_end_json       SessionEnd                          stop
pre_compact_json       PreCompact                          compact
post_compact_json      PostCompact                         compact
=====================  =================================  ======================

Shared pieces (the per-session state, session scoping, the banner pieces)
live in ``windvane.events.common``.
"""

import os as _os
import sys

# `python -m windvane.events` runs from the session's working directory,
# which Python puts FIRST on sys.path. A session cd'd into a vendored package
# holding an email.py (or json.py, ...) would have every hook die at import:
# that file shadows the stdlib module. Under -m, drop the cwd entry before
# the event modules import anything; this package resolves through its
# __path__ and needs nothing from the cwd. (Python 3.11+ has -P for the
# same; this covers 3.10.)
if sys.argv[:1] == ["-m"] and sys.path and not getattr(sys.flags, "safe_path", False):
    try:
        _head = sys.path[0]
        if _head == "" or _os.path.normcase(_os.path.abspath(_head)) == _os.path.normcase(_os.getcwd()):
            del sys.path[0]
    except Exception:
        pass

# event -> (module, handler). bash_failure_json is the older name of
# tool_failure_json and is still accepted.
EVENTS = {
    "session_start_json": ("session_start", "_hook_session_start"),
    "prompt_json": ("prompt", "_hook_prompt"),
    "pre_edit_json": ("pre_tool", "_hook_pre_edit"),
    "pre_read_json": ("pre_tool", "_hook_pre_read"),
    "pre_bash_json": ("pre_tool", "_hook_pre_bash"),
    "pre_tool_json": ("pre_tool", "_hook_pre_tool"),
    "bash_json": ("post_tool", "_hook_bash"),
    "post_edit_json": ("post_tool", "_hook_post_edit"),
    "post_milestone_json": ("post_tool", "_hook_post_milestone"),
    "tool_failure_json": ("post_tool", "_hook_tool_failure"),
    "bash_failure_json": ("post_tool", "_hook_tool_failure"),
    "post_batch_json": ("post_tool", "_hook_post_batch"),
    "stop_json": ("stop", "_hook_stop"),
    "stop_failure_json": ("stop", "_hook_stop_failure"),
    "notification_json": ("stop", "_hook_notification"),
    "session_end_json": ("stop", "_hook_session_end"),
    "pre_compact_json": ("compact", "_hook_pre_compact"),
    "post_compact_json": ("compact", "_hook_post_compact"),
}


def _handler(event: str):
    entry = EVENTS.get(event)
    if not entry:
        return None
    import importlib

    module = importlib.import_module(f"windvane.events.{entry[0]}")
    return getattr(module, entry[1], None)


def main() -> None:
    """Run the event named by ``sys.argv[1]`` with the hook JSON on stdin
    (or in ``common._stdin_cache`` when a caller already holds it). An
    unknown event is silent."""
    from windvane.events import common

    hook_type = sys.argv[1] if len(sys.argv) > 1 else ""
    # Identify the Claude Code session before any state I/O so per-session
    # working state doesn't collide when two sessions share a workspace.
    common._init_session_id()
    handler = _handler(hook_type)
    if handler is None:
        return
    project_dir = common.get_project_dir()
    handler(project_dir)


def dispatch(event: str, stdin_text: str) -> str:
    """Run one event in this process and return what it printed. The
    per-call globals (the cached stdin, the session id) are set for the call
    and reset after, so a long-lived caller (the daemon) serves one session's
    call without leaking it into the next."""
    import contextlib
    import io

    from windvane.events import common

    old_argv = sys.argv
    buf = io.StringIO()
    common._stdin_cache = stdin_text or ""
    common._session_id = ""
    sys.argv = ["windvane.events", event]
    try:
        with contextlib.redirect_stdout(buf):
            try:
                main()
            except SystemExit:
                pass
    finally:
        sys.argv = old_argv
        common._stdin_cache = None
        common._session_id = ""
    return buf.getvalue()

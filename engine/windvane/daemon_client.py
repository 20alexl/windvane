"""
Thin hook client -- the process Claude Code actually spawns per hook event.

Run with ``python -S`` (skip site initialization) by the plugin's classic
hooks:

    python -S "<plugin root>/engine/windvane/daemon_client.py" <event>

with the hook's JSON on stdin. This file imports only stdlib modules, so a
hook costs interpreter start + one TCP round trip to the daemon (~30-60ms)
instead of the full package import chain (~220ms). When the daemon is down,
unsupported, or anything at all goes wrong, it falls back to running the
real handler in-process (``windvane.hooks.dispatch``) -- same behaviour,
original cost -- and fire-and-forgets a daemon (re)start for the next call.

Invoked by path, not as a module (``-S`` leaves site-packages off sys.path,
and the plugin is not installed); the engine directory is derived from
__file__ for the fallback import and the daemon spawn.
"""

from typing import Any
import json
import os
import socket
import sys
import threading
import time

# Events this client sends to the daemon. Lifecycle events (session
# start/end, stop, compaction) run in-process from here: they are rare and
# this path pays an interpreter start either way. The daemon serves most of
# them too (daemon._HOOK_EVENTS), for the plugin's hooks module.
DAEMON_EVENTS = {
    "pre_edit_json",
    "post_edit_json",
    "bash_json",
    "prompt_json",
    "pre_read_json",
    "tool_failure_json",
    "post_batch_json",
    "pre_bash_json",
    "pre_tool_json",
}

# daemon.SESSION_ENV, copied: this file imports nothing of the package's
# (python -S). A test keeps the two in step.
SESSION_ENV = (
    "CLAUDE_PROJECT_DIR",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "CLAUDE_CONFIG_DIR",
    "WINDVANE_AUTONOMY",
    "WINDVANE_ALERT_COMMAND",
    "WINDVANE_STRIKE_CAP",
    "WINDVANE_GOAL_TURN_CAP",
    "WINDVANE_LIVE_MINE",
)

# <plugin root>/engine: the directory that holds the windvane package.
_ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _storage_dir() -> str:
    """The store, resolved the way windvane.config.store_dir() resolves it:
    WINDVANE_DIR when set, else ~/.windvane."""
    override = os.environ.get("WINDVANE_DIR", "").strip()
    if override:
        return os.path.expanduser(override)
    return os.path.join(os.path.expanduser("~"), ".windvane")


def _read_stdin(timeout_secs: float = 0.5) -> str:
    """One payload, or "" when stdin gives nothing within the timeout."""
    result = {"data": ""}

    def _read():
        try:
            result["data"] = sys.stdin.read()
        except Exception:
            pass

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout=timeout_secs)
    return result["data"]


_DEBUG = os.environ.get("WINDVANE_HOOK_DEBUG", "")


def _dbg(msg: str) -> None:
    if _DEBUG:
        sys.stderr.write(f"[daemon_client] {msg}\n")


def _try_daemon(hook_type: str, payload: str) -> bool:
    """One TCP round trip. True = response printed; False = use the fallback."""
    port_file = os.path.join(_storage_dir(), "daemon_port")
    try:
        with open(port_file) as f:
            port = int(f.read().strip())
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.25)
        sock.connect(("127.0.0.1", port))
        sock.settimeout(1.5)  # handlers are ~5-30ms warm; headroom, not budget
        request = {
            "hook_event": hook_type,
            "stdin": payload,
            # This process's own session environment, which the daemon
            # applies for the call (daemon.SESSION_ENV).
            "env": {k: os.environ.get(k, "") for k in SESSION_ENV},
        }
        sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        sock.close()
        response = json.loads(data.decode("utf-8").strip())
        if "output" not in response:
            _dbg(f"daemon error response: {str(response)[:120]}")
            return False  # old daemon / error -- run the real handler instead
        out = response["output"]
        if out:
            sys.stdout.write(out)
            sys.stdout.flush()
        _dbg("served by daemon")
        return True
    except Exception as e:
        _dbg(f"daemon unreachable: {type(e).__name__}: {e}")
        return False


def _run_fallback(hook_type: str, payload: str) -> None:
    """Run the real handler in this process (engine dir on sys.path; the
    site initialization skipped by -S doesn't matter for a source tree)."""
    sys.path.insert(0, _ENGINE_DIR)
    try:
        from windvane import hooks

        dispatch = getattr(hooks, "dispatch", None)
        if callable(dispatch):
            out = dispatch(hook_type, payload)
            if out:
                sys.stdout.write(out)
                sys.stdout.flush()
        else:
            # The argv-style entry: it reads the event from sys.argv[1] (as
            # this process already has it) and the cached stdin from common.
            from windvane.hooks import common

            common._stdin_cache = payload
            sys.argv = ["windvane.hooks", hook_type]
            hooks.main()
    except SystemExit:
        pass
    except Exception:
        pass


def _nudge_daemon() -> None:
    """Fire-and-forget daemon start so the NEXT hook call is fast. After the
    fallback has produced its output, so the only cost is this spawn.

    A 30s-TTL marker keeps a burst of falling-back hooks from spawning a
    pile of interpreters; the daemon's own process lock (windvane.proc_lock)
    is what guarantees that at most one of them stays."""
    if os.environ.get("WINDVANE_NO_DAEMON", "").strip():
        return  # benches / temporary stores: never spawn from here
    marker = os.path.join(_storage_dir(), "daemon_starting")
    try:
        if os.path.exists(marker) and time.time() - os.path.getmtime(marker) < 30:
            return
        with open(marker, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass
    try:
        import subprocess

        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(
            [sys.executable, "-m", "windvane.daemon"],
            cwd=_ENGINE_DIR,
            **kwargs,
        )
    except Exception:
        pass


def main() -> None:
    hook_type = sys.argv[1] if len(sys.argv) > 1 else ""
    if not hook_type:
        return
    payload = _read_stdin()

    if hook_type in DAEMON_EVENTS and _try_daemon(hook_type, payload):
        return

    _run_fallback(hook_type, payload)
    if hook_type in DAEMON_EVENTS:
        _nudge_daemon()


if __name__ == "__main__":
    main()

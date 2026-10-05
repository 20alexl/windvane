"""
The windvane daemon: one resident process that serves the hook events, the
plugin tools and (optionally) the semantic encoder warm.

A cold hook process pays interpreter start plus the package imports before
any work; the daemon keeps them loaded and serves each request in-process.
It auto-starts on the first hook that finds none, and exits after
``WINDVANE_DAEMON_TIMEOUT`` seconds idle (default 30 min) or as soon as the
package's source changes on disk.

The semantic tier is optional. With the ``semantic`` extra installed and the
tier requested (``"semantic": true`` in the plugin config, or
``WINDVANE_SEMANTIC=1``), the daemon loads the configured sentence-
transformers model in the background (``windvane.semantic``) and answers
scoring and embedding requests with it. Otherwise it never imports the
encoder and answers those requests with ``{"error": "no semantic tier"}``;
every caller then uses its regex tier.

Two protocols share the one loopback listener; the first line of a
connection decides which (``_handle_client``):

JSON lines over TCP (a first line starting with ``{``):
  Request:  {"text": "let's use Redis"}\\n
  Response: {"score": 0.85, "text": "let's use redis"}\\n
  Also {"embed": ...}, {"embed_batch": [...]}, and the hook dispatch
  {"hook_event": <hook_type>, "stdin": <hook stdin text>,
   "env": {"CLAUDE_PROJECT_DIR": ..., ...}}\\n -> {"output": <stdout text>}\\n
  or {"error": ...}\\n (the thin hook client, ``daemon_client.py``).

HTTP/1.1 (a first line like ``POST /hook HTTP/1.1``), for the plugin's
hooks module, which reaches the daemon through ``$.http.fetch``:
  POST /hook with Content-Length (no chunked bodies) and a JSON body of the
  same {"hook_event", "stdin", "env"} keys -> 200, Content-Type
  application/json, body {"output": ...} or {"error": ...}, Connection:
  close. POST /tool with {"tool", "arguments", "env"} -> 200 and the
  ``windvane.tools.run`` answer {"text", "isError", "ms"}. The request must
  carry ``X-Windvane-Hook: 1`` and, when it sends a Host, a loopback one: a
  web page can neither add the header cross-origin (no preflight is ever
  answered) nor pass the Host check through DNS rebinding, so a browser
  cannot drive the handlers. 403 otherwise, 404 for another path, 405 for
  another method, 400 for a malformed request, 411 without Content-Length,
  413 past ``HTTP_MAX_BODY``, 501 for a chunked body.

Entry point: ``python -m windvane.daemon``.
"""

from typing import Any
import json
import os
import re
import signal
import socket
import sys
import time
import threading
from pathlib import Path

from windvane.semantic.encoder import MAX_ENCODE_CHARS  # noqa: F401  (re-exported)

# How long to stay alive without requests (seconds)
IDLE_TIMEOUT = int(
    os.environ.get("WINDVANE_DAEMON_TIMEOUT", "1800")
)  # 30 min default

# The package's parent directory: what ``python -m windvane.daemon`` needs on
# sys.path. The plugin runs the engine from its own folder, not an install,
# so a spawned daemon starts with this as its working directory.
ENGINE_DIR = Path(__file__).resolve().parent.parent


# Where to store the port number so hooks can find us. MODEL_FILE records the
# embedding signature the running daemon loaded ("none" when it runs without
# the semantic tier): a client whose configured model differs must not use
# this daemon (vectors from two models share no space), so it replaces it
# instead. Lives under config.store_dir(), so an isolated store gets an
# isolated daemon (and tests never touch the real one).


def _storage_root() -> Path:
    from windvane.config import store_dir

    return Path(store_dir())


PORT_FILE = _storage_root() / "daemon_port"
PID_FILE = _storage_root() / "daemon_pid"
MODEL_FILE = _storage_root() / "daemon_model"
DEVICE_FILE = _storage_root() / "daemon_device"
# Held by the daemon for its lifetime (windvane.proc_lock). The kernel
# releases it when the process exits, so "held" means a live daemon and
# nothing else is consulted: a connect that fails (a stall, a full backlog)
# used to be read as a dead daemon, its files deleted, a second daemon
# spawned, and the first left idling 30 minutes at ~3 GB -- each exit then
# deleting the successor's files until the machine ran out of commit charge.
LOCK_FILE = _storage_root() / "daemon.lock"
# Marker that a spawn is under way (one spawn per 30 s); the daemon clears it
# when it binds. daemon_client.py writes the same file.
STARTING_FILE = _storage_root() / "daemon_starting"
# How many hooks may wait in the accept queue during a stall before a connect
# fails. 8 filled in under a second with three sessions and a workflow.
LISTEN_BACKLOG = 64

NO_SEMANTIC = "none"  # MODEL_FILE of a daemon that loaded no encoder


def _semantic_on() -> bool:
    try:
        from windvane import semantic

        return semantic.enabled()
    except Exception:
        return False


def _model_signature() -> str:
    """What MODEL_FILE should say for the current configuration."""
    if not _semantic_on():
        return NO_SEMANTIC
    from windvane.semantic.config import embed_signature

    return embed_signature()


# Hook events the daemon may run in-process: every handler that is safe
# inside a long-lived process serving many sessions. The thin hook client
# still sends only its own DAEMON_EVENTS; the rest arrive from the plugin's
# hooks module (POST /hook).
#
# The lifecycle handlers were checked one by one:
#   session_start_json  starts the daemon (start_server_background), which
#                       inside the daemon is a no-op: is_server_running()
#                       answers True while _SERVING; the banner is pure reads.
#   stop_json           live-mine tick = a detached spawn, lock-guarded.
#   pre_compact_json    banks the drafted checkpoint, spawns the miner
#                       (detached, lock-guarded).
#   post_compact_json   bookkeeping only.
#   stop_failure_json, notification_json
#                       a state record and an alert (subprocess with a timeout).
#   post_milestone_json a PostToolUse handler, not lifecycle at all.
# None of them calls sys.exit (bash_json does, caught below), and none keeps
# module state between calls beyond what the dispatch resets.
# session_end_json stays out: it runs while Claude Code is exiting, where a
# module's request may never be sent, and the classic hook is the path proven
# there (the run report and the post-session miner).
_HOOK_EVENTS = {
    "pre_edit_json",
    "post_edit_json",
    "bash_json",
    "prompt_json",
    "pre_read_json",
    "tool_failure_json",
    "post_batch_json",
    "pre_bash_json",
    "pre_tool_json",
    "post_milestone_json",
    "session_start_json",
    "stop_json",
    "pre_compact_json",
    "post_compact_json",
    "stop_failure_json",
    "notification_json",
}

# The per-session environment a hook process would have had: under the
# daemon these are set from the request's "env" for the call and restored
# after. A key the request carries is set (or unset when empty); a key it
# leaves out keeps the daemon's value, except the two in PER_CALL_ENV, which
# are unset when absent: CLAUDE_PROJECT_DIR because an older client sent it
# alone, CLAUDE_CODE_SESSION_ID because the daemon inherits the id of the
# session whose hook started it, and a call that names no session must not
# be filed under that one.
SESSION_ENV = (
    "CLAUDE_PROJECT_DIR",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "CLAUDE_CONFIG_DIR",
    "WINDVANE_AUTONOMY",
    "WINDVANE_ALERT_COMMAND",
    "WINDVANE_STRIKE_CAP",
    "WINDVANE_GOAL_TURN_CAP",
    "WINDVANE_LIVE_MINE",
)
PER_CALL_ENV = frozenset({"CLAUDE_PROJECT_DIR", "CLAUDE_CODE_SESSION_ID"})

# True inside serve(): this process IS the daemon, so a handler that asks for
# one (session_start_json's start_server_background, a scorer call's
# _ensure_server) must not probe, replace or kill it.
_SERVING = False

# The hook dispatch keeps per-event process state (the cached stdin, the
# session id, CLAUDE_PROJECT_DIR); under the daemon those are module globals,
# so dispatch is serialized. Handlers are ~5-30ms warm -- queueing beats races.
_HOOK_LOCK = threading.Lock()


def _apply_session_env(env: dict) -> dict:
    """Set the call's session environment; returns the values to restore."""
    old_env = {k: os.environ.get(k) for k in SESSION_ENV}
    for key in SESSION_ENV:
        if key not in env and key not in PER_CALL_ENV:
            continue
        value = env.get(key)
        if isinstance(value, str) and value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    return old_env


def _restore_env(old_env: dict) -> None:
    for key, old in old_env.items():
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old


def _dispatch(events, event: str, payload: str):
    """``events.dispatch(event, stdin_text)`` when the events package has it;
    else the argv-style ``events.main()`` with the per-call globals in
    ``events.common`` set and reset around it (what dispatch does itself).
    Returns dispatch's string, or None when main() printed its output."""
    dispatch = getattr(events, "dispatch", None)
    if callable(dispatch):
        return dispatch(event, payload)
    from windvane.events import common

    old_argv = sys.argv
    common._stdin_cache = payload
    common._session_id = ""
    sys.argv = ["windvane.events", event]
    try:
        events.main()
    finally:
        sys.argv = old_argv
        common._stdin_cache = None
        common._session_id = ""
    return None


def _serve_hook_event(request: dict) -> dict:
    """Run a hook dispatch in-process (``windvane.events.dispatch``). The
    entire win of the daemon: a cold hook process pays interpreter start +
    imports before any work; here only the work remains."""
    hook_type = str(request.get("hook_event", ""))
    if hook_type not in _HOOK_EVENTS:
        return {"error": f"unsupported hook_event {hook_type!r}"}
    payload = request.get("stdin", "") or ""
    _env = request.get("env")
    env: dict = _env if isinstance(_env, dict) else {}

    import contextlib
    import io

    with _HOOK_LOCK:
        from windvane import events

        old_env = _apply_session_env(env)
        buf = io.StringIO()
        out = ""
        try:
            with contextlib.redirect_stdout(buf):
                try:
                    ret = _dispatch(events, hook_type, payload)
                    if isinstance(ret, str):
                        out = ret
                except SystemExit:
                    pass
        except Exception as e:
            return {"error": str(e)[:200]}
        finally:
            _restore_env(old_env)
        # dispatch returns what the hook printed; anything a handler wrote to
        # stdout outside its capture is part of the hook's output too.
        return {"output": out + buf.getvalue()}


HTTP_MAX_HEAD = 64 * 1024
HTTP_MAX_BODY = 16 * 1024 * 1024  # a hook's stdin: a PostToolUse with a big tool result
_HTTP_REQUEST_LINE = re.compile(rb"^([A-Z]+) (\S+) HTTP/1\.[01]\r?\n")
_HTTP_REASONS = {
    200: "OK",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    411: "Length Required",
    413: "Payload Too Large",
    431: "Request Header Fields Too Large",
    501: "Not Implemented",
}
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
HOOK_HEADER = "X-Windvane-Hook"


def _http_reply(conn, status: int, body: dict) -> None:
    payload = json.dumps(body).encode("utf-8")
    head = (
        f"HTTP/1.1 {status} {_HTTP_REASONS.get(status, 'Error')}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    conn.sendall(head + payload)
    # A lingering close: an answer sent before the request was read whole (a
    # 404, a 413) would otherwise be lost to the reset that closing a socket
    # with unread bytes sends on Windows. The client has the answer already.
    try:
        conn.shutdown(socket.SHUT_WR)
        conn.settimeout(0.2)
        drained = 0
        while drained < HTTP_MAX_HEAD * 16:
            chunk = conn.recv(65536)
            if not chunk:
                break
            drained += len(chunk)
    except OSError:
        pass


def _serve_http(conn, data: bytes) -> None:
    """One HTTP/1.1 request on a connection whose first line said so.
    ``data`` is what was read so far (the request line at least). Answers
    once and returns; the caller closes the connection."""
    while b"\r\n\r\n" not in data and b"\n\n" not in data:
        if len(data) > HTTP_MAX_HEAD:
            _http_reply(conn, 431, {"error": "headers too large"})
            return
        chunk = conn.recv(4096)
        if not chunk:
            _http_reply(conn, 400, {"error": "incomplete request"})
            return
        data += chunk
    sep = b"\r\n\r\n" if b"\r\n\r\n" in data else b"\n\n"
    head, _, body = data.partition(sep)
    if len(head) > HTTP_MAX_HEAD:
        _http_reply(conn, 431, {"error": "headers too large"})
        return
    lines = head.decode("latin-1").replace("\r\n", "\n").split("\n")
    method, target, _version = (lines[0].split(" ") + ["", "", ""])[:3]
    headers: dict = {}
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        if colon:
            headers[name.strip().lower()] = value.strip()

    host = headers.get("host", "")
    hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    if host and hostname.lower() not in _LOOPBACK_HOSTS:
        _http_reply(conn, 403, {"error": "loopback host only"})
        return
    if headers.get(HOOK_HEADER.lower()) != "1":
        _http_reply(conn, 403, {"error": f"missing {HOOK_HEADER} header"})
        return
    route = target.split("?", 1)[0]
    if route not in ("/hook", "/tool"):
        _http_reply(conn, 404, {"error": f"no route {target[:80]}"})
        return
    if method != "POST":
        _http_reply(conn, 405, {"error": "POST only"})
        return
    if "chunked" in headers.get("transfer-encoding", "").lower():
        _http_reply(conn, 501, {"error": "chunked bodies are not supported"})
        return
    try:
        length = int(headers["content-length"])
    except (KeyError, ValueError):
        _http_reply(conn, 411, {"error": "Content-Length required"})
        return
    if length < 0 or length > HTTP_MAX_BODY:
        _http_reply(conn, 413, {"error": "body too large"})
        return
    while len(body) < length:
        chunk = conn.recv(min(65536, length - len(body)))
        if not chunk:
            _http_reply(conn, 400, {"error": "body shorter than Content-Length"})
            return
        body += chunk
    try:
        request = json.loads(body[:length].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        _http_reply(conn, 400, {"error": "body is not JSON"})
        return
    if route == "/tool":
        if not isinstance(request, dict) or "tool" not in request:
            _http_reply(conn, 400, {"error": "body needs tool"})
            return
        _http_reply(conn, 200, _serve_tool_call(request))
        return
    if not isinstance(request, dict) or "hook_event" not in request:
        _http_reply(conn, 400, {"error": "body needs hook_event"})
        return
    _http_reply(conn, 200, _serve_hook_event(request))


_WARM = None  # the warm tools.Warm() instance behind POST /tool, built on first use


def _serve_tool_call(request: dict) -> dict:
    """Answer one plugin tool call (``windvane.tools.run``) in-process:
    ``{"tool", "arguments", "env"}`` in, ``{"text", "isError", "ms"}`` out.
    The session environment applies for the call the way it does for a hook
    event (the draft and the hook state are keyed by the session id); one
    ``tools.Warm()`` instance stays warm across calls, so a call costs the
    work and not the imports."""
    global _WARM
    _env = request.get("env")
    env: dict = _env if isinstance(_env, dict) else {}
    with _HOOK_LOCK:
        from windvane import tools

        old_env = _apply_session_env(env)
        try:
            if _WARM is None:
                _WARM = tools.Warm()
            return tools.run(request, _WARM)
        except Exception as e:
            return {"text": f"tool failed in the daemon: {str(e)[:200]}", "isError": True, "ms": 0}
        finally:
            _restore_env(old_env)


_NO_TIER = b'{"error": "no semantic tier"}\n'


def _handle_client(conn, holder):
    """Handle a single client connection: one request, either protocol (the
    module docstring); the first line decides. ``holder`` is the encoder
    holder, or None when this daemon runs without the semantic tier."""
    try:
        conn.settimeout(5.0)
        data = b""
        while b"\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
            if len(data) > HTTP_MAX_HEAD and not data.lstrip().startswith(b"{"):
                break  # no line end in sight and not a JSON line: malformed

        if not data:
            return

        if _HTTP_REQUEST_LINE.match(data):
            try:
                _serve_http(conn, data)
            except Exception as e:  # a timeout mid-body, a reset: answer if we still can
                try:
                    _http_reply(conn, 400, {"error": f"{type(e).__name__}"})
                except Exception:
                    pass
            return

        request = json.loads(data.decode("utf-8").strip())

        if "hook_event" in request:
            response = json.dumps(_serve_hook_event(request)) + "\n"
            conn.sendall(response.encode("utf-8"))
            return

        # Everything below needs the encoder.
        if holder is None:
            conn.sendall(_NO_TIER)
            return
        from windvane.semantic.encoder import serve_model_request

        conn.sendall(serve_model_request(request, holder))
    except Exception:
        try:
            conn.sendall(json.dumps({"score": 0.0, "text": ""}).encode("utf-8") + b"\n")
        except Exception:
            pass
    finally:
        conn.close()


def serve():
    """Run the daemon. Blocks until idle timeout or a source change.

    Binds and announces itself BEFORE loading any model: hook events are
    served from the first millisecond, while embedding/scoring requests wait
    on the background model load (and degrade to their callers' fallbacks
    until it finishes). Without the semantic tier no model is ever loaded."""
    # Single instance: the process lock, not the port file. Whoever holds it
    # is the daemon; a spawn that finds it held has nothing to add, whatever
    # the files say and whether or not the holder answers a connect right now.
    from windvane import proc_lock

    lock = proc_lock.acquire(LOCK_FILE)
    if lock is None:
        print("Another daemon holds the lock - exiting.", file=sys.stderr)
        return
    global _SERVING
    _SERVING = True

    semantic_on = _semantic_on()
    sig = _model_signature()

    # Bind to any available port on localhost
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(("127.0.0.1", 0))
    port = server_sock.getsockname()[1]
    server_sock.listen(LISTEN_BACKLOG)

    # Write model signature, PID and port so hooks can find and validate us.
    # The port file goes last: a client that reads it finds the rest in place.
    PORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    MODEL_FILE.write_text(sig)
    PID_FILE.write_text(str(os.getpid()))
    PORT_FILE.write_text(str(port))
    # Clear the spawn marker: we're up, spawns may flow again
    STARTING_FILE.unlink(missing_ok=True)

    holder = None
    if semantic_on:
        from windvane.semantic.encoder import _ModelHolder

        holder = _ModelHolder(device_file=DEVICE_FILE)
        print(f"Loading embedding model {sig} in background...", file=sys.stderr)
        threading.Thread(target=holder.load, daemon=True).start()
    else:
        print("No semantic tier: serving hooks and tools only.", file=sys.stderr)

    def _warm_imports():
        # Pre-import the hook dispatcher and the tools so the first request
        # doesn't pay (or race) the import under the dispatch lock.
        try:
            with _HOOK_LOCK:
                from windvane import events  # noqa: F401
                from windvane import tools  # noqa: F401
        except Exception:
            pass

    threading.Thread(target=_warm_imports, daemon=True).start()

    print(
        f"windvane daemon listening on 127.0.0.1:{port} (PID {os.getpid()})",
        file=sys.stderr,
    )

    last_activity = time.time()

    # Handle SIGTERM gracefully
    def _shutdown(signum, frame):
        server_sock.close()
        _cleanup()
        sys.exit(0)

    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except (OSError, ValueError):
        pass  # Windows doesn't support SIGTERM in all contexts

    # The daemon serves code it imported at start. After an edit to the
    # package it would keep serving the old code until its idle timeout, so
    # it watches the package's newest source mtime and exits when that moves;
    # the next hook call falls back in-process and respawns it.
    code_stamp = _code_stamp()
    last_code_check = time.time()

    try:
        while True:
            server_sock.settimeout(60.0)  # Check idle every 60s
            try:
                conn, addr = server_sock.accept()
                last_activity = time.time()
                # Handle in thread for concurrency
                t = threading.Thread(
                    target=_handle_client,
                    args=(conn, holder),
                    daemon=True,
                )
                t.start()
            except socket.timeout:
                if time.time() - last_activity > IDLE_TIMEOUT:
                    print("Idle timeout - shutting down.", file=sys.stderr)
                    break
            if time.time() - last_code_check >= CODE_CHECK_SECS:
                last_code_check = time.time()
                if _code_stamp() != code_stamp:
                    print("Package source changed - exiting so the next hook restarts on the new code.", file=sys.stderr)
                    break
    finally:
        server_sock.close()
        _cleanup()
        lock.release()


CODE_CHECK_SECS = 10.0


def _code_stamp() -> float:
    """Newest mtime across the package's Python sources (one directory walk,
    ~100 files, a few milliseconds)."""
    root = Path(__file__).resolve().parent
    newest = 0.0
    try:
        for p in root.rglob("*.py"):
            try:
                newest = max(newest, p.stat().st_mtime)
            except OSError:
                continue
    except Exception:
        return newest
    return newest


def _cleanup():
    """Remove this daemon's port/pid/model/device files on shutdown. Files
    that name another pid belong to a daemon that took over; an exit that
    deleted them left that daemon unreachable and the next hook spawned a
    third."""
    try:
        owner = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        owner = os.getpid()
    if owner != os.getpid():
        return
    _remove_files()


def _remove_files():
    for f in (PORT_FILE, PID_FILE, MODEL_FILE, DEVICE_FILE):
        try:
            f.unlink(missing_ok=True)
        except Exception:
            pass


def _daemon_alive() -> bool:
    """Is a daemon process alive? The process lock is the only evidence."""
    from windvane import proc_lock

    return proc_lock.held(LOCK_FILE)


def _server_model_matches() -> bool:
    """Does the running daemon's encoder match the configured one? A daemon
    started without the semantic tier says "none"; one whose tier differs
    from the current configuration (the tier switched on or off, the model
    changed) is replaced. A missing MODEL_FILE is a daemon between taking
    its lock and announcing itself, which is not a mismatch."""
    if not MODEL_FILE.exists():
        return True
    try:
        return MODEL_FILE.read_text().strip() == _model_signature()
    except Exception:
        return False


def _stop_running_server():
    """Terminate a running daemon (used when the model config changed).
    Best-effort: on failure the stale files are removed so a new daemon starts
    and the old one dies at its idle timeout."""
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.2)
    except Exception:
        pass
    _remove_files()


def is_server_running() -> bool:
    """Is a daemon alive WITH the configured encoder? Decided by the process
    lock: a live daemon that does not answer a connect this instant (binding,
    stalled, a full backlog) is still the daemon, and its files stay. Files
    with no holder behind them are stale and removed. A daemon loaded with a
    different model is replaced -- using it would mix vector spaces. Inside
    the daemon itself (a hook it serves asked) the answer is True without a
    probe: replacing it would kill the caller mid-request."""
    if _SERVING:
        return True
    if not _daemon_alive():
        if PORT_FILE.exists() or PID_FILE.exists():
            _remove_files()
        return False
    if not _server_model_matches():
        _stop_running_server()
        return False
    return True


def daemon_port() -> "int | None":
    """The running daemon's port, or None when no daemon holds the lock."""
    if not is_server_running():
        return None
    try:
        return int(PORT_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def start_server_background():
    """
    Start the daemon as a detached background process.

    Fire-and-forget: spawns the process and returns immediately.
    Does NOT wait for the daemon to be ready -- the first hook call
    that tries it will either connect (ready) or fall back in-process
    (still starting). This avoids blocking the SessionStart hook
    (2s timeout budget).
    """
    if is_server_running():
        return True
    if os.environ.get("WINDVANE_NO_DAEMON", "").strip():
        # Benches and one-off hook runs against a temporary store: a daemon
        # spawned from there inherits WINDVANE_DIR, outlives the temp dir and
        # idles for 30 minutes.
        return False

    import subprocess
    import platform

    # One spawn per 30 s: a burst of hooks that all found no daemon would
    # each start one; the extras exit on the lock, but every one of them is
    # an interpreter start on a machine that is already short of breath. The
    # daemon clears the marker when it binds (same marker as daemon_client).
    marker = STARTING_FILE
    try:
        if marker.exists() and time.time() - marker.stat().st_mtime < 30:
            return False
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(time.time()))
    except OSError:
        pass

    try:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "cwd": str(ENGINE_DIR),
        }
        if platform.system() == "Windows":
            # CREATE_NO_WINDOW prevents any console window from appearing
            CREATE_NO_WINDOW = 0x08000000
            kwargs["creationflags"] = CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True

        subprocess.Popen(
            [sys.executable, "-m", "windvane.daemon"],
            **kwargs,
        )
        return True  # Fire-and-forget -- don't wait
    except Exception:
        return False


def score_via_server(text: str) -> tuple[float, str]:
    """
    Score text by connecting to the daemon.
    Auto-starts the daemon if not running. Returns (score, extracted_text),
    or (0.0, "") when unavailable or when the semantic tier is off.
    """
    if not _semantic_on() or not _ensure_server():
        return 0.0, ""

    try:
        port = int(PORT_FILE.read_text().strip())
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect(("127.0.0.1", port))

        request = json.dumps({"text": text}) + "\n"
        sock.sendall(request.encode("utf-8"))

        data = b""
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk

        sock.close()
        result = json.loads(data.decode("utf-8").strip())
        return result.get("score", 0.0), result.get("text", "")
    except Exception:
        return 0.0, ""


_auto_start_attempted = False


def _ensure_server() -> bool:
    """Auto-start the daemon if not running. Returns True if it is available."""
    global _auto_start_attempted
    if is_server_running():  # a live holder; replaces a model mismatch itself
        _auto_start_attempted = False
        return True
    if _auto_start_attempted:
        return False
    _auto_start_attempted = True
    if not start_server_background():
        return False  # another process spawned one within the last 30 s
    for _ in range(20):
        if PORT_FILE.exists():
            return True
        time.sleep(0.5)
    return False


def embed_via_server(text: str) -> list[float]:
    """
    Get the embedding vector for text from the daemon.
    Auto-starts the daemon if not running. Returns a model-dim list, or an
    empty list when unavailable or when the semantic tier is off.
    """
    if not _semantic_on() or not _ensure_server():
        return []

    try:
        port = int(PORT_FILE.read_text().strip())
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(("127.0.0.1", port))

        request = json.dumps({"embed": text}) + "\n"
        sock.sendall(request.encode("utf-8"))

        data = b""
        while b"\n" not in data:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data += chunk

        sock.close()
        result = json.loads(data.decode("utf-8").strip())
        return result.get("embedding", [])
    except Exception:
        return []


def embed_batch_via_server(texts: list[str]) -> list[list[float]]:
    """
    Get embeddings for multiple texts in a single TCP call.

    The daemon encodes all texts in one model.encode() call, which is much
    faster than individual calls (1 roundtrip vs N roundtrips). Returns a
    list of model-dim vectors; empty entries for failures, and all empty
    when the semantic tier is off.
    """
    if not texts:
        return []
    if not _semantic_on() or not _ensure_server():
        return [[] for _ in texts]

    try:
        port = int(PORT_FILE.read_text().strip())
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(30.0)  # Batch can take longer
        sock.connect(("127.0.0.1", port))

        request = json.dumps({"embed_batch": [t[:500] for t in texts]}) + "\n"
        sock.sendall(request.encode("utf-8"))

        # Batch responses can be large (~3KB * N texts)
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk

        sock.close()
        result = json.loads(data.decode("utf-8").strip())
        return result.get("embeddings", [[] for _ in texts])
    except Exception:
        return [[] for _ in texts]


if __name__ == "__main__":
    serve()

"""The daemon (windvane.daemon) and the encoder discipline (windvane.semantic).

Every test runs against a temporary store with WINDVANE_NO_DAEMON set, so no
test ever starts a daemon on the real store; the one end-to-end test starts a
daemon on its own temporary store and kills it.
"""

import importlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1] / "engine"
CLIENT_TIMEOUT = 30


def _has(module: str, attr: str = "") -> bool:
    try:
        mod = importlib.import_module(module)
    except Exception:
        return False
    return not attr or hasattr(mod, attr)


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    monkeypatch.delenv("WINDVANE_SEMANTIC", raising=False)
    (tmp_path / "store").mkdir()
    yield


@pytest.fixture
def ss(tmp_path: Path):
    """windvane.daemon reloaded so its file constants name the temp store;
    reloaded again afterwards with the environment the test leaves."""
    from windvane import daemon

    mod = importlib.reload(daemon)
    assert mod.PORT_FILE.parent == tmp_path / "store"
    yield mod
    importlib.reload(daemon)


class _NoModel:
    def wait(self, timeout: float = 0.0) -> bool:
        return False


def _listener(ss, holder):
    """A loopback listener whose every connection goes to _handle_client, as
    serve() hands them over."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.2)
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            threading.Thread(target=ss._handle_client, args=(conn, holder), daemon=True).start()

    t = threading.Thread(target=loop, daemon=True)
    t.start()

    def close():
        stop.set()
        t.join(2)
        srv.close()

    return srv.getsockname()[1], close


@pytest.fixture
def listener(ss):
    port, close = _listener(ss, _NoModel())
    yield port
    close()


@pytest.fixture
def tierless_listener(ss):
    port, close = _listener(ss, None)
    yield port
    close()


def _raw(port: int, data: bytes) -> bytes:
    # Generous: the handlers answer in milliseconds, but a full suite on a
    # loaded machine (antivirus, parallel sessions) can stall a thread for seconds.
    with socket.create_connection(("127.0.0.1", port), timeout=CLIENT_TIMEOUT) as s:
        s.sendall(data)
        out = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return out
            out += chunk


def _post(port: int, body: dict, headers: "dict | None" = None, path: str = "/hook", method: str = "POST"):
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=CLIENT_TIMEOUT)
    hdrs = {"Content-Type": "application/json", "X-Windvane-Hook": "1"}
    hdrs.update(headers or {})
    conn.request(method, path, body=json.dumps(body), headers=hdrs)
    res = conn.getresponse()
    out = (res.status, res.getheader("Content-Type"), res.getheader("Connection"), json.loads(res.read() or b"{}"))
    conn.close()
    return out


# ── the two protocols on one listener ────────────────────────────────────


def test_both_protocols_share_one_listener(ss, listener, monkeypatch):
    seen = []

    def fake_serve(request):
        seen.append(request)
        return {"output": f"served {request['hook_event']}"}

    monkeypatch.setattr(ss, "_serve_hook_event", fake_serve)
    port = listener
    line = _raw(port, json.dumps({"hook_event": "prompt_json", "stdin": "{}", "env": {}}).encode() + b"\n")
    assert json.loads(line) == {"output": "served prompt_json"}

    status, ctype, conn_hdr, body = _post(port, {"hook_event": "stop_json", "stdin": '{"session_id": "s1"}', "env": {"CLAUDE_PROJECT_DIR": "/demo"}})
    assert (status, ctype, conn_hdr) == (200, "application/json", "close")
    assert body == {"output": "served stop_json"}
    assert seen[1] == {"hook_event": "stop_json", "stdin": '{"session_id": "s1"}', "env": {"CLAUDE_PROJECT_DIR": "/demo"}}

    # The scorer protocol is untouched: a still-loading model degrades as before.
    assert json.loads(_raw(port, b'{"text": "let us use redis for the cache"}\n')) == {"error": "model unavailable"}


def test_without_the_semantic_tier_every_model_request_says_so(ss, tierless_listener):
    port = tierless_listener
    for req in (b'{"text": "let us use redis for the cache"}\n', b'{"embed": "x"}\n', b'{"embed_batch": ["x"]}\n'):
        assert json.loads(_raw(port, req)) == {"error": "no semantic tier"}


def test_http_refuses_what_is_not_a_hook_post(ss, listener, monkeypatch):
    monkeypatch.setattr(ss, "_serve_hook_event", lambda request: {"output": "ran"})
    port = listener
    ok = {"hook_event": "prompt_json", "stdin": "{}"}
    assert _post(port, ok, path="/nope")[0] == 404
    assert _post(port, ok, method="PUT")[0] == 405
    assert _post(port, ok, headers={"X-Windvane-Hook": "0"})[0] == 403  # a browser cannot add the header cross-origin
    assert _post(port, ok, headers={"Host": "evil.example:80"})[0] == 403  # DNS rebinding
    assert _post(port, {"stdin": "{}"})[0] == 400  # no hook_event
    assert _post(port, ok, headers={"Host": f"localhost:{port}"})[0] == 200

    def raw_status(data: bytes) -> int:
        return int(_raw(port, data).split(b" ", 2)[1])

    head = b"POST /hook HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Windvane-Hook: 1\r\n"
    assert raw_status(head + b"\r\n") == 411  # no Content-Length
    assert raw_status(head + b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n") == 501
    assert raw_status(head + b"Content-Length: 9\r\n\r\nnot json!") == 400
    assert raw_status(head + b"Content-Length: 99999999999\r\n\r\n") == 413
    # The old header name is not this daemon's.
    old = b"POST /hook HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Engram-Hook: 1\r\nContent-Length: 2\r\n\r\n{}"
    assert raw_status(old) == 403
    # A first line that is neither JSON nor a request line gets the old
    # JSON-path answer, never a hook run.
    assert json.loads(_raw(port, b"hello\n")) == {"score": 0.0, "text": ""}


# ── POST /hook: the seam on windvane.hooks.dispatch ──────────────────────


def test_a_hook_post_applies_the_session_env_and_restores_it(ss, listener, monkeypatch):
    """Mocked seam: the dispatch sees the request's env and stdin; the
    daemon's own values come back after the call."""
    from windvane import hooks

    seen = {}

    def fake_dispatch(event, stdin_text):
        seen.update({k: os.environ.get(k) for k in ("CLAUDE_PROJECT_DIR", "WINDVANE_AUTONOMY", "CLAUDE_CODE_AUTO_COMPACT_WINDOW")})
        seen["event"], seen["stdin"] = event, stdin_text
        print("printed", end="")
        return "returned\n"

    monkeypatch.setattr(hooks, "dispatch", fake_dispatch, raising=False)
    monkeypatch.setenv("WINDVANE_AUTONOMY", "daemon-own")
    port = listener
    status, _, _, body = _post(port, {"hook_event": "stop_json", "stdin": "{}", "env": {
        "CLAUDE_PROJECT_DIR": "/demo", "WINDVANE_AUTONOMY": "", "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000"}})
    assert status == 200 and body == {"output": "returned\nprinted"}
    assert seen == {"CLAUDE_PROJECT_DIR": "/demo", "WINDVANE_AUTONOMY": None, "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000",
                    "event": "stop_json", "stdin": "{}"}
    assert os.environ["WINDVANE_AUTONOMY"] == "daemon-own"  # restored after the call
    # An older client sends CLAUDE_PROJECT_DIR alone: the daemon's other values stand.
    _post(port, {"hook_event": "stop_json", "stdin": "{}", "env": {"CLAUDE_PROJECT_DIR": "/demo"}})
    assert seen["WINDVANE_AUTONOMY"] == "daemon-own"
    # CLAUDE_PROJECT_DIR is always unset when the request leaves it out.
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/daemon-cwd")
    _post(port, {"hook_event": "stop_json", "stdin": "{}", "env": {}})
    assert seen["CLAUDE_PROJECT_DIR"] is None and os.environ["CLAUDE_PROJECT_DIR"] == "/daemon-cwd"

    # A SystemExit inside a handler is a finished hook, not a dead daemon.
    def exiting(event, stdin_text):
        print("before exit", end="")
        raise SystemExit(0)

    monkeypatch.setattr(hooks, "dispatch", exiting, raising=False)
    assert _post(port, {"hook_event": "bash_json", "stdin": "{}"})[3] == {"output": "before exit"}
    # An exception is an error answer.
    monkeypatch.setattr(hooks, "dispatch", lambda e, s: 1 / 0, raising=False)
    assert "division by zero" in _post(port, {"hook_event": "bash_json", "stdin": "{}"})[3]["error"]


def test_without_dispatch_the_argv_style_main_runs_with_common_set(ss, listener, monkeypatch):
    """Mocked seam: a hooks package that has main() but no dispatch() is
    driven the way engram's daemon drove remind.main(): stdin cached in
    hooks.common, the event in sys.argv, both reset afterwards."""
    import types

    import windvane

    seen = {}
    common = types.ModuleType("windvane.hooks.common")
    common._stdin_cache, common._session_id = None, "stale"
    hooks = types.ModuleType("windvane.hooks")
    hooks.common = common

    def main():
        seen.update(argv=list(sys.argv), stdin=common._stdin_cache, sid=common._session_id)
        print("from main")

    hooks.main = main
    monkeypatch.setitem(sys.modules, "windvane.hooks", hooks)
    monkeypatch.setitem(sys.modules, "windvane.hooks.common", common)
    monkeypatch.setattr(windvane, "hooks", hooks, raising=False)
    argv = list(sys.argv)
    status, _, _, body = _post(listener, {"hook_event": "prompt_json", "stdin": '{"prompt": "x"}'})
    assert status == 200 and body == {"output": "from main\n"}
    assert seen == {"argv": ["windvane.hooks", "prompt_json"], "stdin": '{"prompt": "x"}', "sid": ""}
    assert common._stdin_cache is None and sys.argv == argv


def test_session_end_is_not_served_in_process(ss, listener):
    status, _, _, body = _post(listener, {"hook_event": "session_end_json", "stdin": "{}"})
    assert status == 200 and "unsupported hook_event" in body["error"]


@pytest.mark.skipif(not _has("windvane.hooks", "dispatch"), reason="windvane.hooks.dispatch (port-hooks) not in the tree yet")
def test_a_hook_post_runs_the_real_dispatch(ss, listener, tmp_path: Path, monkeypatch):
    """Unmocked: POST /hook reaches windvane.hooks.dispatch through
    _serve_hook_event; a pre_tool event with nothing halted is a silent pass."""
    monkeypatch.setenv("WINDVANE_AUTONOMY", "daemon-own")
    stdin = json.dumps({"session_id": "bridge-s1", "hook_event_name": "PreToolUse", "tool_name": "Bash",
                        "tool_input": {"command": "ls"}, "tool_use_id": "t1", "cwd": str(tmp_path)})
    status, _, _, body = _post(listener, {"hook_event": "pre_tool_json", "stdin": stdin, "env": {"CLAUDE_PROJECT_DIR": str(tmp_path)}})
    assert status == 200 and "output" in body, body
    assert body["output"] == ""
    assert os.environ["WINDVANE_AUTONOMY"] == "daemon-own"


def test_every_served_event_is_one_the_dispatch_knows(ss):
    lifecycle = {"session_start_json", "stop_json", "pre_compact_json", "post_compact_json",
                 "stop_failure_json", "notification_json", "post_milestone_json"}
    assert lifecycle <= ss._HOOK_EVENTS
    assert "session_end_json" not in ss._HOOK_EVENTS
    hooks = importlib.import_module("windvane.hooks")
    known = getattr(hooks, "EVENTS", None)
    if known is None:
        pytest.skip("windvane.hooks exposes no EVENTS table yet (port-hooks)")
    assert ss._HOOK_EVENTS <= set(known)


# ── POST /tool: the seam on windvane.tools ───────────────────────────────


def test_post_tool_keeps_one_warm_instance_and_restores_the_env(ss, listener, monkeypatch):
    """Mocked seam: tools.run gets the request and the one tools.Warm()."""
    import types

    built = []

    class Warm:
        def __init__(self):
            built.append(self)

    def run(request, warm=None):
        if request["tool"] == "boom":
            raise RuntimeError("kaput")
        return {"text": f"{request['tool']} via {id(warm)} in {os.environ.get('CLAUDE_PROJECT_DIR')}", "isError": False, "ms": 1}

    fake = types.ModuleType("windvane.tools")
    fake.Warm, fake.run = Warm, run
    monkeypatch.setitem(sys.modules, "windvane.tools", fake)
    import windvane

    monkeypatch.setattr(windvane, "tools", fake, raising=False)
    monkeypatch.setattr(ss, "_WARM", None)
    port = listener
    status, _, _, body = _post(port, {"tool": "memory", "arguments": {}, "env": {"CLAUDE_PROJECT_DIR": "/p"}}, path="/tool")
    assert status == 200 and body["text"].startswith("memory via ") and body["text"].endswith(" in /p")
    status, _, _, body2 = _post(port, {"tool": "checkpoint", "arguments": {}, "env": {}}, path="/tool")
    assert len(built) == 1 and body2["text"].split(" via ")[1].split(" ")[0] == body["text"].split(" via ")[1].split(" ")[0]
    status, _, _, body = _post(port, {"tool": "boom", "arguments": {}}, path="/tool")
    assert status == 200 and body["isError"] is True and "kaput" in body["text"]
    assert _post(port, {"arguments": {}}, path="/tool")[0] == 400
    assert os.environ.get("CLAUDE_PROJECT_DIR") != "/p"
    monkeypatch.setattr(ss, "_WARM", None)


@pytest.mark.skipif(not (_has("windvane.tools", "run") and _has("windvane.tools", "Warm")), reason="windvane.tools not importable yet (port-store's module, which needs port-hooks' modules)")
def test_post_tool_runs_the_real_tools(ss, listener, tmp_path: Path, monkeypatch):
    """Unmocked: POST /tool reaches windvane.tools.run with one warm instance."""
    monkeypatch.setattr(ss, "_WARM", None)
    proj = tmp_path / "proj"
    proj.mkdir()
    port = listener
    status, _, _, body = _post(port, {"tool": "memory", "arguments": {"operation": "remember", "content": "The daemon serves tools.", "project_path": str(proj)}, "env": {}}, path="/tool")
    assert status == 200 and body["isError"] is False, body
    first = ss._WARM
    assert first is not None
    status, _, _, body = _post(port, {"tool": "nope", "arguments": {}, "env": {}}, path="/tool")
    assert status == 200 and body["isError"] is True
    assert ss._WARM is first
    monkeypatch.setattr(ss, "_WARM", None)


# ── the process lock and the files ───────────────────────────────────────


def test_inside_the_daemon_a_handler_never_probes_or_replaces_it(ss, monkeypatch):
    monkeypatch.setattr(ss, "_SERVING", True)
    monkeypatch.setattr(ss, "_daemon_alive", lambda: (_ for _ in ()).throw(AssertionError("probed")))
    assert ss.is_server_running() is True
    assert ss.start_server_background() is True


def test_the_files_are_named_for_windvane(ss, tmp_path: Path):
    store = tmp_path / "store"
    assert (ss.PORT_FILE, ss.LOCK_FILE, ss.PID_FILE, ss.MODEL_FILE) == (
        store / "daemon_port", store / "daemon.lock", store / "daemon_pid", store / "daemon_model")


@pytest.mark.skipif(not _has("windvane.proc_lock", "acquire"), reason="windvane.proc_lock (port-hooks) not in the tree yet")
def test_a_live_daemon_is_never_unregistered_by_a_failed_connect(ss):
    """The daemon holds a process lock for its lifetime. While it is held,
    a connect that fails (a stall, a full backlog) is not a dead daemon:
    the files stay and no second daemon is spawned."""
    from windvane import proc_lock

    daemon = proc_lock.acquire(ss.LOCK_FILE)
    ss.PORT_FILE.write_text("1")  # nobody listens on port 1
    ss.PID_FILE.write_text(str(os.getpid()))
    ss.MODEL_FILE.write_text(ss._model_signature())
    assert ss.is_server_running() is True
    assert ss.PORT_FILE.exists() and ss.PID_FILE.exists()
    daemon.release()
    assert ss.is_server_running() is False  # no holder: the files were stale
    assert not ss.PORT_FILE.exists() and not ss.PID_FILE.exists()


def test_a_daemons_exit_leaves_another_daemons_files_alone(ss):
    ss.PORT_FILE.write_text("5000")
    ss.PID_FILE.write_text(str(os.getpid() + 1))
    ss._cleanup()
    assert ss.PORT_FILE.exists(), "another pid owns the files"
    ss.PID_FILE.write_text(str(os.getpid()))
    ss._cleanup()
    assert not ss.PORT_FILE.exists() and not ss.PID_FILE.exists()


def test_the_model_stamp_follows_the_semantic_tier(ss, monkeypatch):
    monkeypatch.setattr(ss, "_semantic_on", lambda: False)
    assert ss._model_signature() == "none"
    assert ss._server_model_matches() is True  # no stamp yet: a daemon still announcing itself
    ss.MODEL_FILE.write_text("none")
    assert ss._server_model_matches() is True
    monkeypatch.setattr(ss, "_semantic_on", lambda: True)
    from windvane.semantic.config import embed_signature

    assert ss._model_signature() == embed_signature()
    assert ss._server_model_matches() is False  # the tier was switched on: replace the daemon
    ss.MODEL_FILE.write_text(embed_signature())
    assert ss._server_model_matches() is True


def test_no_daemon_env_stops_every_spawn(ss, monkeypatch):
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(ss, "is_server_running", lambda: False)
    assert ss.start_server_background() is False
    assert spawned == []


def test_a_spawn_runs_the_engine_from_its_own_folder(ss, monkeypatch):
    monkeypatch.delenv("WINDVANE_NO_DAEMON", raising=False)
    monkeypatch.setattr(ss, "is_server_running", lambda: False)
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)))
    assert ss.start_server_background() is True
    cmd, kw = calls[-1]
    assert cmd[1:] == ["-m", "windvane.daemon"] and Path(kw["cwd"]) == ENGINE
    assert ss.STARTING_FILE.exists()
    assert ss.start_server_background() is False  # one spawn per 30 s
    assert len(calls) == 1


# ── the semantic tier ────────────────────────────────────────────────────


def test_without_the_tier_the_clients_answer_empty_without_a_round_trip(ss, monkeypatch):
    monkeypatch.setattr(ss, "_semantic_on", lambda: False)
    monkeypatch.setattr(ss, "_ensure_server", lambda: (_ for _ in ()).throw(AssertionError("connected")))
    assert ss.score_via_server("let's use postgres instead of sqlite") == (0.0, "")
    assert ss.embed_via_server("x") == []
    assert ss.embed_batch_via_server(["a", "b"]) == [[], []]
    assert ss.embed_batch_via_server([]) == []


def test_the_tier_is_requested_by_config_or_env(monkeypatch):
    from windvane import semantic

    monkeypatch.setattr("windvane.config.plugin_config", lambda: {})
    assert semantic.requested() is False
    monkeypatch.setenv("WINDVANE_SEMANTIC", "1")
    assert semantic.requested() is True
    monkeypatch.delenv("WINDVANE_SEMANTIC")
    for value, want in ((True, True), ("true", True), ("false", False), (False, False), ("0", False)):
        monkeypatch.setattr("windvane.config.plugin_config", lambda v=value: {"semantic": v})
        assert semantic.requested() is want, value
    monkeypatch.setattr(semantic, "available", lambda: False)
    assert semantic.enabled() is False  # requested but not installed


def test_importing_the_semantic_package_imports_no_heavy_module():
    code = ("import sys, windvane.semantic, windvane.semantic.config, windvane.semantic.worker, windvane.daemon; "
            "print([m for m in ('numpy', 'torch', 'sentence_transformers') if m in sys.modules])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": str(ENGINE)}, stdin=subprocess.DEVNULL, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_the_daemons_cpu_batch_is_small_and_overridable(monkeypatch):
    """The resident daemon keeps the activation arena of its largest batch
    for life; 64 rows parked 1.2 GB more than 16 at the same speed."""
    from windvane.semantic import worker as ew

    monkeypatch.delenv("WINDVANE_CPU_BATCH", raising=False)
    assert ew.cpu_batch_size() == ew.CPU_BATCH_DEFAULT == 16
    monkeypatch.setenv("WINDVANE_CPU_BATCH", "8")
    assert ew.cpu_batch_size() == 8
    monkeypatch.setenv("WINDVANE_CPU_BATCH", "junk")
    assert ew.cpu_batch_size() == 16
    monkeypatch.setenv("WINDVANE_GPU_BULK_MIN", "100")
    assert ew.bulk_threshold() == 100
    assert ew.embed_texts_bulk([]) == []


def test_the_resident_device_is_cpu_unless_forced(monkeypatch):
    from windvane.semantic import config as ec

    monkeypatch.delenv("WINDVANE_DEVICE", raising=False)
    assert ec.resolve_device() == "cpu"
    monkeypatch.setenv("WINDVANE_DEVICE", "CUDA:1")
    assert ec.resolve_device() == ec.resolve_bulk_device() == "cuda:1"


def test_the_embedding_signature_reads_env_then_store_config(tmp_path: Path, monkeypatch):
    from windvane.semantic import config as ec

    monkeypatch.delenv("WINDVANE_EMBED_MODEL", raising=False)
    monkeypatch.delenv("WINDVANE_EMBED_DIM", raising=False)
    assert ec.embed_signature() == f"{ec.DEFAULT_MODEL}@native"
    (tmp_path / "store" / "config.json").write_text(json.dumps({"embed_model": "all-MiniLM-L6-v2"}), encoding="utf-8")
    assert ec.embed_signature() == "all-MiniLM-L6-v2@native" == ec.LEGACY_SIGNATURE
    monkeypatch.setenv("WINDVANE_EMBED_MODEL", "m")
    monkeypatch.setenv("WINDVANE_EMBED_DIM", "256")
    assert ec.embed_signature() == "m@256"


# ── model calls stay pinned to one thread (the ~0.73 MB/request leak) ────


def test_model_work_runs_on_one_pinned_thread_never_the_callers():
    from windvane.semantic.encoder import _on_model_thread

    caller = threading.get_ident()
    seen = [_on_model_thread(threading.get_ident) for _ in range(50)]
    assert caller not in seen and len(set(seen)) == 1
    from_threads = []
    lock = threading.Lock()

    def connection():
        tid = _on_model_thread(threading.get_ident)
        with lock:
            from_threads.append(tid)

    workers = [threading.Thread(target=connection) for _ in range(40)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    assert set(from_threads) == set(seen)


def test_the_pool_is_transparent_to_callers():
    from windvane.semantic.encoder import _on_model_thread

    assert _on_model_thread(lambda a, b=1: a + b, 41) == 42
    assert _on_model_thread(lambda a, b=1: a + b, 40, b=2) == 42

    def boom():
        raise ValueError("propagated")

    with pytest.raises(ValueError, match="propagated"):
        _on_model_thread(boom)


def test_no_model_call_bypasses_the_pool():
    """Source guard: the leak comes back the moment one call site slips."""
    from windvane import daemon
    from windvane.semantic import encoder

    src = Path(encoder.__file__).read_text(encoding="utf-8")
    body = src[src.index("def serve_model_request"):]
    assert not re.findall(r"^\s*[^#\n]*\bmodel\.encode\(", body, re.M)
    assert body.count("_on_model_thread(") >= 3
    assert not re.findall(r"^\s*(?:score, extracted = )?_score_text\(", body, re.M)
    dsrc = Path(daemon.__file__).read_text(encoding="utf-8")
    handler = dsrc[dsrc.index("def _handle_client"):dsrc.index("def serve(")]
    assert "model.encode(" not in handler and "_score_text(" not in handler


def test_a_model_request_is_answered_on_the_pinned_thread():
    from windvane.semantic import encoder

    threads = []

    class Arr(list):
        def tolist(self):
            return list(self)

    class Model:
        device = "cpu"

        def encode(self, texts, normalize_embeddings=True, batch_size=32):
            threads.append(threading.get_ident())
            return Arr(Arr([float(len(t)), 1.0]) for t in texts)

    class Holder:
        model, decision_embs, non_decision_embs = Model(), None, None

        def wait(self, timeout=20.0):
            return True

    out = json.loads(encoder.serve_model_request({"embed": "x" * 5000}, Holder()))
    assert out == {"embedding": [2000.0, 1.0]}  # capped at MAX_ENCODE_CHARS
    out = json.loads(encoder.serve_model_request({"embed_batch": ["ab", "abc"]}, Holder()))
    assert out == {"embeddings": [[2.0, 1.0], [3.0, 1.0]]}
    assert json.loads(encoder.serve_model_request({"embed_batch": []}, Holder())) == {"embeddings": []}
    assert threading.get_ident() not in threads and len(set(threads)) == 1


# ── end to end: a real daemon on a temporary store ───────────────────────


@pytest.mark.skipif(not _has("windvane.proc_lock", "acquire"), reason="windvane.proc_lock (port-hooks) not in the tree yet")
def test_a_real_daemon_binds_announces_and_answers(tmp_path: Path):
    store = tmp_path / "store"
    env = {**os.environ, "WINDVANE_DIR": str(store), "WINDVANE_NO_DAEMON": "1", "PYTHONPATH": str(ENGINE)}
    env.pop("WINDVANE_SEMANTIC", None)
    err_log = tmp_path / "daemon.err"
    err = open(err_log, "wb")  # a file, not a pipe: an undrained pipe can block the daemon
    proc = subprocess.Popen([sys.executable, "-m", "windvane.daemon"], cwd=str(ENGINE), env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err)
    try:
        port_file = store / "daemon_port"
        deadline = time.time() + 30
        while not port_file.exists() and time.time() < deadline and proc.poll() is None:
            time.sleep(0.05)
        assert port_file.exists(), err_log.read_text(errors="replace")
        port = int(port_file.read_text())
        assert (store / "daemon_model").read_text() == "none"
        daemon_pid = int((store / "daemon_pid").read_text())
        # A venv's python.exe on Windows is a launcher stub: the daemon is its child.
        try:
            import psutil

            family = {proc.pid} | {c.pid for c in psutil.Process(proc.pid).children(recursive=True)}
            assert daemon_pid in family
        except ImportError:
            assert daemon_pid > 0
        assert json.loads(_raw(port, b'{"embed": "x"}\n')) == {"error": "no semantic tier"}
        assert "unsupported" in json.loads(_raw(port, b'{"hook_event": "nope", "stdin": ""}\n'))["error"]
        assert _post(port, {"hook_event": "session_end_json", "stdin": "{}"})[0] == 200
        # A second daemon on the same store finds the lock held and leaves.
        second = subprocess.run([sys.executable, "-m", "windvane.daemon"], cwd=str(ENGINE), env=env,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        assert "holds the lock" in second.stderr
        assert int(port_file.read_text()) == port
    finally:
        _stop_started(proc)
        err.close()


def _stop_started(proc) -> None:
    """Stop a process this test started and everything it started (a venv's
    python.exe on Windows is a launcher whose child is the daemon), and wait
    until each has exited, so no daemon or lock outlives the test. Only these
    pids are touched."""
    try:
        import psutil
    except ImportError:
        proc.kill()
        proc.wait(30)
        return
    try:
        family = [psutil.Process(proc.pid)]
        family += family[0].children(recursive=True)
    except psutil.Error:
        family = []
    for p in reversed(family):  # the daemon first, the launcher last
        try:
            p.kill()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(family, timeout=30)
    assert not alive, [p.pid for p in alive]
    proc.wait(30)

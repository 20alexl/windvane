"""The thin hook client (windvane/daemon_client.py): stdlib only, one
round trip to the daemon, an in-process fallback, a guarded spawn."""

import ast
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "windvane" / "daemon_client.py"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    (tmp_path / "store").mkdir()


@pytest.fixture
def client():
    """The client loaded by path, the way Claude Code runs it."""
    spec = importlib.util.spec_from_file_location("wv_daemon_client", CLIENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_client_imports_only_the_standard_library():
    tree = ast.parse(CLIENT.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    names.discard("windvane")  # the fallback, after the engine dir is on sys.path
    assert names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names)
    # ...and the windvane import sits only inside the fallback function.
    fallback = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_run_fallback")
    top_level = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not any(isinstance(n, ast.ImportFrom) and (n.module or "").startswith("windvane") for n in top_level)
    assert any(isinstance(n, ast.ImportFrom) and n.module == "windvane.events" for n in ast.walk(fallback))


def test_the_session_env_and_events_match_the_daemon(client):
    from windvane import daemon

    assert client.SESSION_ENV == daemon.SESSION_ENV
    assert client.DAEMON_EVENTS <= daemon._HOOK_EVENTS
    assert Path(client._ENGINE_DIR) == ROOT


def test_the_client_finds_the_store_the_daemon_writes(client, tmp_path: Path, monkeypatch):
    from windvane import config

    assert Path(client._storage_dir()) == Path(config.store_dir()) == tmp_path / "store"
    monkeypatch.delenv("WINDVANE_DIR")
    assert Path(client._storage_dir()) == Path(config.store_dir()) == Path.home() / ".windvane"


TOKEN = "f" * 64  # the fake daemon's token, in the store's daemon_token file


def _fake_daemon(tmp_path: Path, answer: dict):
    """A listener that reads one JSON line, records it, answers ``answer``."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    got = []

    def serve():
        conn, _ = srv.accept()
        data = b""
        while b"\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        got.append(json.loads(data))
        conn.sendall((json.dumps(answer) + "\n").encode())
        conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    (tmp_path / "store" / "daemon_port").write_text(str(srv.getsockname()[1]))
    (tmp_path / "store" / "daemon_token").write_text(TOKEN)
    return srv, t, got


def test_one_round_trip_prints_the_daemons_output(client, tmp_path: Path, monkeypatch):
    srv, t, got = _fake_daemon(tmp_path, {"output": "from the daemon\n"})
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/proj")
    monkeypatch.setenv("WINDVANE_AUTONOMY", "1")
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    try:
        assert client._try_daemon("prompt_json", '{"prompt": "hi"}') is True
    finally:
        t.join(5)
        srv.close()
    assert out.getvalue() == "from the daemon\n"
    req = got[0]
    assert req["token"] == TOKEN  # the daemon refuses a request without its token
    assert req["hook_event"] == "prompt_json" and req["stdin"] == '{"prompt": "hi"}'
    assert set(req["env"]) == set(client.SESSION_ENV)
    assert req["env"]["CLAUDE_PROJECT_DIR"] == "/proj" and req["env"]["WINDVANE_AUTONOMY"] == "1"


def test_an_error_answer_or_no_daemon_means_the_fallback(client, tmp_path: Path):
    srv, t, _ = _fake_daemon(tmp_path, {"error": "unsupported hook_event"})
    try:
        assert client._try_daemon("prompt_json", "{}") is False
    finally:
        t.join(5)
        srv.close()
    (tmp_path / "store" / "daemon_port").unlink()
    assert client._try_daemon("prompt_json", "{}") is False  # no port file
    (tmp_path / "store" / "daemon_port").write_text("1")  # nobody listens
    assert client._try_daemon("prompt_json", "{}") is False
    # No token file, or an empty one: the daemon would refuse, so the client
    # never connects and runs the handler itself.
    srv, t, got = _fake_daemon(tmp_path, {"output": "never"})
    try:
        (tmp_path / "store" / "daemon_token").unlink()
        assert client._try_daemon("prompt_json", "{}") is False
        (tmp_path / "store" / "daemon_token").write_text("")
        assert client._try_daemon("prompt_json", "{}") is False
        assert got == []
    finally:
        with socket.create_connection(("127.0.0.1", srv.getsockname()[1]), timeout=5) as s:
            s.sendall(b"{}\n")  # let the listener's one accept finish
        t.join(5)
        srv.close()


def test_the_fallback_runs_the_dispatch_in_process(client, monkeypatch):
    """Mocked seam: the fallback calls windvane.events.dispatch and prints
    what it returns; a SystemExit or an exception never escapes."""
    seen = []
    fake = types.ModuleType("windvane.events")
    fake.dispatch = lambda event, stdin_text: seen.append((event, stdin_text)) or "handled\n"
    monkeypatch.setitem(sys.modules, "windvane.events", fake)
    import windvane

    monkeypatch.setattr(windvane, "events", fake, raising=False)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    client._run_fallback("stop_json", '{"session_id": "s"}')
    assert seen == [("stop_json", '{"session_id": "s"}')] and out.getvalue() == "handled\n"
    assert sys.path[0] == str(ROOT) or Path(sys.path[0]) == ROOT

    def exiting(event, stdin_text):
        raise SystemExit(2)

    fake.dispatch = exiting
    client._run_fallback("bash_json", "{}")  # swallowed
    fake.dispatch = lambda e, s: 1 / 0
    client._run_fallback("bash_json", "{}")  # swallowed


def test_the_fallback_drives_main_when_there_is_no_dispatch(client, monkeypatch):
    seen = {}
    common = types.ModuleType("windvane.events.common")
    common._stdin_cache = None
    fake = types.ModuleType("windvane.events")
    fake.common = common
    fake.main = lambda: seen.update(argv=list(sys.argv), stdin=common._stdin_cache)
    monkeypatch.setitem(sys.modules, "windvane.events", fake)
    monkeypatch.setitem(sys.modules, "windvane.events.common", common)
    import windvane

    monkeypatch.setattr(windvane, "events", fake, raising=False)
    monkeypatch.setattr(sys, "argv", ["daemon_client.py", "stop_json"])
    client._run_fallback("stop_json", '{"session_id": "s"}')
    assert seen == {"argv": ["windvane.events", "stop_json"], "stdin": '{"session_id": "s"}'}


def test_a_spawn_is_guarded_by_the_env_and_the_marker(client, tmp_path: Path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)))
    client._nudge_daemon()
    assert calls == []  # WINDVANE_NO_DAEMON
    monkeypatch.delenv("WINDVANE_NO_DAEMON")
    client._nudge_daemon()
    cmd, kw = calls[-1]
    assert cmd[1:] == ["-m", "windvane.daemon"] and Path(kw["cwd"]) == ROOT
    assert (tmp_path / "store" / "daemon_starting").exists()
    client._nudge_daemon()
    assert len(calls) == 1  # one spawn per 30 s


def test_main_sends_only_its_own_events_and_falls_back_for_the_rest(client, monkeypatch):
    tried, ran, nudged = [], [], []
    monkeypatch.setattr(client, "_read_stdin", lambda: "{}")
    monkeypatch.setattr(client, "_try_daemon", lambda h, p: tried.append(h) or False)
    monkeypatch.setattr(client, "_run_fallback", lambda h, p: ran.append(h))
    monkeypatch.setattr(client, "_nudge_daemon", lambda: nudged.append(1))
    monkeypatch.setattr(sys, "argv", ["daemon_client.py", "prompt_json"])
    client.main()
    monkeypatch.setattr(sys, "argv", ["daemon_client.py", "session_end_json"])
    client.main()
    assert tried == ["prompt_json"] and ran == ["prompt_json", "session_end_json"] and len(nudged) == 1
    monkeypatch.setattr(sys, "argv", ["daemon_client.py"])
    client.main()  # no event: nothing
    assert len(ran) == 2


def test_the_client_runs_under_python_dash_s_by_path(tmp_path: Path):
    """The plugin's classic hooks run it exactly like this. With no daemon
    and the fallback in place, it exits 0 whatever the handler does."""
    env = {**os.environ, "WINDVANE_DIR": str(tmp_path / "store"), "WINDVANE_NO_DAEMON": "1"}
    env.pop("PYTHONPATH", None)
    r = subprocess.run([sys.executable, "-S", str(CLIENT), "notification_json"], input="{}",
                       capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=60)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "store" / "daemon_starting").exists()  # no spawn under NO_DAEMON

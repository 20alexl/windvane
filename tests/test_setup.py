"""The setup check (windvane.setup): one JSON line about the interpreter, the
store, the semantic extra and the daemon; read-only unless --import."""

import importlib
import io
import json
import os
import socket
import subprocess
import sys
import threading
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1] / "engine"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    from windvane import daemon

    importlib.reload(daemon)  # its port file names the temp store
    yield
    importlib.reload(daemon)


def _run(*argv):
    from windvane import setup

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = setup.main(list(argv))
    lines = buf.getvalue().strip().splitlines()
    assert len(lines) == 1, buf.getvalue()
    return rc, json.loads(lines[0])


def test_a_fresh_machine_reports_every_part(tmp_path: Path):
    rc, out = _run()
    assert rc == 0
    assert out["python"]["ok"] is True and out["python"]["path"] == sys.executable
    assert out["python"]["version"].startswith(f"{sys.version_info[0]}.{sys.version_info[1]}.")
    assert out["store"] == {"path": str(tmp_path / "store"), "exists": False, "projects": 0}
    assert set(out["semantic"]) == {"installed", "requested"}
    assert out["daemon"] == {"answers": False, "port": None}
    assert "import" not in out
    assert not (tmp_path / "store").exists()  # read-only


def test_an_old_interpreter_exits_1(monkeypatch):
    from windvane import setup

    monkeypatch.setattr(setup, "MIN_PYTHON", (99, 0))
    rc, out = _run()
    assert rc == 1 and out["python"]["ok"] is False and "99.0" in out["error"]


def test_import_runs_the_migration_and_the_store_then_counts(tmp_path: Path):
    src = tmp_path / "engram"
    (src / "projects" / "h1").mkdir(parents=True)
    (src / "projects" / "h1" / "memory.json").write_text("{}", encoding="utf-8")
    (src / "manifest.json").write_text(json.dumps({"projects": {"e:/w/a": {"hash": "h1"}, "e:/w/b": {"hash": "h2"}}}), encoding="utf-8")
    rc, out = _run("--import", "--from", str(src))
    assert rc == 0 and out["import"]["copied"] == 2
    assert out["store"]["exists"] is True and out["store"]["projects"] == 2
    rc, out = _run("--import", "--from", str(src))
    assert rc == 1 and "error" in out["import"]  # an existing store, no --merge
    rc, out = _run("--import", "--from", str(src), "--merge")
    assert rc == 0 and out["import"]["copied"] == 0


def test_a_daemon_that_answers_is_reported_with_its_port(tmp_path: Path):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port = srv.getsockname()[1]

    def answer():
        conn, _ = srv.accept()
        data = b""
        while b"\n" not in data:
            data += conn.recv(4096)
        assert json.loads(data)["hook_event"] == "setup_ping"
        conn.sendall(b'{"error": "unsupported hook_event \'setup_ping\'"}\n')
        conn.close()

    t = threading.Thread(target=answer, daemon=True)
    t.start()
    (tmp_path / "store").mkdir()
    (tmp_path / "store" / "daemon_port").write_text(str(port))
    try:
        rc, out = _run()
    finally:
        t.join(5)
        srv.close()
    assert out["daemon"] == {"answers": True, "port": port}
    rc, out = _run()  # the listener is gone: a stale port file
    assert out["daemon"] == {"answers": False, "port": port}


def test_the_module_runs_as_a_command(tmp_path: Path):
    env = {**os.environ, "PYTHONPATH": str(ENGINE), "WINDVANE_DIR": str(tmp_path / "store"), "WINDVANE_NO_DAEMON": "1"}
    r = subprocess.run([sys.executable, "-m", "windvane.setup"], capture_output=True, text=True, env=env,
                       stdin=subprocess.DEVNULL, timeout=120)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["store"]["path"] == str(tmp_path / "store")

"""
Check the machine windvane runs on, and optionally import an engram store.

    python -m windvane.doctor [--import] [--from DIR] [--merge]

Prints one JSON line:

    {"python": {"version": "3.12.4", "path": "...", "ok": true},
     "store": {"path": "...", "exists": true, "projects": 12},
     "semantic": {"installed": false, "requested": false},
     "daemon": {"answers": false, "port": null},
     "import": {...}}            # only with --import (windvane.migrate)

Exit 1 when the interpreter is below 3.10 (windvane needs 3.10), or when
the import was asked for and failed. Reads only: it never starts, stops or
replaces a daemon.

Written in syntax old interpreters still parse, so that a 3.8 reports
itself instead of failing on a newer construct.
"""

import json
import os
import socket
import sys

MIN_PYTHON = (3, 10)


def _python_info():
    v = sys.version_info
    return {
        "version": "%d.%d.%d" % (v[0], v[1], v[2]),
        "path": sys.executable,
        "ok": tuple(v[:2]) >= MIN_PYTHON,
    }


def _store_info():
    from windvane.config import store_dir

    path = str(store_dir())
    manifest = os.path.join(path, "manifest.json")
    projects = 0
    if os.path.isfile(manifest):
        try:
            with open(manifest, encoding="utf-8") as f:
                data = json.load(f)
            projects = len(data.get("projects") or {})
        except Exception:
            projects = 0
    return {"path": path, "exists": os.path.isfile(manifest), "projects": projects}


def _semantic_info():
    """Does the semantic extra actually import (not just exist on disk)?"""
    from windvane import semantic

    try:
        installed = semantic.imports()
    except Exception:
        installed = False
    try:
        requested = semantic.requested()
    except Exception:
        requested = False
    return {"installed": installed, "requested": requested}


def _daemon_info(timeout=1.0):
    """Ask the daemon named by the port file for a cheap answer. A hook event
    no handler serves comes back at once with an error, which proves the
    daemon reads and answers; nothing else is touched."""
    from windvane import daemon

    try:
        port = int(daemon.PORT_FILE.read_text().strip())
    except (OSError, ValueError):
        return {"answers": False, "port": None}
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        sock.settimeout(timeout)
        ping = daemon._with_token({"hook_event": "doctor_ping", "stdin": ""})
        sock.sendall((json.dumps(ping) + "\n").encode("utf-8"))
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        sock.close()
        json.loads(data.decode("utf-8").strip())
        return {"answers": True, "port": port}
    except Exception:
        return {"answers": False, "port": port}


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m windvane.doctor",
        description="Check the interpreter, the store, the semantic extra and the daemon.",
    )
    parser.add_argument("--import", dest="do_import", action="store_true",
                        help="also import an engram store (python -m windvane.migrate --import)")
    parser.add_argument("--from", dest="src", default=None,
                        help="the engram store to import (default ~/.claude_engram)")
    parser.add_argument("--merge", action="store_true",
                        help="import into an existing store: only the projects it lacks")
    args = parser.parse_args(argv)

    py = _python_info()
    out = {"python": py}
    if not py["ok"]:
        out["error"] = "windvane needs Python %d.%d or newer" % MIN_PYTHON
        print(json.dumps(out))
        return 1

    failed = False
    if args.do_import:
        from windvane import migrate

        kwargs = {"merge": args.merge}
        if args.src:
            kwargs["src"] = args.src
        try:
            out["import"] = migrate.import_engram_store(**kwargs)
        except Exception as e:
            out["import"] = {"error": ("%s: %s" % (type(e).__name__, e))[:300]}
        failed = "error" in out["import"]

    for key, fn in (("store", _store_info), ("semantic", _semantic_info), ("daemon", _daemon_info)):
        try:
            out[key] = fn()
        except Exception as e:
            out[key] = {"error": ("%s: %s" % (type(e).__name__, e))[:200]}

    print(json.dumps(out))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

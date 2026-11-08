"""
Alerts: one line, out of the session, when something needs a person.

Two channels, both recorded in the session state either way:

* In the session, the model calls ``PushNotification`` (desktop, and the
  phone when Remote Control is connected). Hooks cannot call it; the halt
  text tells the model when to.
* Out of the session -- the run died on an API error, a permission prompt
  is waiting with nobody there -- a shell command the owner configured runs
  with the message. Nothing is built in: there is no service windvane would
  be right to pick. ``{message}`` in the command is replaced (shell-quoted);
  with no placeholder the message arrives on stdin.

    .windvane/config.json:  {"alert_command": "curl -s -d {message} https://ntfy.sh/<topic>"}
    or WINDVANE_ALERT_COMMAND, or the plugin's alert_command setting

The rule: under 200 characters, leads with what to act on, never for
routine progress.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

ALERT_TIMEOUT = 15.0
KEEP = 50
MAX_DELIVERIES = 2
QUEUED = "queued"
OUTCOMES_FILE = "alert_outcomes.json"  # in the store: how the last KEEP deliveries went


def alert_command(project_dir: str = "") -> str:
    try:
        from windvane import config

        return str(config.knob("alert_command", project_dir) or "").strip()
    except Exception:
        return ""


def _quote(s: str) -> str:
    if os.name == "nt":
        # cmd.exe has no escape inside double quotes: a backslash before a
        # quote ends the argument and the rest of the message runs as a
        # command, and %NAME% expands. The message can hold text from a
        # session, so the two characters are left out.
        return '"' + s.replace('"', "'").replace("%", " percent ") + '"'
    return shlex.quote(s)


def _text(message: str) -> str:
    """The one line that goes out and into the session state."""
    from windvane.privacy import redact

    return redact(" ".join(str(message or "").split()))[:200]


def send(
    message: str,
    project_dir: str = "",
    kind: str = "info",
    state: Optional[dict] = None,
    command: str = "",
) -> dict:
    """Run the owner's alert command with the message. Returns the record
    ({at, kind, message, sent, detail}); appends it to state["alerts"] when a
    state dict is given. ``command`` overrides the configured one. Never
    raises."""
    message = _text(message)
    rec = {"at": time.time(), "kind": kind, "message": message, "sent": False, "detail": ""}
    cmd = (command or "").strip() or alert_command(project_dir)
    if not cmd:
        rec["detail"] = "no alert_command configured"
    else:
        try:
            if "{message}" in cmd:
                full = cmd.replace("{message}", _quote(message))
                r = subprocess.run(full, shell=True, capture_output=True, text=True, timeout=ALERT_TIMEOUT, stdin=subprocess.DEVNULL)
            else:
                r = subprocess.run(cmd, shell=True, input=message, capture_output=True, text=True, timeout=ALERT_TIMEOUT)
            rec["sent"] = r.returncode == 0
            rec["detail"] = (r.stdout or r.stderr or "").strip()[:200] if r.returncode != 0 else "ok"
        except subprocess.TimeoutExpired:
            rec["detail"] = f"alert command timed out after {ALERT_TIMEOUT:.0f}s"
        except Exception as e:
            rec["detail"] = str(e)[:200]
    if isinstance(state, dict):
        state["alerts"] = (list(state.get("alerts") or []) + [rec])[-KEEP:]
    return rec


def enqueue(message: str, project_dir: str = "", kind: str = "info", state: Optional[dict] = None) -> dict:
    """``send`` for a hook: the command runs in a process of its own and the
    hook goes on. It ran inside the hook, under the daemon's one lock, for up
    to ALERT_TIMEOUT: every session's hooks waited for a slow webhook. The
    record says queued; the delivery's outcome lands in OUTCOMES_FILE, and
    ``summary`` joins the two. Never raises."""
    cmd = alert_command(project_dir)
    if not cmd:
        return send(message, project_dir, kind, state)
    text = _text(message)
    rec = {"at": time.time(), "kind": kind, "message": text, "sent": False, "detail": QUEUED}
    try:
        kwargs: dict = {"stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                        "cwd": str(Path(__file__).resolve().parent.parent)}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        child = subprocess.Popen([sys.executable, "-m", "windvane.alerts"], **kwargs)
        child.stdin.write(json.dumps({"message": text, "command": cmd, "kind": kind, "at": rec["at"]}).encode("utf-8"))
        child.stdin.close()
    except Exception as e:
        rec["detail"] = f"could not queue: {str(e)[:150]}"
    if isinstance(state, dict):
        state["alerts"] = (list(state.get("alerts") or []) + [rec])[-KEEP:]
    return rec


def _deliver() -> None:
    """The process ``enqueue`` starts. At most MAX_DELIVERIES at once, so a
    stuck webhook cannot pile processes up on a run that alerts in a loop."""
    from windvane import atomic, proc_lock
    from windvane.config import store_dir

    request = json.loads(sys.stdin.buffer.read(65536))
    store = Path(store_dir())
    slot = None
    for n in range(MAX_DELIVERIES):
        slot = proc_lock.acquire(store / f"alert_slot_{n}.lock")
        if slot:
            break
    if slot:
        rec = send(request["message"], command=request["command"], kind=request.get("kind", "info"))
    else:
        rec = {"at": time.time(), "kind": request.get("kind", "info"), "message": request["message"],
               "sent": False, "detail": f"not sent: {MAX_DELIVERIES} alerts already going out"}
    rec["queued_at"] = request.get("at")
    path = store / OUTCOMES_FILE
    with atomic.locked(path, wait=5.0, must_land=True):  # no hook waits on this process
        atomic.write_json(path, {"outcomes": ([*_outcomes(), rec])[-KEEP:]}, indent=2)


def _outcomes() -> list:
    from windvane import atomic
    from windvane.config import store_dir

    try:
        data = atomic.read_json(Path(store_dir()) / OUTCOMES_FILE)
        rows = data.get("outcomes") if isinstance(data, dict) else None
        return [o for o in rows if isinstance(o, dict)] if isinstance(rows, list) else []
    except OSError:
        return []


def summary(state: dict) -> list[dict]:
    """The session's alerts, each queued one with how its delivery went once it
    has gone out."""
    rows = [a for a in (state.get("alerts") or []) if isinstance(a, dict)]
    if not any(a.get("detail") == QUEUED for a in rows):
        return rows
    done = {o.get("queued_at"): o for o in _outcomes()}
    out = []
    for a in rows:
        o = done.get(a.get("at")) if a.get("detail") == QUEUED else None
        out.append({**a, "sent": bool(o.get("sent")), "detail": o.get("detail", "")} if o else a)
    return out


if __name__ == "__main__":
    try:
        _deliver()
    except Exception:
        pass

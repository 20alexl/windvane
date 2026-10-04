"""
``python -m windvane.remember``: store one text as a memory entry.

The plugin's /remember command calls this with the text the person selected
in the transcript (on stdin, so no shell quoting touches it). The entry goes
through ``MemoryStore.remember_discovery``, the writer behind
``memory(remember)`` and the miner's decisions: the same duplicate check,
tags, related files and atomic save.

    python -m windvane.remember --project <dir> [--file <path>] \\
        [--kind decision|discovery] [--text <text>]   # else stdin

The project is the registered project the path belongs to (the deepest one
at or above ``--project``); with ``--file`` under it, the deepest registered
project holding that file, the way a hook scopes by the files a session
touches. A path no project holds is registered as given.

Prints one JSON line: ``{"stored", "id", "project", "message"}``, or
``{"error"}`` with exit status 1.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Optional

KINDS = {"decision": "DECISION: ", "discovery": ""}


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def resolve_project(registered: list, project: str, file: str = "") -> str:
    """The registered project for ``project`` (normalized), narrowed to the
    one holding ``file`` when the file lies under it."""
    holders = [p for p in registered if _under(project, p)]
    base = max(holders, key=len) if holders else project
    if file and _under(file, base):
        inner = [p for p in registered if _under(file, p) and _under(p, base)]
        if inner:
            return max(inner, key=len)
    return base


def remember(project: str, text: str, kind: str = "decision", file: str = "") -> dict:
    from windvane.store import MemoryStore

    store = MemoryStore()
    norm = MemoryStore._normalize_path
    target = resolve_project(
        list(store._manifest.get("projects", {})),
        norm(project),
        norm(file) if file else "",
    )
    added, message = store.remember_discovery(
        target,
        KINDS[kind] + text,
        source="windvane-remember",
        relevance=5,
        category=kind,
        auto_embed=False,  # no encoder started for one command; the miner embeds later
    )
    if store._save_error:
        return {"error": store._save_error}
    found = re.search(r"id=([^,\s)]+)", message)
    return {"stored": added, "id": found.group(1) if found else "", "project": target, "message": message}


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m windvane.remember", description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--project", required=True, help="the session's directory")
    ap.add_argument("--file", default="", help="the file the session last touched, if any")
    ap.add_argument("--kind", choices=sorted(KINDS), default="decision")
    ap.add_argument("--text", default=None, help="the text; read from stdin when left out")
    args = ap.parse_args(argv)
    text = args.text if args.text is not None else sys.stdin.buffer.read().decode("utf-8", errors="replace")
    text = text.strip()
    if not text:
        print(json.dumps({"error": "nothing to remember: the text is empty"}))
        return 1
    try:
        out = remember(args.project, text, args.kind, args.file)
    except Exception as exc:  # one JSON line whatever broke; the plugin shows it
        out = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(out))
    return 1 if "error" in out else 0


if __name__ == "__main__":
    sys.exit(main())

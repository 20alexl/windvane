"""
Bring a project's memory to where its folder went.

    python -m windvane.move OLD NEW
    python <plugin folder>/windvane/move.py OLD NEW

The store files a project's memory under its folder's path, so a project moved
to another folder starts empty there while its rules, mistakes and decisions
stay under a path nothing reads again. A move brings every memory the new
project does not already hold (the same id, or the same text) and the old
project's archived rows into the new one, points file references under the old
folder at the new one, and only then forgets the old project. Prints one JSON
line; exit 1 when nothing was moved.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):  # run as a file: the package is beside it
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _repath(value: str, old: str, new: str) -> str:
    """``value`` with a leading ``old`` folder replaced by ``new``, in either slash style."""
    flat = value.replace("\\", "/")
    if flat.lower().startswith(old.lower() + "/") or flat.lower() == old.lower():
        return new + flat[len(old):]
    return value


def _repath_text(text: str, old: str, new: str) -> str:
    out = text
    for spelled in {old, old.replace("/", "\\")}:
        for variant in {spelled, spelled[:1].upper() + spelled[1:]}:
            out = out.replace(variant, new)
    return out


def move_project(old_path: str, new_path: str, store=None) -> dict:
    """Move ``old_path``'s memory and archive into ``new_path``. The report
    counts what moved; ``error`` names why nothing was forgotten."""
    from windvane.store import MemoryStore

    store = store or MemoryStore()
    old, new = store._normalize_path(old_path), store._normalize_path(new_path)
    if old == new:
        return {"error": "the old and the new path are the same project"}
    source = store.get_project(old)
    if source is None:
        return {"error": f"no project is filed under {old}"}
    target = store.remember_project(new)
    report = {"from": old, "to": new, "moved": 0, "already_there": 0, "archived": 0, "forgot": False}
    have = {e.id for e in target.entries}
    for entry in source.entries:
        same = [e for e in target.entries if e.category == entry.category]
        if entry.id in have or store._is_duplicate(entry.content, same):
            report["already_there"] += 1
            continue
        moved = copy.deepcopy(entry)
        moved.content = _repath_text(moved.content, old, new)
        moved.related_files = [_repath(f, old, new) for f in moved.related_files]
        target.entries.append(moved)
        store._update_indexes(target, moved)
        have.add(moved.id)
        report["moved"] += 1
    for table in ("key_files", "key_directories"):
        for key, value in getattr(source, table).items():
            getattr(target, table).setdefault(_repath(key, old, new), value)
    for field in ("summary", "language", "framework"):
        if not getattr(target, field) and getattr(source, field):
            setattr(target, field, getattr(source, field))
    store._dirty_projects.add(target.project_path)
    if not store._save() or target.project_path in store._dirty_projects:
        return {**report, "error": f"the new project could not be written: {store._save_error}"}

    store._load_archive()
    shelved = store._archive_projects.get(old)
    if shelved is not None and shelved.entries:
        into = store._archive_project_for(new, target.project_name)
        kept = {e.id for e in into.entries}
        before = list(into.entries)
        for entry in shelved.entries:
            if entry.id not in kept:
                into.entries.append(copy.deepcopy(entry))
                report["archived"] += 1
        if not store._save_archive():
            into.entries = before
            return {**report, "error": "archive.json could not be written; the old project is kept"}

    store.forget_project(old)
    report["forgot"] = store.get_project(old) is None
    return report


def moved_hint(project_path: str, store=None) -> list:
    """Lines for the session-start banner: a project whose folder is gone,
    with this folder's name and memories of its own, is likely this one moved.
    The exact command to bring them is named; nothing is moved by itself."""
    try:
        from windvane.store import MemoryStore

        store = store or MemoryStore()
        here = store._normalize_path(project_path)
        name = Path(here).name.lower()
        script = Path(__file__).resolve()
        lines = []
        for path in list(store._manifest.get("projects", {})):
            if path == here or Path(path).name.lower() != name or os.path.isdir(path):
                continue
            if not os.path.isdir(str(Path(path).parent)) and not os.path.isdir(str(Path(path).anchor)):
                continue  # the drive is not there now: the folder may only be away
            proj = store.get_project(path)
            store._load_archive()
            shelved = store._archive_projects.get(path)
            count = (len(proj.entries) if proj else 0) + (len(shelved.entries) if shelved else 0)
            if count:
                lines.append(
                    f"windvane: {count} memories (archived ones counted) of a project named {Path(path).name} are filed under {path},"
                    f" a folder that no longer exists. If this is that project moved, bring them here with:"
                    f' python "{script}" "{path}" "{here}"'
                )
        return lines
    except Exception:
        return []


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m windvane.move", description="Bring a moved project's memory to its new folder.")
    parser.add_argument("old", help="the folder the project's memory is filed under")
    parser.add_argument("new", help="the folder the project is in now")
    args = parser.parse_args(argv)
    try:
        report = move_project(args.old, args.new)
    except Exception as e:  # the person reads the reason
        report = {"error": f"{type(e).__name__}: {e}"[:300]}
    print(json.dumps(report))
    return 1 if "error" in report else 0


if __name__ == "__main__":
    sys.exit(main())

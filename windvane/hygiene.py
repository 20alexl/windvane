"""
The store hygiene that reaches every project, not only the one being mined.

The miner cleans the memory of the project whose session it mines. Every
other registered project kept its broken entries and doubles until a session
ran there again, and a project whose folder was deleted (a finished worktree,
a removed checkout) stayed registered for good. Once a day this sweep:

- cleans every other project whose folder exists: broken entries go, near
  duplicates merge, shared tags become clusters. It never ages them: decay
  and archiving judge an entry by when it was last read, and in a project
  nobody opened for months every entry would look stale, so they stay with
  the project in use;
- forgets a project whose folder is gone, when it holds no memory and no
  archived rows, nothing in its store folder changed for 30 days, and some
  folder above it still exists below the drive's root (a drive that is away
  or unplugged is not a deleted project). A gone project with memories is
  kept, and the session-start banner offers to move it (windvane.move).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

QUIET_DAYS = 30
INTERVAL_SECS = 24 * 3600


def stamp_file(storage_dir=None) -> Path:
    """The day's stamp, in the store the miner was handed (else the configured one)."""
    if storage_dir:
        return Path(storage_dir).expanduser() / "hygiene_last"
    from windvane.paths import get_windvane_storage_dir

    return get_windvane_storage_dir() / "hygiene_last"


def due(storage_dir=None, now: float | None = None) -> bool:
    """Has a day passed since the last sweep (or has none run)?"""
    try:
        age = (now or time.time()) - stamp_file(storage_dir).stat().st_mtime
    except OSError:
        return True
    return age >= INTERVAL_SECS


def stamp(storage_dir=None) -> None:
    try:
        f = stamp_file(storage_dir)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(str(time.time()).encode("ascii"))
    except OSError:
        pass


def _drive_present(path: str) -> bool:
    """Does some folder above ``path`` still exist below its root?"""
    p = Path(path)
    for parent in p.parents:
        if parent == Path(p.anchor) or str(parent) in ("", "."):
            return False
        if parent.is_dir():
            return True
    return False


def _quiet(folder: Path, now: float) -> bool:
    try:
        newest = max((f.stat().st_mtime for f in folder.iterdir()), default=0.0)
    except OSError:
        return False
    return now - newest >= QUIET_DAYS * 86400


def _forgettable(store, path: str, hash_id: str, now: float) -> bool:
    if os.path.isdir(path) or not _drive_present(path):
        return False
    proj = store.get_project(path)
    if proj is not None and proj.entries:
        return False
    store._load_archive()
    shelved = store._archive_projects.get(path)
    if shelved is not None and shelved.entries:
        return False
    return _quiet(store._projects_dir / hash_id, now)


def sweep_projects(store, current_project: str, now: float | None = None) -> dict:
    """Clean every other existing project and forget the gone empty ones.
    The report counts the cleaned projects and lists the forgotten paths."""
    now = now or time.time()
    here = store._normalize_path(current_project)
    report: dict = {"cleaned": 0, "forgot": [], "errors": {}}
    for path, row in list(store._manifest.get("projects", {}).items()):
        if path == here:
            continue  # the miner cleans the project in use itself, with decay
        try:
            if os.path.isdir(path):
                result = store.cleanup_memories(path, dry_run=False, apply_decay=False)
                if "error" not in result:
                    report["cleaned"] += 1
            elif _forgettable(store, path, row.get("hash", ""), now):
                store.forget_project(path)
                if path not in store._manifest.get("projects", {}):
                    report["forgot"].append(path)
        except Exception as e:  # one project never stops the sweep
            report["errors"][path] = f"{type(e).__name__}: {e}"[:200]
    return report

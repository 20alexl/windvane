"""
Import an existing claude-engram store into the windvane store.

windvane reads an engram store as it is: the memory files, the checkpoint
rings and the session index keep their format, and the store module reads
either vocabulary. So the import is a copy, not a conversion:

- The tree under the source (default ``~/.claude_engram``) is copied into
  ``config.store_dir()``: the manifest, ``projects/<hash>/*``,
  ``checkpoints/``, the archive and the other store files.
- Left out: ``sessions/`` (per-session working state, meaningless to a new
  install) and the runtime files of a live engram process (its daemon's
  port/pid/model/lock files, the miner's lock and status, spawn markers,
  half-written ``*.tmp`` files).
- The manifest is the only file rewritten, and only its spelling: a
  top-level key that carries the old name (``engram``) is renamed to carry
  ``windvane``. Values are never touched; a project path that happens to
  contain the word stays the path it is.
- The source is never deleted, moved or written.
- A destination that already holds a manifest is refused, unless
  ``merge=True``: then only the projects the destination does not already
  register are copied (their ``projects/<hash>`` folders and their manifest
  entries); nothing the destination holds is overwritten.

CLI (one JSON line on stdout)::

    python -m windvane.migrate --import [--from DIR] [--dry-run] [--merge]

prints ``{"copied": N, "skipped": N, "dst": ...}`` or ``{"error": ...}``
(exit 1 on an error).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

DEFAULT_SOURCE = "~/.claude_engram"

# Directories under the source that are never copied.
EXCLUDED_DIRS = {"sessions"}

# Top-level files that belong to a running engram process, not to the store.
EXCLUDED_FILES = {
    "scorer_port",
    "scorer_pid",
    "scorer_model",
    "scorer_device",
    "scorer.lock",
    "scorer_starting",
    "mining.lock",
    "mining.pid",
    "mining_status.json",
    "session_active",
    "live_mine_last",
}

OLD_NAME = "engram"
NEW_NAME = "windvane"


def _default_dst() -> Path:
    from windvane.config import store_dir

    return Path(store_dir())


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _respell_manifest(manifest: dict) -> dict:
    """Rename top-level keys that carry the old name. Values stay as they are
    (they are paths, hashes and migration ids)."""
    out: dict = {}
    for key, value in manifest.items():
        new_key = key
        if isinstance(key, str) and OLD_NAME in key.lower():
            new_key = key.replace("claude_engram", NEW_NAME).replace(
                "Engram", "Windvane"
            ).replace(OLD_NAME, NEW_NAME)
            if new_key in manifest:
                new_key = key  # never clobber a key that already exists
        out[new_key] = value
    return out


def _excluded(rel: Path) -> bool:
    parts = rel.parts
    if not parts:
        return False
    if parts[0] in EXCLUDED_DIRS:
        return True
    if len(parts) == 1 and parts[0] in EXCLUDED_FILES:
        return True
    return rel.name.endswith(".tmp")


def _files_under(root: Path):
    """Every regular file under root, as paths relative to root, sorted."""
    return sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file())


def _copy_file(src: Path, dst: Path, dry_run: bool) -> bool:
    """Copy one file unless dst exists. True when it was (or would be) copied."""
    if dst.exists():
        return False
    if not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return True


def import_engram_store(
    src: "str | Path" = DEFAULT_SOURCE,
    dst: "str | Path | None" = None,
    dry_run: bool = False,
    merge: bool = False,
) -> dict:
    """Copy an engram store into the windvane store (module docstring).

    Returns ``{"copied": N, "skipped": N, "dst": str}`` (counts of files;
    ``skipped`` counts the files left out: runtime files, ``sessions/``, and
    under merge the projects the destination already registers), plus
    ``"dry_run": True`` when nothing was written, or ``{"error": str}``.
    """
    src_path = Path(src).expanduser()
    dst_path = Path(dst).expanduser() if dst is not None else _default_dst()
    src_manifest_path = src_path / "manifest.json"
    dst_manifest_path = dst_path / "manifest.json"

    if not src_manifest_path.is_file():
        return {"error": f"no engram store at {src_path} (no manifest.json)"}
    try:
        if src_path.resolve() == dst_path.resolve():
            return {"error": "source and destination are the same folder"}
    except OSError:
        pass
    if dst_manifest_path.exists() and not merge:
        return {
            "error": f"{dst_path} already holds a store; pass --merge to add "
            "only the projects it does not have"
        }

    src_manifest = _load_json(src_manifest_path)
    copied = 0
    skipped = 0

    if not merge:
        for rel in _files_under(src_path):
            if rel == Path("manifest.json"):
                continue  # written below, respelled
            if _excluded(rel):
                skipped += 1
                continue
            if _copy_file(src_path / rel, dst_path / rel, dry_run):
                copied += 1
            else:
                skipped += 1
        if not dry_run:
            _write_json_atomic(dst_manifest_path, _respell_manifest(src_manifest))
        copied += 1  # the manifest
    else:
        dst_manifest = _load_json(dst_manifest_path)
        dst_projects = dst_manifest.setdefault("projects", {})
        if not isinstance(dst_projects, dict):
            return {"error": f"{dst_manifest_path} has no readable project table"}
        added = 0
        for project, info in (src_manifest.get("projects") or {}).items():
            h = info.get("hash") if isinstance(info, dict) else None
            proj_src = src_path / "projects" / h if h else None
            files = _files_under(proj_src) if proj_src and proj_src.is_dir() else []
            if project in dst_projects or (h and (dst_path / "projects" / h).exists()):
                skipped += len(files)
                continue
            for rel in files:
                if rel.name.endswith(".tmp"):
                    skipped += 1
                    continue
                if _copy_file(proj_src / rel, dst_path / "projects" / h / rel, dry_run):
                    copied += 1
                else:
                    skipped += 1
            dst_projects[project] = info
            added += 1
        if added and not dry_run:
            _write_json_atomic(dst_manifest_path, dst_manifest)

    out: dict = {"copied": copied, "skipped": skipped, "dst": str(dst_path)}
    if dry_run:
        out["dry_run"] = True
    return out


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m windvane.migrate",
        description="Import a claude-engram store into the windvane store.",
    )
    parser.add_argument(
        "--import", dest="do_import", action="store_true",
        help="copy the engram store into the windvane store",
    )
    parser.add_argument("--from", dest="src", default=DEFAULT_SOURCE,
                        help=f"the engram store (default {DEFAULT_SOURCE})")
    parser.add_argument("--dry-run", action="store_true",
                        help="count what would be copied, write nothing")
    parser.add_argument("--merge", action="store_true",
                        help="into an existing store: copy only the projects it lacks")
    args = parser.parse_args(argv)
    if not args.do_import:
        print(json.dumps({"error": "nothing to do: pass --import"}))
        return 1
    try:
        result = import_engram_store(args.src, dry_run=args.dry_run, merge=args.merge)
    except Exception as e:
        result = {"error": f"{type(e).__name__}: {e}"[:300]}
    print(json.dumps(result))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())

"""
``python -m windvane.export``: the store's record of a project, as Markdown.

The store is internal; this writes what it holds for one project as files a
person can read, keep in the repository or hand to someone:

- ``rules.md``: the project's rules and the ones it inherits, with their
  detectors noted;
- ``mistakes.md``: the mistakes, hot and archived;
- ``decisions.md``: the decisions, hot and archived;
- ``checkpoints.md``: the checkpoint ring, newest first;
- ``runs/``: copies of the project's run reports.

Each file opens with the project and the time it was written; every entry
carries its date and id. Nothing is deleted from the store.

    python -m windvane.export --project DIR [--out DIR]

prints one JSON line: ``{"project", "out", "written": [paths]}``.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from typing import Optional


def _day(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown date"


def _stamp(ts) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown time"


def _head(title: str, project: str, now: float) -> list:
    return [f"# {title}", "", f"Project: `{project}`  ", f"Exported: {_stamp(now)}", ""]


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def _entry_line(e) -> str:
    return f"- **{_day(e.created_at)}** `[{e.id}]` {_one_line(e.content)}"


def _write(path: Path, lines: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(("\n".join(lines).rstrip("\n") + "\n").encode("utf-8"))
    return path


def _is_decision(e) -> bool:
    return e.category == "decision" or e.content.upper().startswith("DECISION:")


def _is_mistake(e) -> bool:
    return e.category == "mistake" or e.content.upper().startswith("MISTAKE:")


def _rules_md(store, project: str, now: float) -> list:
    lines = _head("Rules", project, now)
    pairs = store.get_rules_with_inheritance(project)
    if not pairs:
        return lines + ["No rules."]
    own = [(r, s) for r, s in pairs if not s]
    inherited = [(r, s) for r, s in pairs if s]
    for title, group in (("This project", own), ("Inherited", inherited)):
        if not group:
            continue
        lines += [f"## {title}", ""]
        for r, src in group:
            line = _entry_line(r)
            if r.detector:
                note = (r.detector or {}).get("note") or "detector"
                line += f" _(watched: {note})_"
            if src:
                line += f" _(from `{src}`)_"
            lines.append(line)
        lines.append("")
    return lines


def _entries_md(title: str, hot: list, archived: list, project: str, now: float) -> list:
    lines = _head(title, project, now)
    if not hot and not archived:
        return lines + [f"No {title.lower()}."]
    for heading, group in (("Active", hot), ("Archived", archived)):
        if not group:
            continue
        lines += [f"## {heading}", ""]
        for e in sorted(group, key=lambda x: x.created_at, reverse=True):
            line = _entry_line(e)
            if e.related_files:
                line += f" _(files: {', '.join(str(f) for f in e.related_files[:5])})_"
            lines.append(line)
        lines.append("")
    return lines


def _checkpoints_md(project: str, now: float) -> list:
    from windvane import checkpoints as ck

    lines = _head("Checkpoints", project, now)
    try:
        ring = ck.read_history(ck.candidate_dirs(project), limit=1000)
    except Exception:
        ring = []
    if not ring:
        return lines + ["No checkpoints."]
    for h in ring:
        task = _one_line(h.get("task_description") or h.get("summary") or "(no task)")
        ident = f"`[{h['task_id']}]` " if h.get("task_id") else ""
        lines += [f"## {_stamp(ck._created_ts(h))} {ident}({h.get('kind', 'auto')})", "", f"**Task:** {task}"]
        if h.get("current_step"):
            lines.append(f"**Current step:** {_one_line(h['current_step'])}")
        note = h.get("summary") or h.get("handoff_summary")
        if note and _one_line(note) != task:
            lines.append(f"**Handoff note:** {_one_line(note)}")
        for label, keys in (
            ("Completed", ("completed_steps",)),
            ("Pending", ("next_steps", "pending_steps")),
            ("Files", ("files_in_progress", "files_involved")),
            ("Decisions", ("decisions", "key_decisions")),
            ("Warnings", ("warnings", "handoff_warnings")),
            ("Context needed", ("context_needed", "handoff_context_needed")),
        ):
            items = next((h.get(k) for k in keys if h.get(k)), None) or []
            if items:
                lines.append(f"**{label}:**")
                lines.extend(f"- {_one_line(i)}" for i in items)
        lines.append("")
    return lines


def _run_reports(project: str) -> list:
    runs = Path(project) / ".windvane" / "runs"
    if not runs.is_dir():
        return []
    return sorted(p for p in runs.iterdir() if p.is_file() and p.suffix in (".md", ".json"))


def export_project(project: str, out_dir: Optional[str] = None) -> dict:
    """Write the project's rules, mistakes, decisions, checkpoints and run
    reports as Markdown under ``out_dir`` (default
    ``<project>/.windvane/export/``). Returns {"project", "out", "written"}."""
    from windvane.store import MemoryStore

    store = MemoryStore()
    norm = MemoryStore._normalize_path(project)
    out = Path(out_dir) if out_dir else Path(project) / ".windvane" / "export"
    out.mkdir(parents=True, exist_ok=True)
    now = time.time()

    proj = store.get_project(norm)
    hot = list(proj.entries) if proj else []
    store._load_archive()
    arch_proj = store._archive_projects.get(norm)
    archived = list(arch_proj.entries) if arch_proj else []

    written = [
        _write(out / "rules.md", _rules_md(store, norm, now)),
        _write(
            out / "mistakes.md",
            _entries_md("Mistakes", [e for e in hot if _is_mistake(e)], [e for e in archived if _is_mistake(e)], norm, now),
        ),
        _write(
            out / "decisions.md",
            _entries_md("Decisions", [e for e in hot if _is_decision(e)], [e for e in archived if _is_decision(e)], norm, now),
        ),
        _write(out / "checkpoints.md", _checkpoints_md(norm, now)),
    ]
    reports = _run_reports(project)
    if reports:
        (out / "runs").mkdir(parents=True, exist_ok=True)
        for src in reports:
            dest = out / "runs" / src.name
            shutil.copyfile(src, dest)
            written.append(dest)
    return {"project": norm, "out": str(out), "written": [str(p) for p in written]}


def main(argv: "Optional[list]" = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m windvane.export", description="Export a project's record as Markdown.")
    ap.add_argument("--project", required=True, help="the project directory")
    ap.add_argument("--out", default=None, help="where to write (default: <project>/.windvane/export/)")
    args = ap.parse_args(argv)
    try:
        result = export_project(args.project, args.out)
    except Exception as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

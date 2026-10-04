"""
``python -m windvane.brief``: the banner's pieces, rendered on demand.

The plugin's hooks module hands windvane's context to places no command
hook reaches: a subagent's prompt and the conversation a compaction
becomes. It asks this CLI for the text, so what it injects is exactly what
the hooks render:

- the rules block of the SessionStart banner (``_rules_block``);
- per file the prompt names, the pre-edit hook's past-mistakes lines
  (``_file_mistakes`` + ``_file_mistake_lines``, labelled with the file);
- with ``--checkpoint``, the checkpoint the SessionStart(compact) banner
  restores, rendered whole (``_banner_checkpoint`` + ``_format_restored_full``).

Read-only: it never writes the session state or the store.

    python -m windvane.brief --project <dir> [--session <id>]
        [--files a.py b.py ...] [--checkpoint] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def build(project: str, session: str = "", files: "list[str] | None" = None, checkpoint: bool = False) -> dict:
    """{"rules": [lines], "files": {path: [lines]}, "checkpoint": [lines]}.
    Paths in ``files`` relative to ``project`` are resolved against it."""
    from windvane.hooks import common

    get_project_dir = common.get_project_dir
    load_project_memory = common.load_project_memory
    if session:
        common._session_id = session  # the session's own state file, read only
    if project and os.path.isdir(project):
        os.chdir(project)  # get_project_dir() reads the cwd, as a hook's does
    project_dir = get_project_dir()

    transcript = ""
    if checkpoint:
        try:
            run = common.load_state().get("run")
            transcript = str((run if isinstance(run, dict) else {}).get("transcript_path") or "")
        except Exception:
            transcript = ""
    source = "compact" if checkpoint else "startup"
    work_project, resume_files = common._banner_work_project(project_dir, source, transcript)
    if not checkpoint and session:
        # A brief for a subagent mid-session: the session's own edits name
        # its project, as they do for the compaction banner.
        try:
            work_project = common.session_project(project_dir)
        except Exception:
            work_project = project_dir

    out: dict = {"rules": common._rules_block(work_project), "files": {}, "checkpoint": []}

    seen: set = set()
    for f in files or []:
        p = Path(f)
        if not p.is_absolute():
            p = Path(project or ".") / p
        full = str(p).replace("\\", "/")
        if full in seen:
            continue
        seen.add(full)
        try:
            memory = load_project_memory(get_project_dir(full))
            mistakes = common._file_mistakes(memory, full)
        except Exception:
            mistakes = []
        if mistakes:
            out["files"][f] = common._file_mistake_lines(mistakes, f)

    if checkpoint:
        restored, skipped = common._banner_checkpoint(project_dir, work_project, source, bool(resume_files), transcript)
        if restored:
            out["checkpoint"] = common._format_restored_full(restored, skipped)
    return out


def render(brief: dict) -> str:
    """The plain text: rules, then each file's mistakes, then the checkpoint,
    one blank line between blocks; '' when there is nothing to say."""
    blocks = [brief.get("rules") or []]
    blocks.extend((brief.get("files") or {}).values())
    blocks.append(brief.get("checkpoint") or [])
    return "\n\n".join("\n".join(b) for b in blocks if b)


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m windvane.brief", description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--project", required=True, help="the session's working directory")
    ap.add_argument("--session", default="", help="the Claude Code session id (its state names the work project)")
    ap.add_argument("--files", nargs="*", default=[], help="files whose past mistakes to include")
    ap.add_argument("--checkpoint", action="store_true", help="add the checkpoint the compaction banner restores")
    ap.add_argument("--json", action="store_true", help="print the blocks as JSON lists of lines")
    args = ap.parse_args(argv)
    try:
        brief = build(args.project, args.session, args.files, args.checkpoint)
    except Exception as e:  # never a traceback into a subagent's prompt
        print(f"brief failed: {e}", file=sys.stderr)
        return 1
    text = json.dumps(brief) if args.json else render(brief)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    if text:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

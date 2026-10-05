"""
Where the repo and the run stand, for a checkpoint to record and a restore
to compare against.

A restored checkpoint carries the last session's framing. Before acting on
it the model should know how far the repo moved since: commits landed,
files changed, and whether the files the checkpoint names are among them.
That is a few git calls at save time (the commit, the branch, the checkout)
and two at restore time (bounded, best effort, silent when there is no repo).

The checkout the figures describe is the one the session runs in. A session
that entered a worktree commits on the worktree's branch, while the project
it belongs to (its rules, its ring) is the main checkout; a record saved
there names the worktree as ``repo_path`` and the restore reads git there,
falling back to the project when the worktree is gone.

The active /goal is the other thing a checkpoint should carry: a resumed
session then reads the condition it is working toward without re-mining
the transcript.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from windvane import config

_GIT_TIMEOUT = 4.0


def _git(args: list[str], cwd: str) -> Optional[str]:
    t0 = time.time()
    outcome = ""
    try:
        r = subprocess.run(
            ["git", "--no-optional-locks", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        outcome = f"rc={r.returncode}"
        return r.stdout if r.returncode == 0 else None
    except Exception as e:
        outcome = f"{type(e).__name__}: {str(e)[:120]}"
        return None
    finally:
        _trace(f"git {' '.join(args)} cwd={cwd} {outcome} {time.time() - t0:.2f}s")


def _trace(line: str) -> None:
    """Append one line to the ``git_trace`` file when one is configured: a
    git call that times out inside a long-lived process (the daemon) is
    invisible otherwise -- the caller just sees ''."""
    try:
        p = str(config.knob("git_trace") or "")
    except Exception:
        p = ""
    if not p:
        return
    try:
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} pid={os.getpid()} {line}\n")
    except Exception:
        pass


def head(project_dir: str) -> str:
    """Short HEAD sha, or '' outside a repo."""
    if not project_dir or not Path(project_dir).is_dir():
        return ""
    out = _git(["rev-parse", "--short", "HEAD"], project_dir)
    return (out or "").strip()


def branch(project_dir: str) -> str:
    """The branch HEAD is on; '' when detached or outside a repo."""
    if not project_dir or not Path(project_dir).is_dir():
        return ""
    out = (_git(["rev-parse", "--abbrev-ref", "HEAD"], project_dir) or "").strip()
    return "" if out == "HEAD" else out


def _norm(path: str) -> str:
    try:
        return str(Path(path).resolve()).replace("\\", "/").rstrip("/")
    except Exception:
        return str(path).replace("\\", "/").rstrip("/")


def repo_root(path: str) -> str:
    """The working directory of the repository ``path`` lies in (a worktree is
    its own), normalized; '' outside a repo."""
    if not path or not Path(path).is_dir():
        return ""
    out = (_git(["rev-parse", "--show-toplevel"], path) or "").strip()
    return _norm(out) if out else ""


def session_dir() -> str:
    """The directory the session runs in: CLAUDE_PROJECT_DIR when the caller
    set it (the mod sends the session's working directory; a hook process
    has it from Claude Code), else this process's own cwd."""
    return os.environ.get("CLAUDE_PROJECT_DIR", "").strip() or os.getcwd()


def checkout_for(project_path: str, where: str = "") -> str:
    """The checkout whose git describes the session's work on
    ``project_path``: the repository the session runs in (``where``, else
    ``session_dir()``) when it is the project's own checkout or a worktree
    of it, else the project path. A session that entered a worktree under
    the project commits there, and a record read against the main checkout
    said "no commits" while the branch had nine (2026-10-05)."""
    repo = repo_root(where or session_dir())
    proj = _norm(project_path) if project_path else ""
    if not repo or not proj:
        return project_path
    if repo.lower() == proj.lower() or repo.lower().startswith(proj.lower() + "/"):
        return repo
    try:
        from windvane.paths import worktree_main

        if _norm(worktree_main(repo)).lower() == proj.lower():
            return repo
    except Exception:
        pass
    return project_path


def stamp(entry: dict, project_path: str, where: str = "") -> None:
    """Put where the repo stands on a record: ``commit`` (short HEAD),
    ``branch``, and ``repo_path`` when the session's checkout is not the
    project path itself. Silent outside a repo."""
    checkout = checkout_for(project_path, where) if project_path else ""
    if not checkout:
        return
    sha = head(checkout)
    if sha:
        entry["commit"] = sha
    name = branch(checkout)
    if name:
        entry["branch"] = name
    if _norm(checkout).lower() != _norm(project_path).lower():
        entry["repo_path"] = checkout


def where_of(entry: dict, fallback: str) -> str:
    """The checkout a record's git figures refer to: its ``repo_path`` while
    it still exists (a worktree may have been removed), else ``fallback``."""
    rp = str(entry.get("repo_path") or "")
    if rp and Path(rp).is_dir():
        return rp
    return fallback


def tree(project_dir: str) -> Optional[dict]:
    """Where the working tree stands: {head, branch, modified, added,
    deleted, untracked}. The first thing a resumed session loses is the
    shape of its uncommitted work; one bounded status call gives it back.
    None outside a repo."""
    if not project_dir or not Path(project_dir).is_dir():
        return None
    sha = head(project_dir)
    if not sha:
        return None
    status = _git(["status", "--porcelain"], project_dir)
    if status is None:
        return None
    counts = {"modified": 0, "added": 0, "deleted": 0, "untracked": 0}
    for line in status.splitlines():
        if len(line) < 2:
            continue
        x, y = line[0], line[1]
        if x == "?":
            counts["untracked"] += 1
        elif "D" in (x, y):
            counts["deleted"] += 1
        elif "A" in (x, y):
            counts["added"] += 1
        elif x != " " or y != " ":
            counts["modified"] += 1
    return {"head": sha, "branch": branch(project_dir), **counts}


def tree_text(info: Optional[dict]) -> str:
    """'HEAD abc1234 on main, 3 modified, 1 added' or 'HEAD abc1234 on main,
    clean tree' (no branch named when HEAD is detached); '' outside a repo."""
    if not info or not info.get("head"):
        return ""
    parts = [f"{info[k]} {k}" for k in ("modified", "added", "deleted", "untracked") if info.get(k)]
    at = f"HEAD {info['head']}" + (f" on {info['branch']}" if info.get("branch") else "")
    return f"{at}, " + (", ".join(parts) if parts else "clean tree")


def since(commit: str, project_dir: str, files: Optional[list[str]] = None, saved_at: float = 0.0,
          branch_was: str = "") -> Optional[dict]:
    """How the repo moved since ``commit``:
    {commits, files_changed, touched (checkpoint files among them), missing,
    other_branches, branch_was}. ``saved_at`` (epoch seconds of the
    checkpoint) bounds the other-branches count to commits made after it;
    ``branch_was`` is the branch the record was saved on, named when HEAD is
    on another now. None outside a repo or with no commit to compare."""
    if not commit or not project_dir or not Path(project_dir).is_dir():
        return None
    if _git(["cat-file", "-e", f"{commit}^{{commit}}"], project_dir) is None:
        return {"commits": 0, "files_changed": 0, "touched": [], "missing": True, "tree": tree(project_dir)}
    count = (_git(["rev-list", "--count", f"{commit}..HEAD"], project_dir) or "0").strip()
    # Every local branch, worktrees included: a checkpoint taken on main
    # said "no commits" while nine sat on three worktree branches
    # (2026-09-12). HEAD's own count is subtracted so the two never overlap.
    # Bounded by the checkpoint's time: `--not <commit>` alone counts the
    # whole history of every branch that diverged before it, and a repo
    # with long-lived feature branches read "2,893 commits on other local
    # branches" against a checkpoint from that morning (2026-09-22).
    args = ["rev-list", "--count", "--branches"]
    if saved_at and saved_at > 0:
        args.append(f"--since={int(saved_at)}")
    everywhere = (_git([*args, "--not", commit], project_dir) or "0").strip()
    names = _git(["diff", "--name-only", f"{commit}..HEAD"], project_dir) or ""
    changed = [n.strip().replace("\\", "/") for n in names.splitlines() if n.strip()]
    touched: list[str] = []
    for f in files or []:
        fp = str(f).replace("\\", "/")
        base = fp.rsplit("/", 1)[-1]
        if any(c == fp or c.endswith("/" + base) or fp.endswith("/" + c) or c == base for c in changed):
            touched.append(base)
    try:
        n = int(count)
    except ValueError:
        n = 0
    try:
        elsewhere = max(0, int(everywhere) - n)
    except ValueError:
        elsewhere = 0
    return {"commits": n, "files_changed": len(changed), "touched": touched, "missing": False,
            "other_branches": elsewhere, "branch_was": branch_was, "tree": tree(project_dir)}


def since_text(info: Optional[dict]) -> str:
    """One line for a banner, or '' when there is nothing to say."""
    if not info:
        return ""
    if info.get("missing"):
        return "Since this checkpoint: its commit is not in this history (rewritten or another clone)"
    n, f = int(info.get("commits", 0)), int(info.get("files_changed", 0))
    other = int(info.get("other_branches", 0) or 0)
    if not n and not f:
        line = "Since this checkpoint: no commits on this branch"
    else:
        line = f"Since this checkpoint: {n} commit{'s' if n != 1 else ''}, {f} file{'s' if f != 1 else ''} changed"
        if info.get("touched"):
            line += " -- incl. " + ", ".join(info["touched"][:4])
    if other:
        line += f"; {other} commit{'s' if other != 1 else ''} on other local branches (worktrees included)"
    now = tree_text(info.get("tree"))
    if now:
        line += f"; {now}"
    was, at = str(info.get("branch_was") or ""), str((info.get("tree") or {}).get("branch") or "")
    if was and at and was != at:
        line += f"; the checkpoint was saved on branch {was}"
    return line


def since_for(entry: dict, project_path: str, files: Optional[list[str]] = None) -> Optional[dict]:
    """``since`` for a ring or checkpoint record: its commit, read in the
    checkout it names (``where_of``), bounded by its time, with its branch."""
    try:
        saved = float(entry.get("created") or entry.get("timestamp") or 0.0)
    except (TypeError, ValueError):
        saved = 0.0
    return since(
        str(entry.get("commit") or ""),
        where_of(entry, project_path),
        files,
        saved_at=saved,
        branch_was=str(entry.get("branch") or ""),
    )


def goal_for_session(state: dict) -> str:
    """The active /goal condition: what the goal bracket recorded in the run
    block (``run.goal``, set while a goal runs), else the goal the session's
    transcript shows as still open (goal.scan_goal: a met, failed or cleared
    goal is not active and is not stamped)."""
    _run = state.get("run")
    run: dict = _run if isinstance(_run, dict) else {}
    goal = str(run.get("goal") or "").strip()
    if goal:
        return goal
    tp = str(run.get("transcript_path") or "")
    if not tp or not Path(tp).is_file():
        return ""
    try:
        from windvane.goal import scan_goal

        s = scan_goal(tp)
        if not s.get("active"):
            return ""
        return " ".join(str(s.get("condition") or "").split()).strip()
    except Exception:
        return ""

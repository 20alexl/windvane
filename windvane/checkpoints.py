"""
Checkpoints: the task state that survives a compaction, a crash or the end
of a session.

One construct, two files:

- the RING (``handoff_history.json`` + ``latest_handoff.json`` per project
  directory in the store, and the global ``checkpoints/`` fallback): a capped
  history of deliberate (``manual``) checkpoints plus a ``latest`` pointer
  that automatic entries contend for under a promotion guard, so a trivial
  automatic entry never buries a deliberate one;
- the per-task file (``checkpoints/task_<n>.json``), the full record in the
  checkpoint vocabulary.

Ring vocabulary: a ring record carries ONE name per concept, ``next_steps``,
``files_in_progress``, ``warnings``, ``context_needed``, ``decisions``,
``created``. The checkpoint-side twins (``pending_steps``,
``files_involved``, ``handoff_warnings``, ``handoff_context_needed``,
``key_decisions``, ``timestamp``) are not written to the ring; records that
carry them are read either way. ``task_description`` and ``summary`` are NOT
twins: ``summary`` is the handoff note, a different sentence from the task.

Restore scope: the project's OWN ring, its DESCENDANT project rings, then
its ancestors'; the global ring only when none of those exist. Selection is
by the newest deliberate checkpoint (this session's own first, skipping one
on a branch of the conversation the person rewound past), and the answer
names the store it came from.

Provenance: a save stamps the project's git HEAD (``commit``) and the
session's goal; a restore prints the goal and how far the repo moved since.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from windvane.store import Response, WorkLog

HISTORY_FILENAME = "handoff_history.json"
LATEST_FILENAME = "latest_handoff.json"
DEFAULT_HISTORY_LIMIT = 20
TASK_FILE_KEEP_DAYS = 90
MANUAL_MAX_AGE_HOURS = 14 * 24.0

# The checkpoint-side names a ring record no longer carries.
RING_ALIAS_TWINS = (
    "pending_steps",
    "files_involved",
    "handoff_warnings",
    "handoff_context_needed",
    "key_decisions",
    "timestamp",
)

# Default next steps the automatic writers emit: no real signal.
_TRIVIAL_NEXT_STEPS = {
    "review what was in progress",
    "continue work from before compaction",
    "review context_needed items",
}

# A deliberate checkpoint is always substantive; this dwarfs any content score.
_MANUAL_BONUS = 100


# ---------------------------------------------------------------------------
# Seams to the hook modules (resolved at call time)
# ---------------------------------------------------------------------------


def _hook_attr(name: str):
    """A name from the hook side: ``windvane.events.common`` first (where the
    hooks' shared pieces live), then ``windvane.paths``. Raises
    AttributeError when neither has it."""
    for mod in ("windvane.events.common", "windvane.paths"):
        try:
            module = __import__(mod, fromlist=[name])
        except Exception:
            continue
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(name)


def storage_dir() -> Path:
    from windvane.config import store_dir

    return store_dir()


def session_id() -> str:
    """This process's Claude Code session: the hooks' adopted id, else the
    one Claude Code exports to the plugin's processes."""
    try:
        sid = _hook_attr("_session_id")
        if sid:
            return str(sid)
    except Exception:
        pass
    try:
        adopt = _hook_attr("adopt_env_session_id")
        sid = adopt()
        if sid:
            return str(sid)
    except Exception:
        pass
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()


class session_scope:
    """Point the hook modules' session (``windvane.events.common._session_id``,
    which ``load_state``/``save_state`` read) at ``sid`` for a block, and put
    the previous value back after. A daemon serves many sessions from one
    process, so a tool call must not leave its session behind."""

    def __init__(self, sid: str):
        self.sid = sid
        self._common = None
        self._old = None

    def __enter__(self):
        if not self.sid:
            return self
        try:
            from windvane.events import common

            self._common = common
            self._old = getattr(common, "_session_id", None)
            common._session_id = self.sid
        except Exception:
            self._common = None
        return self

    def __exit__(self, *exc):
        if self._common is not None:
            self._common._session_id = self._old
        return False


def _normalize(path: str) -> str:
    try:
        return _hook_attr("_normalize_path")(path)
    except Exception:
        from windvane.store import MemoryStore

        return MemoryStore._normalize_path(path)


def project_ring_dir(project_dir: str) -> Optional[Path]:
    """The project's ring directory, or None when it is not registered."""
    try:
        return _hook_attr("_project_hash_dir")(project_dir)
    except AttributeError:
        pass
    if not project_dir:
        return None
    try:
        manifest = json.loads((storage_dir() / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    info = (manifest.get("projects") or {}).get(_normalize(project_dir))
    return storage_dir() / "projects" / info["hash"] if info else None


def register_project_ring(project_dir: str) -> Optional[Path]:
    """Register ``project_dir`` in the store and return its ring directory.
    A project's first checkpoint comes before anything is remembered about
    it, and a record filed in the global folder alone is not where a
    project-scoped restore looks first. None when the store cannot be
    opened."""
    if not project_dir:
        return None
    try:
        from windvane.store import MemoryStore

        return MemoryStore().register_project(project_dir)
    except Exception:
        return None


def global_ring_dir() -> Path:
    try:
        return _hook_attr("_global_handoff_dir")()
    except AttributeError:
        return storage_dir() / "checkpoints"


def candidate_dirs(project_dir: str = "") -> list:
    """The rings a restore reads (own, descendants, ancestors; global as fallback)."""
    try:
        return list(_hook_attr("_handoff_candidate_dirs")(project_dir))
    except AttributeError:
        pass
    storage = storage_dir()
    try:
        projects = json.loads((storage / "manifest.json").read_text(encoding="utf-8")).get("projects", {})
    except Exception:
        projects = {}
    dirs: list = []
    seen: set = set()

    def _add(info) -> None:
        if info and info["hash"] not in seen:
            dirs.append(storage / "projects" / info["hash"])
            seen.add(info["hash"])

    if project_dir:
        norm = _normalize(project_dir)
        _add(projects.get(norm))
        for path, info in sorted(projects.items(), key=lambda kv: kv[0].count("/")):
            if path.startswith(norm.rstrip("/") + "/"):
                _add(info)
        p = Path(norm)
        while True:
            _add(projects.get(_normalize(str(p))))
            if p.parent == p:
                break
            p = p.parent
    if not dirs:
        dirs.append(storage / "checkpoints")
    return dirs


# ---------------------------------------------------------------------------
# The ring
# ---------------------------------------------------------------------------


def _created_ts(h: dict) -> float:
    """A record's time, tolerant of the older ``created_at`` and ``timestamp``."""
    try:
        return float(h.get("created", h.get("created_at", h.get("timestamp", 0))) or 0)
    except (TypeError, ValueError):
        return 0.0


def handoff_signal(h: dict) -> int:
    """How substantive a record is. 0 = a trivial automatic entry; a
    deliberate one always scores high."""
    if not h:
        return -1
    score = 0
    score += len(h.get("files_in_progress") or h.get("files_involved") or [])
    score += 2 * len(h.get("decisions") or h.get("key_decisions") or [])
    score += len(h.get("context_needed") or h.get("handoff_context_needed") or [])
    score += len(h.get("warnings") or h.get("handoff_warnings") or [])
    score += len(h.get("mistakes") or [])
    real_next = [
        s
        for s in (h.get("next_steps") or h.get("pending_steps") or [])
        if s and str(s).strip().lower() not in _TRIVIAL_NEXT_STEPS
    ]
    score += 2 * len(real_next)
    if h.get("kind") == "manual":
        score += _MANUAL_BONUS
    return score


def is_trivial_auto(h: dict) -> bool:
    return h.get("kind") != "manual" and handoff_signal(h) <= 0


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_history(dir_path: Path) -> list:
    data = _read_json(dir_path / HISTORY_FILENAME)
    if isinstance(data, dict):
        return data.get("handoffs", []) or []
    if isinstance(data, list):
        return data
    return []


def _should_promote(new: dict, existing: Optional[dict], stale_hours: float = 24.0) -> bool:
    """Should ``new`` replace the ``latest`` pointer? Always when there is
    none or ``new`` is deliberate; otherwise when it is at least as
    substantive, or the existing one is older than ``stale_hours``."""
    if not existing:
        return True
    if new.get("kind") == "manual":
        return True
    if handoff_signal(new) >= handoff_signal(existing):
        return True
    return (time.time() - _created_ts(existing)) / 3600 > stale_hours


def write_handoff(
    handoff: dict,
    target_dirs: Sequence[Optional[Path]],
    *,
    history_limit: int = DEFAULT_HISTORY_LIMIT,
    stale_hours: float = 24.0,
) -> dict:
    """Persist a record to each target ring. A deliberate (``manual``)
    record appends to the history AND contends for the ``latest`` pointer;
    an automatic one only contends for the pointer (a per-turn automatic
    entry in the history would evict real checkpoints from the capped ring).
    A trivial automatic record is skipped entirely. ``None`` dirs are
    ignored. Returns {"skipped", "appended", "promoted"}."""
    handoff.setdefault("created", time.time())
    handoff.setdefault("kind", "auto")
    report: dict = {"skipped": False, "appended": [], "promoted": []}
    if is_trivial_auto(handoff):
        report["skipped"] = True
        return report
    for d in target_dirs:
        if d is None:
            continue
        history = _load_history(d)
        history_changed = False
        # Seed the history from a single-slot record written before the ring
        # existed, so nothing that was there is silently dropped.
        if not history:
            seed = _read_json(d / LATEST_FILENAME)
            if seed:
                history = [seed]
                history_changed = True
        if handoff.get("kind") == "manual":
            history.append(handoff)
            history_changed = True
        if len(history) > history_limit:
            history = history[-history_limit:]
            history_changed = True
        if history_changed:
            _atomic_write(d / HISTORY_FILENAME, {"handoffs": history})
            report["appended"].append(str(d))
        if _should_promote(handoff, _read_json(d / LATEST_FILENAME), stale_hours=stale_hours):
            _atomic_write(d / LATEST_FILENAME, handoff)
            report["promoted"].append(str(d))
    return report


def prune_task_files(storage: Path, keep_days: int = TASK_FILE_KEEP_DAYS) -> list:
    """Remove the per-task files (``checkpoints/task_*.json``) older than
    ``keep_days`` that no ring names any more. Every ring keeps its newest
    records whole, so a task file is the last copy only while a ring still
    points at it. The miner's hygiene pass calls this. Returns the removed
    paths."""
    ck = storage / "checkpoints"
    if not ck.is_dir():
        return []
    referenced: set = set()
    ring_files = list((storage / "projects").glob("*/" + HISTORY_FILENAME)) + list(
        (storage / "projects").glob("*/" + LATEST_FILENAME)
    )
    ring_files += [ck / HISTORY_FILENAME, ck / LATEST_FILENAME]
    for rf in ring_files:
        data = _read_json(rf)
        if isinstance(data, dict) and "handoffs" in data:
            entries = data.get("handoffs", [])
        else:
            entries = [data] if isinstance(data, dict) else []
        for h in entries:
            if isinstance(h, dict) and h.get("task_id"):
                referenced.add(str(h["task_id"]))
    cutoff = time.time() - keep_days * 86400
    removed = []
    for f in sorted(ck.glob("task_*.json")):
        try:
            if f.stem in referenced or f.stat().st_mtime >= cutoff:
                continue
            f.unlink()
            removed.append(f)
        except Exception:
            continue
    return removed


def read_history(candidate: Sequence[Optional[Path]], *, limit: int = DEFAULT_HISTORY_LIMIT) -> list:
    """The merged history of the candidate rings, newest first, deduplicated
    by (time, summary opening); each ring's ``latest`` pointer is folded in."""
    seen = set()
    out = []
    for d in candidate:
        if d is None:
            continue
        entries = list(_load_history(d))
        ptr = _read_json(d / LATEST_FILENAME)
        if ptr:
            entries.append(ptr)
        for h in entries:
            if not h:
                continue
            key = (round(_created_ts(h), 3), (h.get("summary") or "")[:80])
            if key in seen:
                continue
            seen.add(key)
            out.append(h)
    out.sort(key=_created_ts, reverse=True)
    return out[:limit]


def read_latest(candidate: Sequence[Optional[Path]], *, max_age_hours: Optional[float] = None) -> Optional[dict]:
    """The newest DELIBERATE record across the candidate rings, else the
    newest of any kind. ``max_age_hours`` bounds automatic records; a
    deliberate one gets at least 14 days."""
    hist = read_history(candidate)
    if max_age_hours is not None:
        manual_cut = max(float(max_age_hours), MANUAL_MAX_AGE_HOURS)

        def _fresh(h: dict) -> bool:
            age_h = (time.time() - _created_ts(h)) / 3600
            return age_h <= (manual_cut if h.get("kind") == "manual" else max_age_hours)

        hist = [h for h in hist if _fresh(h)]
    if not hist:
        return None
    manual = [h for h in hist if h.get("kind") == "manual"]
    return manual[0] if manual else hist[0]


def read_ordered(candidate: Sequence[Optional[Path]], *, limit: int = DEFAULT_HISTORY_LIMIT) -> list:
    """The history for listing and index access: the record a restore
    returns first (index 0), then the rest newest first."""
    hist = read_history(candidate, limit=limit)
    latest = read_latest(candidate)
    if not latest:
        return hist

    def _key(h: dict):
        return (round(_created_ts(h), 3), (h.get("summary") or "")[:80])

    lk = _key(latest)
    return ([latest] + [h for h in hist if _key(h) != lk])[:limit]


def get_by_index(candidate: Sequence[Optional[Path]], index: int, *, limit: int = DEFAULT_HISTORY_LIMIT) -> Optional[dict]:
    ordered = read_ordered(candidate, limit=limit)
    if 0 <= index < len(ordered):
        return ordered[index]
    return None


# ---------------------------------------------------------------------------
# ContextGuard: save, restore, list, verify
# ---------------------------------------------------------------------------


@dataclass
class TaskCheckpoint:
    """A saved checkpoint of task state."""

    task_id: str
    task_description: str
    current_step: str
    completed_steps: list
    pending_steps: list
    files_involved: list
    key_decisions: list
    blockers: list
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)
    handoff_summary: str = ""
    handoff_context_needed: list = field(default_factory=list)
    handoff_warnings: list = field(default_factory=list)


class ContextGuard:
    """Saves and restores checkpoints, lists the ring, verifies a claimed
    completion."""

    def __init__(self, storage_dir: Optional[Path] = None):
        if storage_dir is None:
            storage_dir = globals()["storage_dir"]() / "checkpoints"
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._current_checkpoint: Optional[TaskCheckpoint] = None
        self._claimed_completions: list = []
        self._actual_verifications: list = []

    def _task_id(self) -> str:
        """``task_<epoch seconds>``, bumped past an existing file so two saves
        in one second never share a task file."""
        n = int(time.time())
        while (self.storage_dir / f"task_{n}.json").exists():
            n += 1
        return f"task_{n}"

    def save_checkpoint(
        self,
        task_description: str,
        current_step: str,
        completed_steps: list,
        pending_steps: list,
        files_involved: list,
        key_decisions: Optional[list] = None,
        blockers: Optional[list] = None,
        project_path: Optional[str] = None,
        handoff_summary: Optional[str] = None,
        handoff_context_needed: Optional[list] = None,
        handoff_warnings: Optional[list] = None,
        drafted_fields: Optional[list] = None,
    ) -> Response:
        """Save a checkpoint (the task file and a deliberate ring record).

        ``drafted_fields`` names the arguments taken from the recorder's
        draft (``windvane.draft``), recorded as ``metadata.drafted_fields``.
        The handoff fields (summary, context needed, warnings) carry the note
        for the next session in the same call."""
        work_log = WorkLog()
        work_log.what_i_tried.append("saving task checkpoint")
        task_id = self._task_id()
        metadata: dict = {"project_path": project_path} if project_path else {}
        if drafted_fields:
            metadata["drafted_fields"] = list(drafted_fields)
        checkpoint = TaskCheckpoint(
            task_id=task_id,
            task_description=task_description,
            current_step=current_step,
            completed_steps=list(completed_steps or []),
            pending_steps=list(pending_steps or []),
            files_involved=list(files_involved or []),
            key_decisions=list(key_decisions or []),
            blockers=list(blockers or []),
            metadata=metadata,
            handoff_summary=handoff_summary or "",
            handoff_context_needed=list(handoff_context_needed or []),
            handoff_warnings=list(handoff_warnings or []),
        )
        self._current_checkpoint = checkpoint
        checkpoint_file = self.storage_dir / f"{task_id}.json"
        checkpoint_data = {
            "task_id": checkpoint.task_id,
            "task_description": checkpoint.task_description,
            "current_step": checkpoint.current_step,
            "completed_steps": checkpoint.completed_steps,
            "pending_steps": checkpoint.pending_steps,
            "files_involved": checkpoint.files_involved,
            "key_decisions": checkpoint.key_decisions,
            "blockers": checkpoint.blockers,
            "timestamp": checkpoint.timestamp,
            "metadata": checkpoint.metadata,
            "handoff_summary": checkpoint.handoff_summary,
            "handoff_context_needed": checkpoint.handoff_context_needed,
            "handoff_warnings": checkpoint.handoff_warnings,
        }
        # Where the repo and the run stood: the commit lets a restore say how
        # far the repo moved since; the goal lets a resumed session read what
        # it was working toward.
        try:
            from windvane import repo_state as _rs

            _commit = _rs.head(project_path) if project_path else ""
            if _commit:
                checkpoint_data["commit"] = _commit
            _goal = _rs.goal_for_session(_hook_attr("load_state")())
            if _goal:
                checkpoint_data["goal"] = str(_goal)[:500]
        except Exception:
            pass

        temp_file = checkpoint_file.with_suffix(".json.tmp")
        temp_file.write_text(json.dumps(checkpoint_data, indent=2), encoding="utf-8")
        temp_file.replace(checkpoint_file)

        ring = {k: v for k, v in checkpoint_data.items() if k not in RING_ALIAS_TWINS}
        ring["kind"] = "manual"
        ring["created"] = checkpoint.timestamp
        if project_path:
            ring["project_path"] = project_path
        sid = session_id()
        if sid:
            ring["session_id"] = sid
        ring["summary"] = handoff_summary or task_description
        ring["files_in_progress"] = checkpoint.files_involved
        ring["next_steps"] = checkpoint.pending_steps
        ring["context_needed"] = checkpoint.handoff_context_needed
        ring["warnings"] = checkpoint.handoff_warnings
        ring["decisions"] = checkpoint.key_decisions
        filed_to = ""
        try:
            proj_dir = project_ring_dir(project_path) if project_path else None
            if project_path and proj_dir is None:
                proj_dir = register_project_ring(project_path)
            write_handoff(ring, [proj_dir, global_ring_dir()])
            # Only a store that could not be opened leaves a project without
            # a ring of its own: worth saying, since a project-scoped restore
            # does not look in the global folder first.
            filed_to = Path(project_path).name if (project_path and proj_dir) else "global checkpoints"
        except Exception:
            pass

        # A deliberate save resets the turns-since-checkpoint cadence.
        try:
            from windvane import pressure as _p

            _st = _hook_attr("load_state")()
            _p.note_manual_checkpoint(_st)
            _hook_attr("save_state")(_st)
        except Exception:
            pass

        if handoff_summary or pending_steps:
            try:
                self._write_handoff_md(
                    summary=handoff_summary or task_description,
                    next_steps=list(pending_steps or []),
                    context_needed=list(handoff_context_needed or []),
                    warnings=list(handoff_warnings or []),
                    project_path=project_path,
                )
            except Exception:
                pass

        work_log.what_worked.append(f"checkpoint saved: {task_id}")
        has_handoff = bool(handoff_summary or handoff_context_needed or handoff_warnings)
        # Raw counts, not a percentage: each save re-derives its step lists,
        # so a percentage can fall while the work progresses.
        reasoning = (
            f"Checkpoint saved: {len(checkpoint.completed_steps)} steps done, "
            f"{len(checkpoint.pending_steps)} remaining."
        )
        if filed_to:
            reasoning += f" Filed under {filed_to}."
        if has_handoff:
            reasoning += " Includes handoff info for next session."
        suggestions = ["checkpoint(restore) at the next session start continues from here"]
        if not has_handoff:
            suggestions.append("Add handoff_summary for clearer session transitions")
        return Response(
            status="success",
            confidence="high",
            reasoning=reasoning,
            work_log=work_log,
            data={
                "task_id": task_id,
                "checkpoint_file": str(checkpoint_file),
                "filed_to": filed_to,
                "completed": len(checkpoint.completed_steps),
                "pending": len(checkpoint.pending_steps),
                "has_handoff": has_handoff,
            },
            suggestions=suggestions,
        )

    def restore_checkpoint(self, task_id: Optional[str] = None, project_path: Optional[str] = None, index: int = 0) -> Response:
        """Restore task state. With no ``task_id``: this session's newest
        deliberate checkpoint on the live branch, else the scope's newest
        deliberate one; ``index > 0`` reaches an older ring record."""
        work_log = WorkLog()
        work_log.what_i_tried.append("restoring checkpoint")
        data = None
        rewound: list = []
        if not task_id:
            try:
                dirs = candidate_dirs(project_path or "")
                if index and index > 0:
                    data = get_by_index(dirs, index)
                else:
                    try:
                        from windvane import transcript as _tc

                        sid = session_id()
                        tp = _tc.transcript_for_session(sid) if sid else None
                        data, rewound = _hook_attr("_own_session_checkpoint")(dirs, sid, str(tp or ""))
                    except Exception:
                        data, rewound = None, []
                    if data is None:
                        data = read_latest(dirs)
            except Exception:
                data = None

        # An explicit index addresses the ring only, never the legacy file.
        if data is None and index and index > 0:
            return Response(
                status="not_found",
                confidence="high",
                reasoning=f"No checkpoint at index {index}; checkpoint(list) shows how many exist",
                work_log=work_log,
            )

        if data is None:
            checkpoint_file = None
            if task_id:
                checkpoint_file = self.storage_dir / f"{task_id}.json"
            elif project_path:
                pdir = project_ring_dir(project_path)
                if pdir is not None and (pdir / "latest_checkpoint.json").exists():
                    checkpoint_file = pdir / "latest_checkpoint.json"
            if not checkpoint_file:
                checkpoint_file = self.storage_dir / "latest_checkpoint.json"
            if not checkpoint_file.exists():
                return Response(
                    status="not_found",
                    confidence="high",
                    reasoning="No checkpoint found to restore",
                    work_log=work_log,
                    suggestions=["checkpoint(save) when a task begins"],
                )
            data = json.loads(checkpoint_file.read_text(encoding="utf-8"))

        age_hours = (time.time() - _created_ts(data)) / 3600
        work_log.what_worked.append(f"checkpoint restored from {age_hours:.1f} hours ago")
        warnings = []
        if age_hours > 24:
            warnings.append(f"Checkpoint is {age_hours:.0f} hours old - verify it's still relevant")
        for _r in rewound:
            warnings.append(
                f"Skipped {_r.get('task_id', '?')} \"{str(_r.get('task_description') or _r.get('summary') or '')[:80]}\": "
                "saved by this session on a branch of the conversation that was rewound past; "
                "the checkpoint above is the newest on the live branch"
            )

        # Provenance: the winning record can belong to another sub-project
        # than the one asked from; say which store it came from.
        _entry_project = data.get("project_path") or (data.get("metadata") or {}).get("project_path") or ""
        _from = Path(_entry_project).name if _entry_project else ""
        if _entry_project and project_path:
            try:
                if _normalize(_entry_project) != _normalize(project_path):
                    warnings.append(
                        f"Restored from {_from or _entry_project}; you asked from "
                        f"{Path(project_path).name}. The newest deliberate checkpoint "
                        f"in scope wins; checkpoint(list) shows the rest."
                    )
            except Exception:
                pass

        _completed = data.get("completed_steps", []) or []
        _pending = data.get("pending_steps", []) or data.get("next_steps", []) or []
        _has_task_state = bool(data.get("task_description") or data.get("current_step") or _completed)
        headline = data.get("task_description") or data.get("summary", "Unknown")
        summary_lines = [f"**{'Task' if _has_task_state else 'Handoff'}:** {headline}"]
        if _from:
            summary_lines.append(f"**From:** {_from} · {age_hours:.1f}h ago")
        if data.get("goal"):
            summary_lines.append(f"**Goal:** {data['goal']}")
        try:
            from windvane import repo_state as _rs

            _pp = data.get("project_path") or (data.get("metadata") or {}).get("project_path") or project_path or ""
            _since = _rs.since_text(
                _rs.since(
                    str(data.get("commit") or ""),
                    str(_pp),
                    data.get("files_in_progress") or data.get("files_involved") or [],
                    saved_at=_created_ts(data),
                )
            )
            if _since:
                summary_lines.append(f"**{_since}**")
        except Exception:
            pass
        if data.get("current_step"):
            summary_lines.append(f"**Current step:** {data['current_step']}")
        if _completed or _pending:
            summary_lines.append(f"**Progress:** {len(_completed)}/{len(_completed) + len(_pending)} steps")
        if data.get("blockers"):
            summary_lines.append(f"**Blockers:** {', '.join(data['blockers'])}")
        _decisions = data.get("key_decisions") or data.get("decisions") or []
        if _decisions:
            summary_lines.append(f"**Key decisions:** {len(_decisions)} recorded")

        handoff_summary = data.get("handoff_summary")
        handoff_context = data.get("handoff_context_needed", []) or data.get("context_needed", []) or []
        handoff_warnings = data.get("handoff_warnings", []) or data.get("warnings", []) or []
        if handoff_summary and handoff_summary != headline and handoff_summary != data.get("current_step"):
            summary_lines.extend(["", "## Handoff note:", handoff_summary])
        if handoff_context:
            summary_lines.append(f"**Context needed:** {', '.join(handoff_context[:3])}")
        warnings.extend(handoff_warnings)

        _current = data.get("current_step")
        _pending_sug = [s for s in _pending if s and s != _current]
        suggestions = []
        _note_shown = bool(handoff_summary) and handoff_summary != headline and handoff_summary != _current
        if not _current and not _note_shown:
            suggestions.append(f"Continue with: {data.get('summary', 'previous work')}")
        if _pending_sug:
            suggestions.append(
                f"Remaining steps: {', '.join(_pending_sug[:3])}{'...' if len(_pending_sug) > 3 else ''}"
            )
        # The record's fields already rendered above are left out of the
        # data dump, so the model reads each once.
        _shown = {"task_description", "current_step", "handoff_warnings", "warnings"}
        if _note_shown or handoff_summary == headline:
            _shown |= {"handoff_summary", "summary"}
        return Response(
            status="success",
            confidence="high",
            reasoning="\n".join(summary_lines),
            work_log=work_log,
            data={k: v for k, v in data.items() if k not in _shown},
            warnings=warnings,
            suggestions=suggestions,
        )

    def list_checkpoints(self, project_path: str = "") -> Response:
        """The ring, index 0 first (what restore returns), then newest first,
        with age, kind and a summary; ``restore`` with ``index=N`` reaches one."""
        work_log = WorkLog()
        work_log.what_i_tried.append("listing checkpoints")
        try:
            hist = read_ordered(candidate_dirs(project_path))
        except Exception:
            hist = []
        if not hist:
            return Response(status="not_found", confidence="high", reasoning="No checkpoints recorded yet", work_log=work_log)
        items = []
        for i, h in enumerate(hist):
            age_h = (time.time() - _created_ts(h)) / 3600
            kind = h.get("kind", "auto")
            summary = (h.get("summary", "") or h.get("task_description", "") or "")[:80]
            # The ring merges the project's own records with its ancestors'
            # and descendants', so each line names its project.
            where = Path(str(h.get("project_path") or "")).name
            items.append(f"[{i}] ({kind}, {age_h:.0f}h{', ' + where if where else ''}) {summary}")
        return Response(
            status="success",
            confidence="high",
            reasoning=f"{len(hist)} checkpoint(s) in history (checkpoint(restore) with index=N to retrieve):",
            work_log=work_log,
            data={"checkpoints": items},
        )

    def verify_completion(self, task: str, verification_steps: list, evidence: Optional[list] = None) -> Response:
        """Check a claimed completion: evidence files exist, and each step is
        verified where it can be (file existence) or marked for a manual check."""
        work_log = WorkLog()
        work_log.what_i_tried.append("verifying task completion")
        claim = {"task": task, "evidence": evidence or [], "timestamp": time.time(), "verified": False}
        self._claimed_completions.append(claim)

        evidence_results = []
        for ev in evidence or []:
            if "/" in ev or "\\" in ev or ev.endswith((".py", ".js", ".ts", ".md", ".json", ".yaml", ".yml")):
                if Path(ev).exists():
                    evidence_results.append({"item": ev, "status": "FOUND", "valid": True})
                else:
                    evidence_results.append({"item": ev, "status": "NOT FOUND", "valid": False})
            else:
                evidence_results.append({"item": ev, "status": "noted", "valid": True})
        step_results = [self._verify_step(step) for step in verification_steps]

        evidence_failed = sum(1 for r in evidence_results if not r["valid"])
        steps_passed = sum(1 for r in step_results if r["status"] == "passed")
        steps_failed = sum(1 for r in step_results if not r["valid"])
        steps_manual = sum(1 for r in step_results if r["status"] == "manual")
        all_passed = evidence_failed == 0 and steps_failed == 0
        self._actual_verifications.append(
            {"task": task, "steps": verification_steps, "timestamp": time.time(), "passed": all_passed}
        )

        lines = ["## Completion Verification", f"**Task:** {task}", ""]
        if evidence_results:
            lines.append("**Evidence check:**")
            for r in evidence_results:
                lines.append(f"  {r['status']} {r['item']}")
            lines.append("")
        lines.append("**Verification steps:**")
        for r in step_results:
            if r["status"] == "passed":
                lines.append(f"  PASS: {r['step']}")
            elif r["status"] == "failed":
                lines.append(f"  FAIL: {r['step']} - {r.get('reason', 'failed')}")
            else:
                lines.append(f"  TODO: {r['step']} (needs manual check)")
        lines.append("")
        if all_passed and steps_manual == 0:
            lines.append("**Result: ALL CHECKS PASSED**")
            claim["verified"] = True
            status = "success"
        elif all_passed:
            lines.append(f"**Result: {steps_manual} steps need manual verification**")
            status = "success"
        else:
            lines.append(f"**Result: VERIFICATION FAILED** ({evidence_failed + steps_failed} checks failed)")
            status = "failed"
        work_log.what_worked.append(
            f"verified {len(verification_steps)} steps: {steps_passed} passed, {steps_failed} failed, {steps_manual} manual"
        )
        warnings = []
        if steps_manual > 0:
            warnings.append(f"{steps_manual} steps require manual verification")
        if evidence_failed > 0:
            warnings.append(f"{evidence_failed} evidence files not found!")
        return Response(
            status=status,
            confidence="high" if steps_manual == 0 else "medium",
            reasoning="\n".join(lines),
            work_log=work_log,
            data={
                "task": task,
                "verification_steps": verification_steps,
                "evidence": evidence or [],
                "evidence_results": evidence_results,
                "step_results": step_results,
                "all_passed": all_passed,
                "needs_manual": steps_manual > 0,
            },
            warnings=warnings,
        )

    def _verify_step(self, step: str) -> dict:
        """One step: a file-existence claim is checked; anything else is manual."""
        import re

        step_lower = step.lower()
        file_patterns = [
            "file exists", "file created", "created file", "added file",
            "exists at", "saved to", "wrote to", "created at",
        ]
        if any(p in step_lower for p in file_patterns):
            path_match = re.search(r'["\']?([a-zA-Z0-9_/\\.-]+\.[a-zA-Z0-9]+)["\']?', step)
            if path_match:
                path = Path(path_match.group(1))
                if path.exists():
                    return {"step": step, "status": "passed", "valid": True}
                return {"step": step, "status": "failed", "valid": False, "reason": f"file not found: {path}"}
        return {"step": step, "status": "manual", "valid": True}

    def _write_handoff_md(
        self,
        summary: str,
        next_steps: list,
        context_needed: Optional[list] = None,
        warnings: Optional[list] = None,
        project_path: Optional[str] = None,
    ) -> Path:
        """The human-readable HANDOFF.md: a global mirror, plus a copy beside
        the project's ring when the project is registered (so a workspace with
        several projects never shows the wrong one's). Returns the most
        specific path written."""
        md_lines = ["# Session Handoff", f"*Created: {time.strftime('%Y-%m-%d %H:%M')}*"]
        if project_path:
            md_lines.append(f"**Project:** {project_path}")
        md_lines += ["", "## Summary", summary, "", "## Next Steps"]
        for i, step in enumerate(next_steps, 1):
            md_lines.append(f"{i}. {step}")
        if context_needed:
            md_lines.extend(["", "## Context Needed"] + [f"- {c}" for c in context_needed])
        if warnings:
            md_lines.extend(["", "## Warnings"] + [f"- {w}" for w in warnings])
        text = "\n".join(md_lines)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        global_md = self.storage_dir / "HANDOFF.md"
        global_md.write_text(text, encoding="utf-8")
        written = global_md
        if project_path:
            try:
                proj_dir = project_ring_dir(project_path)
                if proj_dir is not None:
                    proj_dir.mkdir(parents=True, exist_ok=True)
                    proj_md = proj_dir / "HANDOFF.md"
                    proj_md.write_text(text, encoding="utf-8")
                    written = proj_md
            except Exception:
                pass
        return written

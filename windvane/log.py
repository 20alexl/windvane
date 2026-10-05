"""
The work log: mistakes and decisions the model records by hand.

Most mistakes are captured by the hooks (every failed tool call) and most
decisions by the prompt hook and the miner. This is the deliberate path for
what those miss: ``log(mistake)`` stores a ``mistake`` entry at relevance 9
(written with intent, so never auto-archived), and ``log(decision)`` stores
a ``decision`` entry with its reason and the alternatives considered.

A mistake reaches the store only when a project is known; without one it
stays in this tracker for the process and the caller is told so.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from windvane.store import Response, WorkLog

EVENTS_KEEP = 200


@dataclass
class WorkEvent:
    """One event in the current session."""

    event_type: str  # "edit", "search", "error", "decision"
    description: str
    file_path: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)


class WorkTracker:
    """What the model did this session: searches, mistakes, decisions."""

    def __init__(self, memory):
        self.memory = memory
        self._events: list = []
        self._session_start_time: float = 0
        self._current_project: Optional[str] = None
        self._mistakes: list = []

    def start_session(self, project_path: str):
        """Begin tracking work for a project."""
        self._events = []
        self._session_start_time = time.time()
        self._current_project = project_path
        self._mistakes = []

    def set_project(self, project_path: str):
        """Point the tracker at a project without resetting its events (the
        newest ``EVENTS_KEEP`` stay, so a long-lived daemon's tracker is bounded)."""
        if project_path:
            self._current_project = project_path
        self._events = self._events[-EVENTS_KEEP:]
        self._mistakes = self._mistakes[-EVENTS_KEEP:]

    def log_search(self, query: str, results_count: int, directory: str):
        self._events.append(
            WorkEvent(
                event_type="search",
                description=f"Searched for '{query}' - found {results_count} results",
                file_path=directory,
                metadata={"query": query, "results_count": results_count},
            )
        )

    def log_mistake(self, description: str, file_path: Optional[str] = None, how_to_avoid: Optional[str] = None) -> bool:
        """Record a mistake; True when it reached the project's memory, False
        when no project is known yet (session-only)."""
        self._mistakes.append(
            {"description": description, "file_path": file_path, "how_to_avoid": how_to_avoid, "timestamp": time.time()}
        )
        self._events.append(
            WorkEvent(event_type="error", description=description, file_path=file_path, metadata={"how_to_avoid": how_to_avoid})
        )
        if not self._current_project:
            return False
        kwargs = {}
        if file_path:
            kwargs["related_files"] = [file_path]
        self.memory.remember_discovery(
            self._current_project,
            f"MISTAKE: {description}" + (f" - Fix: {how_to_avoid}" if how_to_avoid else ""),
            source="work_tracker",
            relevance=9,
            category="mistake",
            **kwargs,
        )
        return True

    def log_decision(self, decision: str, reason: str, alternatives_considered: Optional[list] = None) -> bool:
        """Record a decision and why; True when it reached the project's memory."""
        description = f"{decision} - Reason: {reason}"
        self._events.append(
            WorkEvent(
                event_type="decision",
                description=description,
                metadata={"decision": decision, "reason": reason, "alternatives": alternatives_considered or []},
            )
        )
        if not self._current_project:
            return False
        alts = [a for a in (alternatives_considered or []) if str(a).strip()]
        content = f"DECISION: {description}" + (f" (Alternatives: {', '.join(alts)})" if alts else "")
        self.memory.remember_discovery(
            self._current_project,
            content,
            source="work_tracker",
            relevance=7,
            category="decision",
        )
        return True

    def get_relevant_context(self, file_path: str) -> Response:
        """This session's events and the stored mistakes that touch a file."""
        work_log = WorkLog()
        work_log.what_i_tried.append(f"finding context for {Path(file_path).name}")
        relevant = [
            {"type": e.event_type, "description": e.description, "when": e.timestamp}
            for e in self._events
            if e.file_path and self._paths_related(e.file_path, file_path)
        ]
        past_context = []
        if self._current_project:
            project = self.memory.recall(project_path=self._current_project).get("project") or {}
            for disc in project.get("discoveries", []):
                content = disc.get("content", "")
                if "MISTAKE" in content and Path(file_path).name in content:
                    past_context.append(content)
        warnings = []
        if past_context:
            warnings.append(f"Found {len(past_context)} past issues with this file!")
            warnings.extend(f"  - {c}" for c in past_context[:3])
        if relevant or past_context:
            reasoning = (
                f"Found {len(relevant)} session events and {len(past_context)} past mistakes for {Path(file_path).name}"
            )
        else:
            reasoning = f"No history for {Path(file_path).name} yet"
        work_log.what_worked.append(f"found {len(relevant)} relevant events")
        return Response(
            status="success",
            confidence="medium" if (relevant or past_context) else "low",
            reasoning=reasoning,
            work_log=work_log,
            data={"current_session_events": relevant, "past_mistakes": past_context},
            warnings=warnings,
        )

    def _paths_related(self, path1: str, path2: str) -> bool:
        p1, p2 = Path(path1), Path(path2)
        return p1 == p2 or p1.name == p2.name or p1.parent == p2.parent

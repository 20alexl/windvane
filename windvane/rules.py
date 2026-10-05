"""
Rules: the entries that always apply, the detectors that watch them, and
the pack windvane seeds.

A rule is a memory entry of category ``rule``. It is never archived or
decayed, it is shown at session start and before a compaction, and a rule
written at a workspace root binds every project under it. A rule may carry a
hand-written detector (tool names, a command regex, path globs, an input
regex; see ``windvane.compliance``): every matching tool call is recorded,
and the rule is injected before a matching shell command runs. A rule
without one is advisory.

The pack has two tiers:

- the default tier (16 rules), seeded into a project once, on its first
  fresh session, unless ``"default_rules": false``;
- the strict tier (11 rules), seeded when asked
  (``seed_rules(project, strict=True)``, ``"strict_pack": true``, or
  ``python -m windvane.rules seed --project DIR --strict``).

Seeding skips any rule the project or an ancestor already has in substance
(word overlap, its opening, or an anchor phrase), so a project that wrote
its own rules keeps them and gets no duplicates; a covering rule without a
detector adopts the pack rule's detector.

    python -m windvane.rules seed --project DIR [--strict]
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Optional

PACK_VERSION = 1

# Detectors (windvane.compliance) for the pack rules a regex can watch.
# Hand-written and shipped with the rule. ``unattended: deny`` refuses the
# call when nobody is there to ask (bypass / auto / dontAsk permission modes).
DESTRUCTIVE_DETECTOR = {
    "tools": ["Bash", "PowerShell"],
    "command": (
        r"(?:^|[;&|(]\s*|\bsudo\s+|\bxargs\s+(?:-\S+\s+)*)rm\s+(?!--cached\b)(?!--help\b)"
        r"|\bgit\s+rm\s+(?!--cached\b)"
        r"|\bRemove-Item\b[^|;\n]*(?:-Recurse|-Force)"
        r"|\brmdir\s+/s\b|\bdel\s+/[sq]\b|\brd\s+/s\b"
        r"|\bgit\s+(?:reset\s+--hard|clean\s+-[a-zA-Z]*[fx]|branch\s+-D|checkout\s+--\s|restore\s+--staged|stash\s+drop|filter-branch|filter-repo)"
        r"|\bgit\s+push\b[^|;\n]*(?:--force\b|-f\b|--force-with-lease)"
        r"|\bDROP\s+(?:TABLE|DATABASE|SCHEMA|INDEX)\b|\bTRUNCATE\s+TABLE\b"
        r"|\bformat\s+[a-zA-Z]:|\bmkfs\b|\bdd\s+if=|\bshred\b"
        r"|\btaskkill\b|\bStop-Process\b|\bpkill\b|\bkillall\b|\bkill\s+-9\b"
    ),
    "note": "destructive shell: recursive/forced delete, hard reset, force-push, DROP/TRUNCATE, disk format, kill",
    "unattended": "deny",
}
KILL_BY_NAME_DETECTOR = {
    "tools": ["Bash", "PowerShell"],
    "command": r"\btaskkill\b[^|;\n]*/IM\b|\bStop-Process\b[^|;\n]*-Name\b|\bpkill\b|\bkillall\b|\bkill\s+-9\s+\$\(pgrep",
    "note": "kill by image or process name",
    "unattended": "deny",
}
OUTBOUND_DETECTOR = {
    "tools": ["Bash", "PowerShell"],
    "command": (
        r"\bgit\s+push\b|\bgh\s+(?:pr\s+(?:create|merge|comment|review|close)|issue\s+(?:create|comment|close)|release\s+create|repo\s+create)\b"
        r"|\bglab\s+mr\s+create\b|\bnpm\s+publish\b|\btwine\s+upload\b|\bcargo\s+publish\b|\bdocker\s+push\b"
        r"|\bcurl\b[^|;\n]*(?:-X\s*(?:POST|PUT|PATCH|DELETE)|--data\b|-d\s)"
    ),
    "note": "leaves the machine: push, pull request, publish, outbound POST",
    "unattended": "deny",
}

# The default tier: seeded in every project.
DEFAULT_RULES: list[dict] = [
    {
        "id": "p-destructive",
        "content": "Do not run a destructive command without asking: a recursive or forced delete, a hard reset, a force-push, a DROP or TRUNCATE, a disk format, a kill. Prefer a trash over rm. Undo is cheaper than recovery.",
        "reason": "These cannot be taken back, so the person decides; with nobody there to ask, the command is refused.",
        "detector": DESTRUCTIVE_DETECTOR,
        "anchors": ["destructive command"],
    },
    {
        "id": "p-outbound",
        "content": "Anything that leaves the machine is the person's decision: a push, a pull request, a publish, a comment, an outbound request. Local commits are free. Ask before acting externally and keep private things private.",
        "reason": "What leaves the machine cannot be called back, and other people see it.",
        "detector": OUTBOUND_DETECTOR,
        "anchors": ["leaves the machine", "ask before acting externally"],
    },
    {
        "id": "p-kill",
        "content": "Never kill processes by name; kill only a PID you started. A kill by name takes down every process that shares the name, including ones you never saw.",
        "reason": "Only a PID you started is known to be yours.",
        "detector": KILL_BY_NAME_DETECTOR,
        "anchors": ["kill processes by", "kill only a pid"],
    },
    {
        "id": "p-search",
        "content": "Search first, then read only the relevant files. A whole-file read spends context and hides the one line that matters.",
        "reason": "Context is finite; spend it on the lines that decide the change.",
        "anchors": ["search first"],
    },
    {
        "id": "p-verify",
        "content": 'Verify before you claim: run it and quote what it printed. Run the targeted tests for what you changed and one unmocked end-to-end path, never the whole suite by default. "Found, not fixed" is not done.',
        "reason": "A claim without output is a guess, and a mocked path can hide a seam that does not work.",
        "anchors": ["quote what it printed", "verify before claiming", "verify before you claim"],
    },
    {
        "id": "p-prereq",
        "content": "Complete prerequisites before dependent work. When something a later step needs is broken, fix it first; building on it repeats the failure downstream.",
        "reason": "Every later step inherits whatever is broken underneath it.",
        "anchors": ["complete prerequisites", "prerequisites before"],
    },
    {
        "id": "p-checkpoint",
        "content": "Before declaring a step done, bank the checkpoint: the recorder has drafted it, a bare checkpoint call accepts it, one field amends it. A compaction keeps only what is written.",
        "reason": "The model is the one who knows when a unit of work closed.",
        "anchors": ["bank the checkpoint", "checkpoint when"],
    },
    {
        "id": "p-secrets",
        "content": "Secrets never appear in code, a commit, a log line or a pull request body. Not once, not temporarily, not on a branch you plan to rebase: a committed key is a rotated key.",
        "reason": "History, logs and pull requests are copied and kept beyond anyone's control.",
        "anchors": ["secrets never appear"],
    },
    {
        "id": "p-remember",
        "content": "When something genuinely surprised you about this codebase, a tool or a failure, store it with remember before moving on. The recorder captures what it can detect; surprise it cannot.",
        "reason": "The next session starts without what this one learned unless it is written down.",
        "anchors": ["genuinely surprised"],
    },
    {
        "id": "p-onefunction",
        "content": 'One function, one purpose; the tell is the word "and" in its name or its description. No massive files: break them down by purpose.',
        "reason": "Code that does one thing can be read, tested and changed on its own.",
        "anchors": ["one function, one purpose", "massive files"],
    },
    {
        "id": "p-names",
        "content": "Names say what the thing does now, never a category that means nothing (utils, helpers, manager). A hard-to-name thing usually does two jobs. Comments explain why, not what.",
        "reason": "The code already says what it does; a comment earns its place by recording the reason or the constraint.",
        "anchors": ["semantically accurate", "why, not what"],
    },
    {
        "id": "p-lying",
        "content": "Nothing left lying around: no commented-out code, dead branches, abandoned scaffolding or stale TODOs. Git remembers the old version; the file does not have to.",
        "reason": "Dead code reads as live code to the next person and the next model.",
        "anchors": ["lying around", "commented-out code"],
    },
    {
        "id": "p-swallow",
        "content": "Never swallow an error on a decision path: no bare except, no log-and-continue, no fallback to a default that looks reasonable. Fail loudly; a silent exception reads as a pass.",
        "reason": "An ignored error on a decision path turns a failure into a wrong answer.",
        "anchors": ["swallow an error", "bare except"],
    },
    {
        "id": "p-determinism",
        "content": "Same inputs, same outputs where replay or testing depends on it: no wall-clock reads and no unseeded randomness on a decision path.",
        "reason": "A run that cannot be reproduced cannot be debugged, and two runs that disagree cannot be settled.",
        "anchors": ["same inputs, same outputs", "unseeded randomness"],
    },
    {
        "id": "p-mocks",
        "content": "Prefer the real thing to a stand-in. Where a substitute is needed, make it a real implementation of the seam driven by real data; a mock is a thing that agrees with you.",
        "reason": "A hand-written stand-in encodes a belief about the other side, and that belief is the likeliest thing to be wrong.",
        "anchors": ["real thing to a stand-in", "mock is a thing that agrees"],
    },
    {
        "id": "p-perf",
        "content": "Build for performance from the start: the right data structure first, batching, indexes and bounded memory on day one, sized for the real scale. Measure before optimizing and keep the measurement beside the change.",
        "reason": "The slow version is the one that ships; performance deferred is rarely added.",
        "anchors": ["performance from the start", "fast path on purpose", "never do work twice", "smart over busy"],
    },
]

# The strict tier: opt-in.
STRICT_RULES: list[dict] = [
    {
        "id": "p-direct",
        "content": "Be direct. No filler, no emojis. Have opinions and disagree when the work calls for it; a reviewer who agrees with everything reviews nothing.",
        "reason": "Filler costs the reader time and hides the point.",
        "anchors": ["be direct"],
    },
    {
        "id": "p-try",
        "content": "Try before asking. Most blockers dissolve on the first attempt; ask about the ones that do not.",
        "reason": "A question costs the person a round trip that a first attempt often saves.",
        "anchors": ["try before asking"],
    },
    {
        "id": "p-pushback",
        "content": "Push back without being asked: flag risks, forgotten items and drift from the plan as you see them.",
        "reason": "A risk raised early is cheap; one found after the work is built is not.",
        "anchors": ["push back", "pushback"],
    },
    {
        "id": "p-sequential",
        "content": "Follow the plan in order. Do not skip a step or jump ahead because a later one looks more interesting; a skipped step is what a later step silently builds on.",
        "reason": "The order of a plan usually encodes its dependencies.",
        "anchors": ["plan sequentially", "plan in order", "skip steps"],
    },
    {
        "id": "p-quality",
        "content": "Quality over speed: do it right the first time, and check the real API signature before writing against it.",
        "reason": "Rework costs more than doing it right once.",
        "anchors": ["quality over speed"],
    },
    {
        "id": "p-plan",
        "content": "Plan before code: write the design down and get it agreed before implementing. An approved design is not an approval to build.",
        "reason": "Code written before the design settles encodes an assumption nobody argued about.",
        "anchors": ["plan before code"],
    },
    {
        "id": "p-decided",
        "content": "Proposed, open and decided are three different states. A proposal never silently becomes a decision; a decision is written where decisions live, with the condition that would reopen it.",
        "reason": "A decision that is not written down with what would reopen it gets argued again or quietly forgotten.",
        "anchors": ["proposed, open"],
    },
    {
        "id": "p-gates",
        "content": "Every milestone has a gate written before the work and a verdict written after. A phase is done when its gate is met and recorded; a large one gets an independent review before anything builds on it.",
        "reason": "A phase never checked against its gate is a phase nobody knows the state of.",
        "anchors": ["gate written before"],
    },
    {
        "id": "p-delegate",
        "content": "Delegate by size, not by habit: small work yourself, multi-hour or parallel builds to subagents, a cheap model for maintenance and a strong one for review. Every agent prompt carries a hard budget.",
        "reason": "Delegation without a cap spends far more than the work needs.",
        "anchors": ["delegate by size", "agent budget"],
    },
    {
        "id": "p-numbers",
        "content": "Numbers get a source and evidence gets kept: a cited number points at what produced it, where and when.",
        "reason": "A number without its source cannot be checked when someone asks where it came from.",
        "anchors": ["numbers get a source"],
    },
    {
        "id": "p-learning",
        "content": "Write the learning when it happens, not at the end, and only what the repo does not already say.",
        "reason": "A lesson written while it is fresh is specific; one written later is vague or lost.",
        "anchors": ["write the learning when"],
    },
]

RULES: list[dict] = DEFAULT_RULES + STRICT_RULES
TIERS = {"default": DEFAULT_RULES, "strict": STRICT_RULES}

STRUCTURE_MARKERS = (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "CLAUDE.md", "setup.py", "Makefile")
LEARNING_FILES = ("ERRORS.md", "LEARNINGS.md")
MARKER_FILE = "default_pack.json"

_WORD = re.compile(r"[a-z0-9]+")


def __getattr__(name: str):
    """``compile_detector``, ``normalize_detector`` and ``rules_with_detectors``
    live in ``windvane.compliance`` (the matcher); they are reachable from
    here too, so a caller that deals in rules has one import."""
    if name in ("compile_detector", "normalize_detector", "rules_with_detectors"):
        from windvane import compliance

        return getattr(compliance, name)
    raise AttributeError(f"module 'windvane.rules' has no attribute {name!r}")


# ---------------------------------------------------------------------------
# Rule entries (mixed into MemoryStore)
# ---------------------------------------------------------------------------


class RulesMixin:
    """The rule methods of ``windvane.store.MemoryStore``."""

    def add_rule(
        self,
        project_path: str,
        content: str,
        reason: Optional[str] = None,
        relevance: int = 9,
        detector: Optional[dict] = None,
    ) -> tuple[bool, str]:
        """Add a rule. A similar rule already there is not duplicated; if it
        has no detector and one is given, it adopts the detector.
        Returns (added, message)."""
        from windvane.store import MemoryEntry

        proj = self.remember_project(project_path)
        full_content = f"{content} (Reason: {reason})" if reason else content
        existing_rules = [e for e in proj.entries if e.category == "rule"]
        duplicate = self._is_duplicate(full_content, existing_rules)
        if duplicate:
            if detector and not duplicate.detector:
                duplicate.detector = dict(detector)
                self._dirty_projects.add(proj.project_path)
                self._save()
                return (False, f"Similar rule already exists (id={duplicate.id}); detector attached")
            return (False, f"Similar rule already exists (id={duplicate.id})")
        entry = MemoryEntry(
            id=self._generate_entry_id(full_content, project_path=project_path),
            content=full_content,
            category="rule",
            source="add_rule",
            relevance=relevance,
            tags=self._extract_tags(full_content) + ["rule"],
            related_files=self._extract_file_refs(full_content),
            detector=dict(detector) if detector else None,
        )
        proj.entries.append(entry)
        self._update_indexes(proj, entry)
        proj.last_updated = time.time()
        self._dirty_projects.add(proj.project_path)
        self._save()
        return (True, f"Rule added with id={entry.id}")

    def set_detector(self, project_path: str, memory_id: str, detector: Optional[dict]) -> tuple[bool, str]:
        """Attach a detector to a rule, or clear it (None / {})."""
        proj = self.get_project(project_path)
        if not proj:
            return (False, "Project not found")
        entry = self._get_entry_by_id(proj, memory_id)
        if not entry:
            return (False, f"Memory {memory_id} not found")
        if entry.category != "rule":
            return (False, f"Memory {memory_id} is a {entry.category}, not a rule")
        entry.detector = dict(detector) if detector else None
        proj.last_updated = time.time()
        self._dirty_projects.add(self._normalize_path(project_path))
        self._save()
        return (True, f"Detector {'set' if entry.detector else 'cleared'} on rule {memory_id}")

    def get_rules(self, project_path: str) -> list:
        """A project's own rules, most relevant first."""
        proj = self.get_project(project_path)
        if not proj:
            return []
        return sorted([e for e in proj.entries if e.category == "rule"], key=lambda x: x.relevance, reverse=True)

    def get_rules_with_inheritance(self, project_path: str) -> list:
        """A project's rules AND the ones it inherits from ancestor projects,
        as ``(rule, source)`` pairs: the project's own first (source ``""``),
        then ancestors nearest first (source = the ancestor's path).
        Duplicates by id or by identical text are kept once, at the nearest
        owner."""
        pairs = [(r, "") for r in self.get_rules(project_path)]
        seen_ids = {r.id for r, _ in pairs}
        seen_text = {r.content.strip().lower() for r, _ in pairs}
        norm = self._normalize_path(project_path)
        registered = {k.lower(): k for k in (self._manifest.get("projects", {}) or {})}
        current = Path(norm)
        while True:
            parent = current.parent
            if parent == current:
                break
            current = parent
            owner = registered.get(self._normalize_path(str(current)).lower())
            if owner is None:
                continue
            for rule in self.get_rules(owner):
                text = rule.content.strip().lower()
                if rule.id in seen_ids or text in seen_text:
                    continue
                seen_ids.add(rule.id)
                seen_text.add(text)
                pairs.append((rule, owner))
        return pairs


# ---------------------------------------------------------------------------
# The pack
# ---------------------------------------------------------------------------


def _words(s: str) -> set:
    return {w for w in _WORD.findall(s.lower()) if len(w) > 2}


def similar(a: str, b: str, threshold: float = 0.45, anchors: Optional[list] = None) -> bool:
    """Same rule in substance: word-set Jaccard, one's opening inside the
    other, or a rule-specific anchor phrase present in the existing text."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    if len(wa & wb) / len(wa | wb) >= threshold:
        return True
    head = a.lower()[:40].strip()
    if head and head in b.lower():
        return True
    bl = b.lower()
    return any(anc.lower() in bl for anc in (anchors or []))


def is_project_dir(project_dir: str) -> bool:
    """A directory that is already a project (a repo, a manifest, a
    CLAUDE.md), never a home directory or a drive root."""
    p = Path(project_dir)
    try:
        if not p.is_dir():
            return False
        if p == Path.home() or p.parent == p:
            return False
    except Exception:
        return False
    return any((p / m).exists() for m in STRUCTURE_MARKERS)


def _marker_path(project_dir: str) -> Optional[Path]:
    try:
        from windvane.store import project_store_dir

        return project_store_dir(project_dir) / MARKER_FILE
    except Exception:
        return None


def _marker(project_dir: str) -> dict:
    p = _marker_path(project_dir)
    if not p or not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def seeded_version(project_dir: str) -> int:
    try:
        return int(_marker(project_dir).get("version", 0))
    except (TypeError, ValueError):
        return 0


def existing_rules(project_dir: str, store=None) -> list:
    """This project's rules plus the ones inherited from ancestors, as raw
    entries ({id, content, detector, ...})."""
    try:
        if store is None:
            from windvane.store import MemoryStore

            store = MemoryStore()
        return [
            r.model_dump()
            for r, _src in store.get_rules_with_inheritance(project_dir)
            if not r.archived_at
        ]
    except Exception:
        return []


def existing_rule_texts(project_dir: str) -> list:
    return [str(e.get("content", "")) for e in existing_rules(project_dir)]


def _attach_detector(store, project_dir: str, rule_id: str, detector: dict) -> bool:
    """Attach a pack detector to a covering rule that has none. The rule may
    live in this project or an ancestor, so walk up until a store knows it."""
    p = Path(project_dir).resolve()
    for _ in range(12):
        try:
            ok, _msg = store.set_detector(str(p), rule_id, detector)
        except Exception:
            ok = False
        if ok:
            return True
        if p.parent == p:
            break
        p = p.parent
    return False


def seed_rules(project_dir: str, strict: bool = False, force: bool = False) -> dict:
    """Add the pack's rules the project does not already have in substance:
    the default tier once per project, the strict tier when ``strict``.

    Returns {"added": [...], "skipped": [...], "detectors_attached": [...],
    "tiers": [...], "already_seeded": bool}."""
    report: dict = {"added": [], "skipped": [], "detectors_attached": [], "tiers": [], "already_seeded": False}
    marker = _marker(project_dir)
    try:
        version = int(marker.get("version", 0))
    except (TypeError, ValueError):
        version = 0
    current = version >= PACK_VERSION
    tiers = []
    if force or not current:
        tiers.append("default")
    if strict and (force or not current or not marker.get("strict")):
        tiers.append("strict")
    if not tiers:
        report["already_seeded"] = True
        return report
    report["tiers"] = tiers

    try:
        from windvane.store import MemoryStore

        store = MemoryStore()
    except Exception:
        return report
    existing_entries = existing_rules(project_dir, store)
    for tier in tiers:
        for rule in TIERS[tier]:
            covering = [
                e for e in existing_entries if similar(rule["content"], str(e.get("content", "")), anchors=rule.get("anchors"))
            ]
            if covering:
                report["skipped"].append(rule["id"])
                det = rule.get("detector")
                for e in covering:
                    if not det or not e.get("id"):
                        continue
                    have = e.get("detector")
                    if not have:
                        if _attach_detector(store, project_dir, str(e["id"]), det):
                            report["detectors_attached"].append(str(e["id"]))
                    elif (
                        isinstance(have, dict)
                        and have.get("note") == det.get("note")
                        and det.get("unattended")
                        and have.get("unattended") != det.get("unattended")
                    ):
                        # A pack-shaped detector from an earlier pack: carry
                        # the newer policy onto it, keep everything else.
                        if _attach_detector(store, project_dir, str(e["id"]), {**have, "unattended": det["unattended"]}):
                            report["detectors_attached"].append(str(e["id"]))
                continue
            try:
                ok, _ = store.add_rule(project_dir, rule["content"], rule["reason"], detector=rule.get("detector"))
            except Exception:
                ok = False
            (report["added"] if ok else report["skipped"]).append(rule["id"])
    p = _marker_path(project_dir)
    if p:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            body = {
                "version": PACK_VERSION,
                "seeded_at": time.time(),
                "strict": bool(marker.get("strict")) or "strict" in tiers,
                "added": report["added"],
                "skipped": report["skipped"],
            }
            p.write_bytes((json.dumps(body, indent=2) + "\n").encode("utf-8"))
        except Exception:
            pass
    return report


# ---------------------------------------------------------------------------
# Structure: the scaffold for a new project (opt-in, "structure": true)
# ---------------------------------------------------------------------------


def _claude_md(project: str) -> str:
    rules = "\n".join(f"- {r['content']}" for r in DEFAULT_RULES)
    return (
        f"# {project}\n\n"
        "## Purpose\n(describe the project)\n\n"
        "## Testing\n```bash\n# (add test commands)\n```\n\n"
        "## Structure\n- `.learnings/`: errors and learnings\n- `session-logs/`: daily session logs\n\n"
        f"## Rules\n\n{rules}\n"
    )


def _errors_md() -> str:
    return "# Errors\n\nProject-specific errors and fixes.\n"


def _learnings_md() -> str:
    return "# Learnings\n\nProject-specific learnings and patterns.\n"


def _per_person_layout(root: Path) -> bool:
    """A layout with one subfolder per person (``.learnings/<name>/``,
    ``session-logs/<name>/``) is left alone."""
    for base in (root / ".learnings", root / "session-logs"):
        if base.is_dir() and any(d.is_dir() and not d.name.startswith(".") and d.name != "archive" for d in base.iterdir()):
            return True
    return False


def ensure_structure(project_dir: str, today: Optional[str] = None) -> list:
    """Create the missing pieces of the scaffold; return what was created."""
    if not is_project_dir(project_dir):
        return []
    root = Path(project_dir)
    created: list = []
    wanted = {"CLAUDE.md": _claude_md(root.name)}
    if not _per_person_layout(root):
        wanted[".learnings/ERRORS.md"] = _errors_md()
        wanted[".learnings/LEARNINGS.md"] = _learnings_md()
    for rel, body in wanted.items():
        p = root / rel
        if p.exists():
            continue
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body.encode("utf-8"))
            created.append(rel)
        except Exception:
            continue
    logs = root / "session-logs"
    if not logs.exists():
        try:
            logs.mkdir(parents=True)
            created.append("session-logs/")
        except Exception:
            pass
    return created


def run_at_session_start(project_dir: str) -> list:
    """Seed the rules and create the structure per the settings; return the
    banner lines. Called on a fresh session start."""
    from windvane.config import knob

    lines: list = []
    if knob("structure", project_dir):
        created = ensure_structure(project_dir)
        if created:
            lines.append(
                "Project structure created: " + ", ".join(created)
                + ' (turn off with "structure": false in .windvane/config.json)'
            )
    if knob("default_rules", project_dir) and is_project_dir(project_dir):
        rep = seed_rules(project_dir, strict=bool(knob("strict_pack", project_dir)))
        if rep["added"]:
            lines.append(
                f"Rules seeded: {len(rep['added'])} added, {len(rep['skipped'])} already covered "
                '(memory(list_rules) to review; "default_rules": false in .windvane/config.json to opt out)'
            )
    return lines


def main(argv: "Optional[list]" = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m windvane.rules", description="Seed the rule pack into a project.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    seed = sub.add_parser("seed", help="seed the default tier (and the strict tier with --strict)")
    seed.add_argument("--project", required=True, help="the project directory")
    seed.add_argument("--strict", action="store_true", help="also seed the strict tier")
    seed.add_argument("--force", action="store_true", help="seed again even when the marker says seeded")
    args = ap.parse_args(argv)
    try:
        rep = seed_rules(str(Path(args.project)), strict=args.strict, force=args.force)
    except Exception as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        return 1
    rep["project"] = str(Path(args.project))
    rep["pack_version"] = PACK_VERSION
    print(json.dumps(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())

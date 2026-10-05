"""
The plugin's tools, one dispatcher.

The plugin's hooks module registers the tools with Claude Code and serves
each call through the daemon (warm) or ``python -m windvane.tools`` (the
subprocess fallback): the call arrives as ``{"tool": NAME, "arguments":
{...}}`` and the answer leaves as ``{"text": str, "isError": bool, "ms":
int}``.

    checkpoint   save | restore | list
    compact_now  (no operation): bank the drafted checkpoint, then the plugin compacts
    memory       remember | recall | recent | search | forget | add_rule | list_rules |
                 modify | delete | promote | archive | archive_search | restore |
                 list_mistakes | acknowledge_mistake | set_detector
    log          mistake | decision
    mine         search | decisions | errors | struggles | replay | timeline |
                 run_report | run_status | status | reindex
    deps         map | impact

``project_path`` is mapped to its repository root (a worktree names its
project); when a call names none it is the session's work project (the
hooks' ``session_project``, else the draft's), and the working directory
only as the last resort. ``checkpoint(save)`` with no
fields accepts the recorder's draft; a call with some fields amends those.

    echo '{"tool": "checkpoint", "arguments": {"operation": "list"}}' | python -m windvane.tools
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from windvane.store import Response

TOOLS = ("checkpoint", "compact_now", "memory", "log", "mine", "deps")

OPERATIONS = {
    "checkpoint": ("save", "restore", "list"),
    "compact_now": (),
    "memory": (
        "remember", "recall", "recent", "search", "forget", "add_rule", "list_rules", "modify",
        "delete", "promote", "archive", "archive_search", "restore", "list_mistakes", "acknowledge_mistake",
        "set_detector",
    ),
    "log": ("mistake", "decision"),
    "mine": (
        "search", "decisions", "errors", "struggles", "replay", "timeline", "run_report", "run_status", "status",
        "reindex",
    ),
    "deps": ("map", "impact"),
}


class Warm:
    """The long-lived instances a daemon keeps across calls: the memory
    store, the checkpoint guard and the work tracker. Each instance stays
    correct across processes (the store reloads what another writer saved)."""

    def __init__(self):
        from windvane.checkpoints import ContextGuard
        from windvane.log import WorkTracker
        from windvane.store import MemoryStore

        self.store = MemoryStore()
        self.checkpoints = ContextGuard()
        self.tracker = WorkTracker(self.store)
        self._indexes: dict = {}  # project -> (index file stamp, CodeIndex)

    def code_index(self, project: str):
        """The project's code index, kept while its file on disk is unchanged
        (the miner rebuilds it; a changed stamp reloads)."""
        from windvane.code_index import CodeIndex, index_paths

        held = self._indexes.get(project)
        for path in index_paths(project):
            try:
                st = path.stat()
            except OSError:
                continue
            stamp = (str(path), st.st_mtime_ns, st.st_size)
            if held and held[0] == stamp:
                return held[1]
            idx = CodeIndex(path)
            if idx.module_count() > 0:
                self._indexes[project] = (stamp, idx)
                return idx
        self._indexes.pop(project, None)
        return None

    def close(self):
        """Drop the cached indexes; nothing else holds a handle."""
        self._indexes.clear()


class ToolError(Exception):
    """A call the model has to fix (a missing argument, an absent module)."""


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def _coerce_list(val) -> list:
    """A list from a list, a JSON array string, or one non-empty string."""
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        val = val.strip()
        if val.startswith("["):
            try:
                parsed = json.loads(val)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass
        if val:
            return [val]
    return []


def _int(val, default: int) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _bool(val, default: bool) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("true", "1", "yes"):
            return True
        if s in ("false", "0", "no"):
            return False
    return default


def _repo_root(path: str) -> str:
    try:
        from windvane.paths import canonical_project_root

        return canonical_project_root(path) or path
    except Exception:
        return path


def session_work_project() -> str:
    """The project this session works in, when the call names none: the
    hooks' answer (``session_project``: the session's own edits, then its
    hook state, then the cwd's repository), else the recorder's draft's work
    project, else ''. The cwd alone is not trusted: the plugin's process may
    run anywhere, and a save filed under it lands in the wrong ring."""
    cwd = os.getcwd()
    # session_project answers the cwd's repository when the session named no
    # project; that answer is the last resort here, not a hit.
    fallback = _normalized(_repo_root(cwd))
    try:
        from windvane.hooks import common

        state = common.load_state()
        found = common.session_project(cwd, state if isinstance(state, dict) else None)
        if found and _normalized(str(found)) != fallback:
            return str(found)
    except Exception:
        pass
    try:
        from windvane import draft as _d

        sid, state, transcript = _d.session_inputs()
        if sid:
            found = str((_d.draft(cwd, sid, transcript, state).get("metadata") or {}).get("project_path") or "")
            if found and _normalized(found) != fallback:
                return found
    except Exception:
        pass
    return ""


def _normalized(path: str) -> str:
    from windvane.store import MemoryStore

    try:
        return MemoryStore._normalize_path(path).lower()
    except Exception:
        return str(path).replace("\\", "/").lower()


def _canonical(arguments: dict) -> dict:
    """``project_path`` mapped to its repository root; when the call names
    none, the session's work project (``session_work_project``), and the cwd
    only as the last resort."""
    out = dict(arguments)
    path = str(out.get("project_path") or "").strip() or session_work_project() or os.getcwd()
    out["project_path"] = _repo_root(path)
    return out


def _clarify(reasoning: str, question: str) -> str:
    return Response(status="needs_clarification", confidence="high", reasoning=reasoning, questions=[question]).to_formatted_string()


def _module(name: str):
    """Import a module another part of windvane provides; an absent one is a
    ToolError naming it."""
    import importlib

    try:
        return importlib.import_module(name)
    except ImportError as e:
        raise ToolError(f"{name} is not available: {e}")


# ---------------------------------------------------------------------------
# checkpoint
# ---------------------------------------------------------------------------


def _fill_from_draft(given: dict) -> tuple:
    """(arguments, names of the ones taken from the draft). The draft is
    built for this process's session (the id Claude Code exports, the
    transcript from that session's hook state) and the project the call
    names, else the cwd. Any failure leaves the arguments as given."""
    try:
        from windvane import draft as _d

        sid, state, transcript = _d.session_inputs()
        project_dir = given.get("project_path") or os.getcwd()
        record = _d.draft(project_dir, sid, transcript, state)
    except Exception:
        return given, []
    from windvane.draft import FIELDS

    out = dict(given)
    drafted: list = []
    for name in FIELDS:
        if not out.get(name) and record.get(name):
            value = record[name]
            out[name] = list(value) if isinstance(value, list) else value
            drafted.append(name)
    work_project = str((record.get("metadata") or {}).get("project_path") or "")
    if not out.get("project_path") and work_project:
        out["project_path"] = work_project
        drafted.append("project_path")
    return out, drafted


def _checkpoint(warm: Warm, op: str, args: dict, raw: dict) -> str:
    guard = warm.checkpoints
    if op == "save":
        # The project is the call's own when given; otherwise the draft's
        # work project (the session's edits name it) fills it.
        given = {
            "task_description": args.get("task_description") or "",
            "current_step": args.get("current_step") or "",
            "completed_steps": _coerce_list(args.get("completed_steps", [])),
            "pending_steps": _coerce_list(args.get("pending_steps", [])),
            "files_involved": _coerce_list(args.get("files_involved", [])),
            "key_decisions": _coerce_list(args.get("key_decisions", [])),
            "handoff_summary": args.get("handoff_summary") or "",
            "handoff_context_needed": _coerce_list(args.get("handoff_context_needed")),
            "handoff_warnings": _coerce_list(args.get("handoff_warnings")),
            "project_path": args["project_path"] if raw.get("project_path") else "",
        }
        filled, drafted = _fill_from_draft(given)
        if not filled.get("project_path"):
            filled["project_path"] = args["project_path"]
        if not filled["task_description"]:
            return _clarify("No task description", "What task are you working on?")
        response = guard.save_checkpoint(
            task_description=filled["task_description"],
            current_step=filled["current_step"],
            completed_steps=filled["completed_steps"],
            pending_steps=filled["pending_steps"],
            files_involved=filled["files_involved"],
            key_decisions=filled["key_decisions"] or None,
            blockers=_coerce_list(args.get("blockers")) or None,
            project_path=filled["project_path"] or None,
            handoff_summary=filled["handoff_summary"] or None,
            handoff_context_needed=filled["handoff_context_needed"] or None,
            handoff_warnings=filled["handoff_warnings"] or None,
            drafted_fields=drafted,
        )
        text = response.to_formatted_string()
        if drafted:
            text += "\nDrafted by the recorder: " + ", ".join(drafted)
        return text
    if op == "restore":
        return guard.restore_checkpoint(
            args.get("task_id") or None, project_path=args["project_path"], index=_int(args.get("index"), 0)
        ).to_formatted_string()
    # list
    return guard.list_checkpoints(project_path=args["project_path"]).to_formatted_string()


def _compact_now(warm: Warm, args: dict, raw: dict) -> str:
    """Bank the drafted checkpoint (trigger ``compact_now``) and answer its
    one-line summary; the plugin compacts after."""
    from windvane import draft as _d

    sid, state, transcript = _d.session_inputs()
    record = _d.draft(args["project_path"], sid, transcript, state)
    entry = _d.bank(record, sid, trigger="compact_now")
    return _d.summary_line(entry)


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


def _memory(warm: Warm, op: str, args: dict, raw: dict) -> str:
    store = warm.store
    project_path = args["project_path"]

    if op == "remember":
        content = args.get("content") or ""
        if not content:
            return _clarify("No content provided", "What would you like me to remember?")
        category = str(args.get("category") or "note")
        relevance = _int(args.get("relevance"), 5)
        try:
            if category == "priority":
                store.add_priority(content, project_path, relevance)
            else:
                kind = category if category in ("decision", "discovery", "context") else "discovery"
                store.remember_discovery(project_path, content, relevance=relevance, category=kind)
            return Response(
                status="success",
                confidence="high",
                reasoning=f"Remembered: {content[:100]}{'...' if len(content) > 100 else ''}",
                data={"category": category, "relevance": relevance},
            ).to_formatted_string()
        except Exception as e:
            return Response(status="failed", confidence="high", reasoning=f"Failed to store memory: {e}").to_formatted_string()

    if op == "recall":
        return Response(
            status="success", confidence="high", reasoning="Here's what I remember", data=store.recall(project_path=project_path)
        ).to_formatted_string()

    if op == "forget":
        store.forget_project(project_path)
        return Response(status="success", confidence="high", reasoning=f"Forgot all memories for: {project_path}").to_formatted_string()

    if op == "search":
        file_path, tags, query = args.get("file_path"), _coerce_list(args.get("tags")) or None, args.get("query")
        if not file_path and not tags and not query:
            return _clarify("No search criteria provided", "Provide file_path, tags, or query to search")
        results = store.search_memories(
            project_path=project_path, file_path=file_path, tags=tags, query=query, limit=_int(args.get("limit"), 5)
        )
        return Response(
            status="success",
            confidence="high",
            reasoning=f"Found {len(results)} memories matching criteria",
            data={
                "count": len(results),
                "memories": [
                    {
                        "id": m.id,
                        "content": m.content,
                        "relevance": m.relevance,
                        "tags": m.tags,
                        "related_files": m.related_files,
                        "access_count": m.access_count,
                    }
                    for m in results
                ],
            },
        ).to_formatted_string()

    if op == "add_rule":
        det = args.get("detector")
        added, msg = store.add_rule(
            project_path=project_path,
            content=args.get("content") or "",
            reason=args.get("reason"),
            relevance=_int(args.get("relevance"), 9),
            detector=det if isinstance(det, dict) else None,
        )
        return Response(status="success" if added else "needs_clarification", confidence="high", reasoning=msg).to_formatted_string()

    if op == "set_detector":
        # A detector is hand-written; validated here so a broken regex is
        # refused, not stored.
        det = args.get("detector")
        if det:
            compliance = _module("windvane.compliance")
            norm = compliance.normalize_detector(det)
            if not norm:
                return "Detector needs at least one of: tools, command (regex), paths (globs), input (regex)"
            _compiled, err = compliance.compile_detector(norm)
            if err:
                return f"Detector rejected: {err}"
            det = norm
        ok, msg = store.set_detector(
            project_path=project_path, memory_id=args.get("memory_id") or "", detector=det if isinstance(det, dict) else None
        )
        return Response(status="success" if ok else "needs_clarification", confidence="high", reasoning=msg).to_formatted_string()

    if op == "list_rules":
        # Inherited rules count: a workspace-level rule binds every project
        # under it, and the hooks inject them.
        pairs = store.get_rules_with_inheritance(project_path)
        if not pairs:
            return "No rules defined for this project"
        own = sum(1 for _, src in pairs if not src)
        inherited = len(pairs) - own
        head = f"Rules for {project_path}: {own} own" + (f", {inherited} inherited" if inherited else "")
        lines = [head, ""]
        for r, src in pairs:
            mark = " [detector]" if r.detector else ""
            if src:
                mark += f" [inherited from {Path(src).name}]"
            lines.append(f"  [{r.id}]{mark} {r.content}")
        return Response(status="success", confidence="high", reasoning="\n".join(lines)).to_formatted_string()

    if op == "modify":
        rel = args.get("relevance")
        ok, msg = store.modify_memory(
            project_path=project_path,
            memory_id=args.get("memory_id") or "",
            content=args.get("content"),
            relevance=_int(rel, 5) if rel is not None else None,
            category=args.get("category"),
        )
        return Response(status="success" if ok else "needs_clarification", confidence="high", reasoning=msg).to_formatted_string()

    if op == "delete":
        ok, msg = store.delete_memory(project_path=project_path, memory_id=args.get("memory_id") or "")
        return Response(status="success" if ok else "needs_clarification", confidence="high", reasoning=msg).to_formatted_string()

    if op == "promote":
        ok, msg = store.promote_to_rule(project_path=project_path, memory_id=args.get("memory_id") or "", reason=args.get("reason"))
        return Response(status="success" if ok else "needs_clarification", confidence="high", reasoning=msg).to_formatted_string()

    if op == "archive":
        result = store.archive_old_memories(project_path=project_path, dry_run=_bool(args.get("dry_run"), True))
        return Response(
            status="success",
            confidence="high",
            reasoning=f"{'Would archive' if result.get('dry_run') else 'Archived'} {result['archived_count']} memories",
            data=result,
        ).to_formatted_string()

    if op == "restore":
        ok, msg = store.restore_from_archive(project_path=project_path, memory_id=args.get("memory_id") or "")
        return Response(status="success" if ok else "needs_clarification", confidence="high", reasoning=msg).to_formatted_string()

    if op == "recent":
        entries = store.get_recent_memories(
            project_path=project_path, category=args.get("category"), limit=_int(args.get("limit"), 10)
        )
        if not entries:
            return "No recent memories"
        lines = ["Recent memories (newest first):", ""]
        for e in entries:
            age_mins = int((time.time() - e.created_at) / 60)
            if age_mins < 60:
                age_str = f"{age_mins}m ago"
            elif age_mins < 1440:
                age_str = f"{age_mins // 60}h ago"
            else:
                age_str = f"{age_mins // 1440}d ago"
            shown = e.content[:57] + "..." if len(e.content) > 60 else e.content
            lines.append(f"  [{e.id}] ({age_str}) [{e.category}] {shown}")
        # The lines carry every entry; the entries are not repeated as data.
        return Response(status="success", confidence="high", reasoning="\n".join(lines)).to_formatted_string()

    if op == "archive_search":
        entries = store.search_archive(
            project_path=project_path,
            query=args.get("query"),
            tags=_coerce_list(args.get("tags")) or None,
            limit=_int(args.get("limit"), 5),
        )
        if not entries:
            return "No archived memories found"
        lines = ["Archived memories:", ""]
        for e in entries:
            age_days = int((time.time() - (e.archived_at or e.created_at)) / 86400)
            shown = e.content[:57] + "..." if len(e.content) > 60 else e.content
            lines.append(f"  [{e.id}] ({age_days}d archived) [{e.category}] {shown}")
        return Response(
            status="success",
            confidence="high",
            reasoning="\n".join(lines),
            suggestions=["Use memory(restore, memory_id='...') to bring one back to active"],
        ).to_formatted_string()

    if op == "list_mistakes":
        entries = store.get_recent_memories(project_path=project_path, category="mistake", limit=_int(args.get("limit"), 20))
        if not entries:
            return "No mistakes tracked"
        lines = [f"Tracked mistakes ({len(entries)}):"]
        for e in entries:
            age_days = int((time.time() - e.created_at) / 86400)
            files = ", ".join(e.related_files[:3]) if e.related_files else "no file"
            lines.append(f"  [{e.id}] ({age_days}d) [{files}] {e.content[:100]}")
        lines += ["", "Use memory(acknowledge_mistake, memory_id='...') to archive a learned mistake"]
        return Response(status="success", confidence="high", reasoning="\n".join(lines)).to_formatted_string()

    # acknowledge_mistake
    mid = args.get("memory_id") or ""
    if not mid:
        return _clarify("No memory_id", "Which mistake to acknowledge? Use list_mistakes to see IDs.")
    proj = store.get_project(project_path)
    if proj:
        entry = next((e for e in proj.entries if e.id == mid and e.category == "mistake"), None)
        if entry:
            # A real move into the archive: hook readers see hot entries
            # only, and the entry stays restorable.
            store._move_entries_to_archive(proj, [entry])
            store._save()
            store._save_archive()
            return Response(
                status="success",
                confidence="high",
                reasoning=f"Mistake [{mid}] acknowledged and archived. It won't appear in pre-edit warnings.",
            ).to_formatted_string()
    return _clarify(f"Mistake [{mid}] not found", "Check the ID with list_mistakes")


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------


def _log(warm: Warm, op: str, args: dict, raw: dict) -> str:
    tracker = warm.tracker
    tracker.set_project(args["project_path"])
    if op == "mistake":
        description = args.get("description") or ""
        if not description:
            return _clarify("No description provided", "What went wrong?")
        persisted = tracker.log_mistake(description, args.get("file_path"), args.get("how_to_avoid"))
        try:
            mark = __import__("windvane.hooks.common", fromlist=["mark_mistake_logged"]).mark_mistake_logged
            mark()
        except Exception:
            pass
        if persisted:
            return Response(
                status="success",
                confidence="high",
                reasoning=f"Logged mistake: {description[:100]}",
                suggestions=["This will warn you if you're about to repeat this mistake"],
            ).to_formatted_string()
        return Response(
            status="partial", confidence="high", reasoning=f"Recorded for this session only: {description[:100]}"
        ).to_formatted_string()
    # decision
    decision, reason = args.get("decision") or "", args.get("reason") or ""
    if not decision or not reason:
        return _clarify("Need both decision and reason", "What was decided and why?")
    tracker.log_decision(decision, reason, _coerce_list(args.get("alternatives")))
    return Response(status="success", confidence="high", reasoning=f"Logged decision: {decision[:100]}").to_formatted_string()


# ---------------------------------------------------------------------------
# mine (the session miner and the run report)
# ---------------------------------------------------------------------------


def _mine(warm: Warm, op: str, args: dict, raw: dict) -> str:
    project_path = args["project_path"]
    storage = str(warm.store.storage_dir)

    if op == "search":
        search = _module("windvane.mining.search")
        results = search.search_sessions(
            project_path,
            query=args.get("query") or "",
            limit=_int(args.get("limit"), 10),
            method=args.get("method") or "hybrid",
            since=args.get("since") or "",
            until=args.get("until") or "",
        )
        if not results:
            return "No results found. The search index builds during background mining."
        seen, uniq = set(), []
        for r in results:
            key = " ".join((r.chunk_text or "")[:120].split()).lower()
            if key in seen:
                continue
            seen.add(key)
            uniq.append(r)
        kind_filter = str(args.get("kind") or "").strip().lower()
        tagged = [(r, search.classify_chunk(r.chunk_text)) for r in uniq]
        if kind_filter:
            tagged = [(r, k) for r, k in tagged if k == kind_filter]
        if not tagged:
            return f"No '{kind_filter}' results among {len(results)} hits."
        lines = [f"Found {len(tagged)} results" + (f" (kind={kind_filter})" if kind_filter else "") + ":"]
        for r, kind in tagged:
            lines.append(f"  [{r.score:.2f}] ({kind}) {r.chunk_text[:150]}")
            lines.append(f"    Session: {r.session_id[:12]} | {r.timestamp[:19]} | {r.msg_type}")
            if r.related_files:
                lines.append(f"    Files: {', '.join(r.related_files[:5])}")
        return "\n".join(lines)

    if op == "decisions":
        search = _module("windvane.mining.search")
        results = search.find_decision(project_path, query=args.get("query") or "")
        if not results:
            return "No matching decisions found."
        lines = [f"Found {len(results)} decision(s):"]
        for r in results:
            if r.msg_type == "git":
                head, _, excerpt = r.chunk_text.partition(" -- diff: ")
                lines.append(f"\n[{r.score:.2f}] {head[:200]}")
                if excerpt:
                    lines.append(f"  Diff: {excerpt[:500]}")
                lines.append(f"  Source: {r.session_id} | {r.timestamp[:10]}")
                continue
            lines.append(f"\n[{r.score:.2f}] {r.chunk_text[:200]}")
            lines.append(f"  Session: {r.session_id[:12]} | {r.timestamp[:19]}")
            if r.surrounding:
                lines.append("  Context:")
                lines.extend(f"    {ctx}" for ctx in r.surrounding)
        return "\n".join(lines)

    if op == "replay":
        search = _module("windvane.mining.search")
        results = search.find_file_discussions(project_path, file_path=args.get("file_path") or "", limit=_int(args.get("limit"), 10))
        if not results:
            return "No discussions found for this file."
        lines = [f"Found {len(results)} discussion(s):"]
        for r in results:
            if r.msg_type == "git":
                lines.append(f"  [{r.score:.2f}] {r.chunk_text[:220]}")
                lines.append(f"    {r.timestamp[:10]} | {r.session_id}")
                continue
            lines.append(f"  [{r.score:.2f}] {r.chunk_text[:150]}")
            lines.append(f"    {r.timestamp[:19]} | {r.msg_type}")
        return "\n".join(lines)

    if op in ("struggles", "errors", "timeline", "status"):
        session_index = _module("windvane.mining.session_index")
        index = session_index.resolve_project_index(project_path)
        if op == "status":
            background = _module("windvane.mining.background")
            status = background.get_mining_status()
            lines = (
                [f"Indexed: {index.get_session_count()} sessions, {index.get_total_messages()} messages"]
                if index
                else ["No index built yet."]
            )
            lines.append(f"Miner: {status.get('status', 'unknown')}")
            return "\n".join(lines)
        if not index:
            return "No session data found."
        if op == "struggles":
            patterns = _module("windvane.mining.patterns")
            struggles = patterns.detect_struggles(index.sessions, project_root=project_path)
            if not struggles:
                return "No struggle patterns detected."
            return "\n".join(["Struggle areas:"] + [f"  {s.file_path}: {s.description}" for s in struggles[:10]])
        if op == "errors":
            patterns = _module("windvane.mining.patterns")
            errors = patterns.detect_recurring_errors(index.sessions, project_path, storage)
            if not errors:
                return "No recurring error patterns."
            lines = ["Recurring errors:"]
            for e in errors:
                lines.append(f"  {e.example or e.message_pattern or e.error_type} ({e.session_count} sessions)")
                if e.fix:
                    lines.append(f"    fix: {e.fix}")
            return "\n".join(lines)
        timeline = _module("windvane.mining.timeline")
        events = timeline.build_timeline(index, project_path, storage)
        if not events:
            return "No timeline events."
        return "\n".join(["Project timeline:"] + [f"  [{e.timestamp[:10]}] {e.event_type}: {e.description[:120]}" for e in events[-20:]])

    if op == "reindex":
        return _reindex(project_path, str(args.get("mode") or "incremental"), storage)

    from windvane.checkpoints import _hook_attr, session_id

    sid = session_id()
    if op == "run_status":
        goal = _module("windvane.goal")
        if not sid:
            return "No session id available to this process."
        try:
            state = _hook_attr("load_state")()
        except AttributeError as e:
            raise ToolError(f"windvane.hooks.common is not available: {e}")
        s = goal.summary(state)
        if not s:
            return "No goal run in this session. A person types `/goal <condition>`; windvane brackets it from the next stop."
        return json.dumps(s, indent=2, default=str)

    # run_report
    report = _module("windvane.report")
    if not sid:
        return "No session id available to this process; run `python -m windvane.report --session <id> --project <dir>` instead."
    path = report.write_report(sid, project_path)
    body = report.render_md(report.collect(sid, project_path))
    head = f"Run report written: {path}\n\n" if path else "Run report could not be written; rendering only.\n\n"
    return head + body


# The tool's two modes, as the miner names them: bootstrap mines every past
# session; incremental is the miner's "full" pass, which reads only what is
# new since its watermarks and then refreshes the patterns.
REINDEX_MODES = {"bootstrap": "bootstrap", "incremental": "full"}
REINDEX_WAIT_SECS = 10.0
REINDEX_POLL_SECS = 0.5


def _reindex(project_path: str, mode: str, storage: str) -> str:
    """Start the background miner and wait up to ``REINDEX_WAIT_SECS`` for
    it to finish; answer the result, or the phase it is in."""
    if mode not in REINDEX_MODES:
        return _clarify(f"Unknown reindex mode {mode!r}", "Use mode bootstrap or incremental")
    background = _module("windvane.mining.background")
    miner_mode = REINDEX_MODES[mode]
    started = background.start_mining_background(project_path, mode=miner_mode, windvane_storage_dir=storage)
    if not started:
        if background.is_mining_running():
            return "Mining already running. Check mine(status) for results."
        if os.environ.get("WINDVANE_NO_DAEMON", "").strip():
            return "Mining did not start: background processes are off (WINDVANE_NO_DAEMON is set)."
        return "Mining did not start. Check mine(status)."
    deadline = time.monotonic() + REINDEX_WAIT_SECS
    while time.monotonic() < deadline:
        time.sleep(REINDEX_POLL_SECS)
        status = background.get_mining_status()
        if status.get("status") == "completed":
            result = status.get("result", {})
            if not isinstance(result, dict):
                result = {}
            lines = [f"Mining completed (mode={mode}):"]
            if result.get("sessions"):
                lines.append(f"  Sessions indexed: {result['sessions']}")
            if result.get("messages"):
                lines.append(f"  Messages: {result['messages']}")
            if result.get("extractions"):
                lines.append(f"  Extractions: {result['extractions']} findings")
            if result.get("embeddings"):
                lines.append(f"  Search chunks: {result['embeddings']}")
            return "\n".join(lines)
    phase = background.get_mining_status().get("phase", "unknown")
    return f"Mining started (mode={mode}), currently in '{phase}' phase. Check mine(status) for results."


# ---------------------------------------------------------------------------
# deps
# ---------------------------------------------------------------------------


def _deps(warm: Warm, op: str, args: dict, raw: dict) -> str:
    from windvane import deps

    project_root = str(args.get("project_root") or args["project_path"])
    if op == "map":
        symbol = args.get("symbol") or ""
        return deps.deps_map(
            file_path=args.get("file_path") or "",
            project_root=project_root,
            include_reverse=_bool(args.get("include_reverse"), False),
            symbol=symbol,
            index=warm.code_index(project_root) if symbol else None,
        )
    return deps.impact_analyze(args.get("file_path") or "", project_root, args.get("proposed_changes"))


_DISPATCH: dict = {"checkpoint": _checkpoint, "memory": _memory, "log": _log, "mine": _mine, "deps": _deps}


def _answer(tool: str, raw: dict, warm: Warm) -> str:
    args = _canonical(raw)
    if tool == "compact_now":
        return _compact_now(warm, args, raw)
    op = str(raw.get("operation") or "")
    if op not in OPERATIONS[tool]:
        raise ToolError(f"Unknown {tool} operation {op!r}; one of {', '.join(OPERATIONS[tool])}")
    handler: Callable = _DISPATCH[tool]
    return handler(warm, op, args, raw)


def run(request: dict, warm: Optional[Warm] = None) -> dict:
    """Answer one request. ``warm`` holds the long-lived instances (the
    daemon keeps one across calls); without it one is built and closed for
    this call. Never raises: a failure is ``isError`` with the reason."""
    started = time.monotonic()

    def _done(text: str, error: bool) -> dict:
        return {"text": text, "isError": error, "ms": int((time.monotonic() - started) * 1000)}

    if not isinstance(request, dict):
        return _done("the request must be a JSON object", True)
    tool = str(request.get("tool", ""))
    arguments = request.get("arguments", {})
    if tool not in TOOLS:
        return _done(f"Unknown plugin tool {tool!r}; one of {', '.join(TOOLS)}", True)
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _done("arguments must be a JSON object", True)
    own = warm is None
    try:
        if own:
            warm = Warm()
        assert warm is not None
        from windvane.checkpoints import session_scope

        # The session this call belongs to: the id Claude Code exports (the
        # daemon applies the caller's environment for the call).
        with session_scope(os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()):
            return _done(_answer(tool, arguments, warm), False)
    except ToolError as e:
        return _done(str(e), True)
    except Exception as e:  # the model reads the reason
        return _done(f"{tool} failed: {type(e).__name__}: {e}", True)
    finally:
        if own and warm is not None:
            warm.close()


def main() -> int:
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    try:
        request = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        print(json.dumps({"text": f"stdin is not JSON: {exc}", "isError": True, "ms": 0}))
        return 1
    if not isinstance(request, dict):
        print(json.dumps({"text": "stdin must be a JSON object", "isError": True, "ms": 0}))
        return 1
    out = run(request)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    print(json.dumps(out, ensure_ascii=False))
    return 1 if out["isError"] else 0


if __name__ == "__main__":
    sys.exit(main())

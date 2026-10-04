"""
The checkpoint the recorder drafts: never ask the model to write what the
machine can record.

A deliberate checkpoint used to be prose the model wrote field by field
(task, step, completed and pending steps, files, warnings, the handoff).
Most of it is recorded elsewhere already: the warnings and much of the
pending list carry forward from the previous record, the files are the
session's own edits, and the reply's closing lines state the handoff. So the
recorder drafts the whole record and the model accepts it with a bare
``checkpoint(save)`` or amends only the fields it wants to change. A
compaction banks the draft as is (the PreCompact hook).

Sources, and only these:

- the previous record: this session's newest deliberate checkpoint on the
  live branch (task, warnings, context needed, pending minus what has
  closed since, files and handoff as fallbacks); when the session has none,
  the project's newest deliberate one lends its warnings and context needed
  only, since another session's task and pending are not this one's;
- the transcript's live chain, across compactions: the session's edits
  (files), its task list (TaskCreate / TaskUpdate: open tasks pending, done
  ones completed, the newest in-progress one the current step), its git
  commits, its first typed prompt (the task when nothing carries forward)
  and the last assistant reply (its closing paragraph is the handoff, and a
  closing sentence that says a pending step is done closes it);
- the hook state: the test runs, a staged milestone claim, the session's
  start, and the decisions stored this session.

Stdlib only. Every failure degrades to an empty field; ``draft`` never
raises.

    python -m windvane.draft --project <dir> [--session <sid>]
        [--transcript <path>] [--json|--text] [--bank]
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

FIELDS = (
    "task_description",
    "current_step",
    "completed_steps",
    "pending_steps",
    "files_involved",
    "key_decisions",
    "handoff_summary",
    "handoff_context_needed",
    "handoff_warnings",
)
STRING_FIELDS = ("task_description", "current_step", "handoff_summary")

MAX_FILES = 20
TASK_CHARS = 160
SUMMARY_CHARS = 300
MANUAL_MAX_AGE_SECS = 14 * 24 * 3600  # read_latest's bound on a deliberate checkpoint

_NEXT_CUE = re.compile(r"\b(?:next|then|waiting|on your word|pending|remaining|blocked)\b", re.IGNORECASE)
# A paragraph that addresses the person ("when you have a moment, reconnect
# the server") is a request, not the handoff; the closing walk skips it.
_SECOND_PERSON = re.compile(r"\b(?:you|your|yours|you'?re|you'?ll|you'?ve)\b", re.IGNORECASE)
_CLOSING_LOOKBACK = 3
_TASK_NUM = re.compile(r"Task #(\w+)")
_GIT_COMMIT = re.compile(r"\bgit((?:\s+-[cC]\s+\S+)*)\s+commit(?![\w-])")
_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")
_TRIVIAL = {"review what was in progress", "continue work from before compaction", "review context_needed items"}
_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_SHELL_TOOLS = frozenset({"Bash", "PowerShell"})

# A bulleted or numbered list line.
_LIST_LINE = re.compile(r"^\s*(?:[-*+\u2022]|\d+[.)])\s+")
# The completion verbs a closing sentence uses to say a step is done.
_DONE_VERBS = frozenset({"done", "landed", "merged", "committed", "passed", "fixed", "built", "written"})
_NEGATION = re.compile(r"\b(?:not|never|no|isn'?t|wasn'?t|aren'?t|hasn'?t|haven'?t|yet|still|pending|remaining|waiting)\b|n't\b", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+|;\s+")
_STEP_WORD = re.compile(r"[a-z0-9_][a-z0-9_.\-]*", re.IGNORECASE)
# Words that say nothing about WHICH step it is.
_STOPWORDS = frozenset(
    """a an the and or but of to in on for with at by from into onto over under as is are was were be been being
    it its this that these those then than so do does did done can could should would will shall may might must
    add adds added make makes made write writes wrote update updates updated fix fixes implement implements wire
    wires set sets get gets run runs ran use uses used new all any some more most each every one two step steps
    task tasks phase part work item items next now also just still again first last""".split()
)


def _norm(s: str) -> str:
    return " ".join(str(s or "").lower().split())


def _cut(s: str, n: int) -> str:
    s = " ".join(str(s or "").split())
    if len(s) <= n:
        return s
    head = s[: n - 3]
    if " " in head[n // 2:]:
        head = head[: head.rfind(" ")]
    return head.rstrip(" ,;:") + "..."


def _epoch(ts) -> float:
    """A transcript record's ``timestamp`` (ISO 8601, ``Z``) as epoch seconds; 0 when unreadable."""
    try:
        s = str(ts or "").strip()
        if not s:
            return 0.0
        from datetime import datetime, timezone

        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except Exception:
        return 0.0


def _strip_emphasis(s: str) -> str:
    s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s)
    return re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"\1", s)


def _items(v) -> list:
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, list):
        return [str(x) for x in v if isinstance(x, (str, int, float)) and str(x).strip()]
    return []


def _pick(rec: dict, *keys: str) -> list:
    """The first non-empty list among ``keys``: a ring record carries the
    ring vocabulary, an older record either."""
    for k in keys:
        got = _items(rec.get(k))
        if got:
            return got
    return []


def _closes(item: str, done: list) -> bool:
    """Has ``item`` (a pending step) closed: equal to a completed entry, or
    one contains the other (eight characters at least, so "tests" closes
    nothing)."""
    n = _norm(item)
    if not n:
        return True
    for d in done:
        if n == d or (len(n) >= 8 and n in d) or (len(d) >= 8 and d in n):
            return True
    return False


def _dedupe(items: list) -> list:
    out, seen = [], set()
    for s in items:
        k = _norm(s)
        if k and k not in seen:
            seen.add(k)
            out.append(s)
    return out


# ── the reply's closing lines ───────────────────────────────────────────────


def _paragraphs(text: str) -> list:
    return [p for p in re.split(r"\n\s*\n", str(text or "")) if p.strip()]


def _is_list(paragraph: str) -> bool:
    """Every non-empty line is a bullet or a numbered item."""
    lines = [ln for ln in paragraph.splitlines() if ln.strip()]
    return bool(lines) and all(_LIST_LINE.match(ln) for ln in lines)


def _closing(text: str) -> str:
    """The closing paragraph of a reply, emphasis stripped, on one line: the
    last one that does not address the person (a request to them is not the
    handoff), looking back three paragraphs at most; the last one when every
    one of them does. A bulleted list is passed over when a prose paragraph
    stands within the lookback (a list is the body of a reply, not the line
    that hands it over)."""
    paras = _paragraphs(text)
    if not paras:
        return ""
    window = paras[-_CLOSING_LOOKBACK:]
    prose = [p for p in window if not _is_list(p)]
    candidates = prose if prose else window
    for p in reversed(candidates):
        if not _SECOND_PERSON.search(p):
            return _cut(_strip_emphasis(p), SUMMARY_CHARS)
    return _cut(_strip_emphasis(candidates[-1]), SUMMARY_CHARS)


def _closing_sentences(text: str) -> list:
    """The sentences (clauses split at ``;``) of the reply's last
    paragraphs, emphasis stripped and lower-cased."""
    out = []
    for p in _paragraphs(text)[-_CLOSING_LOOKBACK:]:
        for s in _SENTENCE_SPLIT.split(_strip_emphasis(p)):
            s = " ".join(s.split()).lower()
            if s:
                out.append(s)
    return out


def _distinctive(step: str) -> list:
    """The words that tell this step from another: no stopwords, no
    completion verbs, two characters at least."""
    words = []
    for w in _STEP_WORD.findall(step.lower()):
        w = w.strip(".-")
        if len(w) < 2 or w in _STOPWORDS or w in _DONE_VERBS:
            continue
        if w not in words:
            words.append(w)
    return words


def _said_done(step: str, sentences: list) -> bool:
    """Does a closing sentence say ``step`` is done: it carries the step's
    distinctive words (all of them for a step of up to three, two thirds
    for a longer one) and a completion verb, and no negation ("not merged
    yet" closes nothing)."""
    words = _distinctive(step)
    if not words:
        return False
    need = len(words) if len(words) <= 3 else -(-2 * len(words) // 3)
    for s in sentences:
        tokens = set(w.strip(".-") for w in _STEP_WORD.findall(s))
        if not tokens & _DONE_VERBS:
            continue
        if _NEGATION.search(s):
            continue
        if sum(1 for w in words if w in tokens) >= need:
            return True
    return False


# ── seams to the hook modules ───────────────────────────────────────────────


def _hook(name: str):
    from windvane.checkpoints import _hook_attr

    return _hook_attr(name)


def _set_session(session_id: str) -> None:
    """Point the hook modules at this session's own state file (read only)."""
    try:
        from windvane.hooks import common

        if hasattr(common, "_session_id"):
            common._session_id = session_id
    except Exception:
        pass


# ── the previous record ─────────────────────────────────────────────────────


def _previous(project_dir: str, work_project: str, session_id: str, transcript_path: str) -> tuple:
    """(record, source): this session's newest deliberate checkpoint on the
    live branch, as the compaction banner picks it, else the project's
    newest deliberate one. ({}, "") when neither exists."""
    try:
        from windvane import checkpoints as ck

        dirs = ck.candidate_dirs(work_project)
        try:
            root = ck._normalize(project_dir) + "/"
            manifest = json.loads((ck.storage_dir() / "manifest.json").read_text(encoding="utf-8"))
            for p, info in (manifest.get("projects") or {}).items():
                if p.startswith(root) and info.get("hash"):
                    d = ck.storage_dir() / "projects" / info["hash"]
                    if d not in dirs:
                        dirs.append(d)
        except Exception:
            pass
        try:
            own, _skipped = _hook("_own_session_checkpoint")(dirs, session_id, transcript_path)
        except Exception:
            own = None
        if own:
            return own, "previous checkpoint"
        now = time.time()
        for h in ck.read_history(dirs):  # newest first
            if h.get("kind") != "manual":
                continue
            if now - ck._created_ts(h) <= MANUAL_MAX_AGE_SECS:
                return h, "project checkpoint"
            break
    except Exception:
        pass
    return {}, ""


# ── the transcript ──────────────────────────────────────────────────────────


def _result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(str(b.get("text") or "") for b in c if isinstance(b, dict))
    return ""


def _first_line(text: str, stop: str = "") -> str:
    for line in str(text or "").splitlines():
        s = line.strip()
        if stop and s == stop:
            return ""
        if s:
            return s
    return ""


def _shell_tokens(s: str) -> list:
    """(value, start, end, quoted) tokens of one shell segment: stops at an
    unquoted ``&&``, ``||``, ``;``, ``|`` or line break. A double-quoted
    value keeps its line breaks (a heredoc inside ``"$(cat <<'EOF'``)."""
    out: list = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch in " \t":
            i += 1
            continue
        if ch in "\n;|&":
            break
        start = i
        if ch in "\"'":
            q = ch
            i += 1
            buf = []
            while i < n and s[i] != q:
                if q == '"' and s[i] == "\\" and i + 1 < n:
                    i += 1
                buf.append(s[i])
                i += 1
            out.append(("".join(buf), start, i + 1, True))
            i += 1
            continue
        while i < n and s[i] not in " \t\n;|&":
            i += 1
        out.append((s[start:i], start, i, False))
    return out


def _message_file(path: str) -> str:
    """The first line of a ``git commit -F <file>`` message, when the file
    is still there (a Git Bash /c/... path read as C:/...)."""
    p = path
    m = re.match(r"^/([a-zA-Z])/(.*)$", p)
    if m and os.name == "nt":
        p = f"{m.group(1).upper()}:/{m.group(2)}"
    try:
        if not os.path.isabs(p) or not os.path.isfile(p) or os.path.getsize(p) > 65536:
            return ""
        with open(p, encoding="utf-8", errors="replace") as fh:
            return _first_line(fh.read())
    except Exception:
        return ""


def _commit_subject(rest: str) -> str:
    """The subject of one ``git commit``, from the text after the word
    ``commit``: ``-m``/``--message`` (a plain string, or the first line of
    a heredoc inside ``"$(cat <<'EOF'``, or of a PowerShell here-string),
    ``-F -`` with a heredoc, or ``-F <file>``. '' when unreadable or when
    the commit reuses a message (``--no-edit``)."""
    toks = _shell_tokens(rest)
    for k, (val, _start, end, quoted) in enumerate(toks):
        if quoted:
            continue
        nxt = toks[k + 1] if k + 1 < len(toks) else None
        msg, mend, mquoted = None, end, False
        if val in ("-m", "--message") or re.fullmatch(r"-[a-zA-Z]+m", val):  # -m, -am, -qm
            if nxt is None:
                return ""
            msg, mend, mquoted = nxt[0], nxt[2], nxt[3]
        elif val.startswith("--message="):
            msg, mend, mquoted = val[len("--message="):], end, False
        elif re.fullmatch(r"-m\S+", val):
            msg, mend, mquoted = val[2:], end, False
        if msg is not None:
            if not mquoted and msg in ("@'", '@"'):  # a PowerShell here-string
                return _first_line(rest[mend:], stop=msg[1] + "@")
            if "<<" in msg:  # "$(cat <<'EOF' ... EOF)"
                hd = _HEREDOC.search(msg)
                body = msg.split("\n", 1)[1] if "\n" in msg else ""
                return _first_line(body, stop=hd.group(1) if hd else "")
            return _first_line(msg)
        if val in ("-F", "--file") or val.startswith("--file="):
            target = val[len("--file="):] if val.startswith("--file=") else (nxt[0] if nxt else "")
            if target == "-":  # -F - <<'EOF'
                hd = _HEREDOC.search(rest)
                if not hd:
                    return ""
                body = rest[hd.end():].split("\n", 1)
                return _first_line(body[1] if len(body) > 1 else "", stop=hd.group(1))
            return _message_file(target)
    return ""


def commit_subjects(command: str) -> list:
    """Every git commit's subject in one shell command (``git commit-tree``
    and a commit with no readable message are skipped)."""
    out = []
    for m in _GIT_COMMIT.finditer(str(command or "")):
        try:
            subj = _commit_subject(command[m.end():])
        except Exception:
            subj = ""
        if subj:
            out.append(subj)
    return out


def _typed_prompt(d: dict) -> str:
    if d.get("isMeta") or d.get("isCompactSummary"):
        return ""
    c = (d.get("message") or {}).get("content")
    if not isinstance(c, str):
        return ""
    s = c.strip()
    if not s or s.startswith(("<", "/", "[Request interrupted", "Caveat:")):
        return ""
    return s


def _is_compaction(d: dict) -> bool:
    return bool(d.get("isCompactSummary")) or (d.get("type") == "system" and d.get("subtype") == "compact_boundary")


def read_transcript(transcript_path: str, since: float = 0.0) -> dict:
    """What the live chain records: edits, tasks, commits, the first typed
    prompt and the last assistant text, whether a compaction happened, and
    the earliest record's time. One read of the tail. A task completed or a
    commit made before ``since`` (epoch seconds; the previous checkpoint's
    creation) is not this record's news and is left out of ``completed``;
    open tasks, edits and the texts are read whatever their time."""
    out: dict = {"files": [], "open_tasks": [], "done_tasks": [], "current_task": "", "commits": [],
                 "first_prompt": "", "last_text": "", "completed": [], "compacted": False, "earliest": 0.0}
    if not transcript_path:
        return out
    try:
        from windvane.transcript import TAIL_BYTES, live_records

        recs = live_records(transcript_path, TAIL_BYTES)
    except Exception:
        return out
    edits: dict = {}
    tasks: dict = {}  # id -> {subject, status, at, started}
    creates: dict = {}  # tool_use_id -> subject
    shell: dict = {}  # tool_use_id -> (pos, [subjects])
    commits: list = []
    pos = 0
    ts = 0.0
    for d in recs:
        typ = d.get("type")
        content = (d.get("message") or {}).get("content")
        rec_ts = _epoch(d.get("timestamp"))
        if rec_ts and (not out["earliest"] or rec_ts < out["earliest"]):
            out["earliest"] = rec_ts
        ts = rec_ts or ts
        if _is_compaction(d):
            out["compacted"] = True
        if typ == "user":
            if not out["first_prompt"]:
                out["first_prompt"] = _typed_prompt(d)
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                tid = b.get("tool_use_id")
                if tid in creates:
                    m = _TASK_NUM.search(_result_text(b))
                    num = m.group(1) if m else ""
                    if not num:
                        tur = d.get("toolUseResult")
                        task = tur.get("task") if isinstance(tur, dict) else None
                        num = str((task or {}).get("id") or "") if isinstance(task, dict) else ""
                    if num:
                        pos += 1
                        tasks[num] = {"subject": creates.pop(tid), "status": "pending", "at": pos, "created": pos, "started": 0}
                elif tid in shell and not b.get("is_error"):
                    at, subs = shell.pop(tid)
                    if ts >= since:
                        commits.extend((at, s) for s in subs)
            continue
        if typ != "assistant" or not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and str(b.get("text") or "").strip():
                out["last_text"] = str(b["text"])
                continue
            if b.get("type") != "tool_use":
                continue
            name = str(b.get("name") or "")
            _ti = b.get("input")
            ti: dict = _ti if isinstance(_ti, dict) else {}
            pos += 1
            if name in _EDIT_TOOLS:
                fp = str(ti.get("file_path") or ti.get("notebook_path") or "").strip()
                if fp:
                    edits.pop(fp, None)
                    edits[fp] = True
            elif name == "TaskCreate":
                subj = str(ti.get("subject") or "").strip()
                if subj:
                    creates[b.get("id")] = subj
            elif name == "TaskUpdate":
                num = str(ti.get("taskId") or "").strip()
                t = tasks.get(num)
                if t is None:
                    continue
                if str(ti.get("subject") or "").strip():
                    t["subject"] = str(ti["subject"]).strip()
                st = str(ti.get("status") or "")
                if st == "deleted":
                    tasks.pop(num, None)
                elif st in ("pending", "in_progress", "completed"):
                    t["status"] = st
                    t["at"] = pos
                    t["when"] = ts
                    if st == "in_progress":
                        t["started"] = pos
            elif name in _SHELL_TOOLS:
                subs = commit_subjects(str(ti.get("command") or ""))
                if subs:
                    shell[b.get("id")] = (pos, subs)
    out["files"] = list(edits)[-MAX_FILES:]
    out["open_tasks"] = [t["subject"] for t in sorted(tasks.values(), key=lambda t: t["created"])
                         if t["status"] in ("pending", "in_progress")]
    running = [t for t in tasks.values() if t["status"] == "in_progress"]
    if running:
        out["current_task"] = max(running, key=lambda t: t["started"])["subject"]
    done = sorted((t["at"], t["subject"]) for t in tasks.values()
                  if t["status"] == "completed" and float(t.get("when") or 0.0) >= since)
    made = [(a, f"commit: {s}") for a, s in commits]
    out["done_tasks"] = [s for _a, s in done]
    out["commits"] = [s for _a, s in made]
    out["completed"] = [s for _a, s in sorted(done + made, key=lambda x: x[0])]
    return out


# ── the hook state ──────────────────────────────────────────────────────────


def _state_completed(state: dict) -> list:
    out = []
    try:
        runs = int(state.get("test_runs_this_session") or 0)
        if runs:
            last = state.get("last_test_passed")
            verdict = ", last passed" if last is True else (", last failed" if last is False else "")
            out.append(f"tests: {runs} run{'s' if runs != 1 else ''}{verdict}")
    except Exception:
        pass
    try:
        ps = state.get("pressure")
        mp = ps.get("milestone_pending") if isinstance(ps, dict) else None
        quote = str((mp or {}).get("quote") or "").strip() if isinstance(mp, dict) else ""
        if quote:
            out.append(_strip_emphasis(quote))
    except Exception:
        pass
    return out


def _session_started(state: dict, tr: dict) -> float:
    """When this session started: the hooks' record of its first start,
    else the earliest record on the live chain."""
    try:
        run = state.get("run") if isinstance(state, dict) else None
        started = float((run if isinstance(run, dict) else {}).get("started_at") or 0.0)
        if started:
            return started
    except (TypeError, ValueError):
        pass
    return float(tr.get("earliest") or 0.0)


def _session_context(work_project: str) -> dict:
    try:
        return _hook("_get_session_context_for_handoff")(work_project) or {}
    except Exception:
        return {}


# ── the draft ───────────────────────────────────────────────────────────────


def work_project_of(project_dir: str, state: Optional[dict] = None) -> str:
    try:
        return _hook("session_project")(project_dir, state if isinstance(state, dict) else None) or project_dir
    except Exception:
        return project_dir


def draft_with_context(project_dir: str, session_id: str, transcript_path: str, state: dict) -> tuple:
    """(record, session context): the context is what
    ``_get_session_context_for_handoff`` returned, so the PreCompact hook
    reuses its mistakes without a second read."""
    st = state if isinstance(state, dict) else {}
    rec: dict = {f: ("" if f in STRING_FIELDS else []) for f in FIELDS}
    src: dict = {}
    ctx: dict = {}
    work_project = project_dir
    try:
        work_project = work_project_of(project_dir, st)
        prev, prev_src = _previous(project_dir, work_project, session_id, transcript_path)
        # Completed since the previous record of this session: what closed
        # before it was its news, and is already in its completed list.
        since = 0.0
        if prev_src == "previous checkpoint":
            since = float(prev.get("created") or prev.get("created_at") or prev.get("timestamp") or 0.0)
        tr = read_transcript(transcript_path, since)
        ctx = _session_context(work_project)
        sentences = _closing_sentences(tr["last_text"])

        # Another session's record (the project fallback) lends only what is
        # true of the project: its warnings and context needed.
        own = prev_src == "previous checkpoint"

        # completed: the task list and the commits in transcript order, then
        # the hook state's test runs and staged claim, then the carried
        # steps the closing reply says are done.
        completed = list(tr["completed"])
        extra = _state_completed(st)
        completed = _dedupe(completed + extra)
        done_norm = [_norm(s) for s in completed]
        carried_all = [s for s in (_pick(prev, "next_steps", "pending_steps") if own else [])
                       if _norm(s) not in _TRIVIAL and not _closes(s, done_norm)]
        said = [s for s in carried_all if _said_done(s, sentences)]
        if said:
            completed = _dedupe(completed + said)
        if completed:
            rec["completed_steps"] = completed
            names = [n for n, v in (("task list", tr["done_tasks"]), ("commits", tr["commits"])) if v]
            names += (["hook state"] if extra else []) + (["closing reply"] if said else [])
            src["completed_steps"] = "+".join(names)

        # The task: the session's own previous record, unless a compaction
        # carried it from a record older than the session itself (then the
        # title belongs to earlier work and the first typed prompt names
        # this session's); the first typed prompt when nothing carries.
        started = _session_started(st, tr)
        stale_title = (
            own
            and tr["compacted"]
            and started > 0
            and float(prev.get("created") or prev.get("created_at") or prev.get("timestamp") or 0.0) < started
            and bool(tr["first_prompt"])
        )
        if own and prev.get("task_description") and not stale_title:
            rec["task_description"] = str(prev["task_description"])
            src["task_description"] = prev_src
        elif tr["first_prompt"]:
            rec["task_description"] = _cut(tr["first_prompt"], TASK_CHARS)
            src["task_description"] = "first prompt"

        for fld, keys in (("handoff_warnings", ("warnings", "handoff_warnings")),
                          ("handoff_context_needed", ("context_needed", "handoff_context_needed"))):
            got = _pick(prev, *keys)
            if got:
                rec[fld] = got
                src[fld] = prev_src

        carried = [s for s in carried_all if s not in said]
        pending = _dedupe(list(tr["open_tasks"]) + carried)
        if pending:
            rec["pending_steps"] = pending
            src["pending_steps"] = "+".join(n for n, v in (("task list", tr["open_tasks"]), (prev_src, carried)) if v)

        if tr["files"]:
            rec["files_involved"] = list(tr["files"])
            src["files_involved"] = "transcript edits"
        elif own:
            got = _pick(prev, "files_in_progress", "files_involved")
            if got:
                rec["files_involved"] = got[-MAX_FILES:]
                src["files_involved"] = prev_src

        closing = _closing(tr["last_text"])
        if closing:
            rec["handoff_summary"] = closing
            src["handoff_summary"] = "closing reply"
        elif own:
            prev_summary = str(prev.get("summary") or prev.get("handoff_summary") or "")
            if prev_summary:
                rec["handoff_summary"] = prev_summary
                src["handoff_summary"] = prev_src

        if tr["current_task"]:
            rec["current_step"] = tr["current_task"]
            src["current_step"] = "task list"
        elif closing and _NEXT_CUE.search(closing):
            rec["current_step"] = closing
            src["current_step"] = "closing reply"

        decisions = _items(ctx.get("decisions"))
        if decisions:
            rec["key_decisions"] = decisions
            src["key_decisions"] = "hook state"
    except Exception:
        pass
    rec["metadata"] = {"draft": True, "draft_sources": src, "project_path": work_project}
    return rec, ctx


def draft(project_dir: str, session_id: str, transcript_path: str, state: dict) -> dict:
    """The checkpoint record the recorder drafts for this session (see the
    module docstring for the sources). Never raises."""
    try:
        return draft_with_context(project_dir, session_id, transcript_path, state)[0]
    except Exception:
        rec: dict = {f: ("" if f in STRING_FIELDS else []) for f in FIELDS}
        rec["metadata"] = {"draft": True, "draft_sources": {}, "project_path": project_dir}
        return rec


def ring_record(record: dict, session_id: str = "", trigger: str = "bank") -> dict:
    """The draft in the ring's vocabulary, as an automatic entry (the
    PreCompact hook's shape)."""
    md = record.get("metadata") or {}
    return {
        "created": time.time(),
        "kind": "auto",
        "trigger": trigger,
        "session_id": session_id,
        "project_path": md.get("project_path", ""),
        "task_description": record.get("task_description", ""),
        "current_step": record.get("current_step", ""),
        "completed_steps": list(record.get("completed_steps") or []),
        "pending_steps": list(record.get("pending_steps") or []),
        "next_steps": list(record.get("pending_steps") or []),
        "files_in_progress": list(record.get("files_involved") or []),
        "warnings": list(record.get("handoff_warnings") or []),
        "context_needed": list(record.get("handoff_context_needed") or []),
        "decisions": list(record.get("key_decisions") or []),
        "summary": record.get("handoff_summary", ""),
        "metadata": {"draft": True, "draft_sources": dict(md.get("draft_sources") or {})},
    }


def render(record: dict) -> list:
    """The draft as the compaction banner would show it
    (``_format_restored_full`` on a ring-shaped copy)."""
    try:
        ring = ring_record(record)
        ring["kind"] = "draft"
        return list(_hook("_format_restored_full")(ring))
    except Exception:
        return []


def summary_line(entry: dict) -> str:
    """One line for a banked record: what it holds and where it went."""
    task = _cut(entry.get("task_description") or entry.get("summary") or "(no task)", 100)
    parts = [f"Banked the drafted checkpoint ({entry.get('trigger', 'bank')}): {task}"]
    n_done = len(entry.get("completed_steps") or [])
    n_next = len(entry.get("next_steps") or [])
    parts.append(f"{n_done} done, {n_next} pending")
    if entry.get("files_in_progress"):
        parts.append(f"{len(entry['files_in_progress'])} files")
    return "; ".join(parts) + "."


def session_inputs(session_id: str = "") -> tuple:
    """(session id, hook state, transcript path) the way the hooks and
    ``windvane.brief`` resolve them: the id given, else the one Claude Code
    exports; the transcript from the session's state, else the file Claude
    Code writes for that id."""
    from windvane.checkpoints import session_id as _sid

    if session_id:
        _set_session(session_id)
    sid = session_id or _sid()
    if sid and not session_id:
        _set_session(sid)
    try:
        state = _hook("load_state")()
    except Exception:
        state = {}
    if not isinstance(state, dict):
        state = {}
    run = state.get("run")
    tp = str((run if isinstance(run, dict) else {}).get("transcript_path") or "")
    if not tp and sid:
        try:
            from windvane.transcript import transcript_for_session

            found = transcript_for_session(sid)
            tp = str(found) if found else ""
        except Exception:
            tp = ""
    return sid, state, tp


def bank(record: dict, session_id: str, trigger: str = "bank") -> dict:
    """Write the draft to the ring as the PreCompact hook does; the ring
    entry written."""
    from windvane import checkpoints as ck

    entry = ring_record(record, session_id, trigger)
    wp = entry["project_path"]
    ck.write_handoff(entry, [ck.project_ring_dir(wp), ck.global_ring_dir()])
    return entry


def main(argv: "Optional[list]" = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m windvane.draft", description="The checkpoint the recorder drafts.")
    ap.add_argument("--project", required=True, help="the session's working directory")
    ap.add_argument("--session", default="", help="the Claude Code session id (default: CLAUDE_CODE_SESSION_ID)")
    ap.add_argument("--transcript", default="", help="the transcript (default: the session's, from its state)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="print the record as JSON")
    mode.add_argument("--text", action="store_true", help="print the record as the banner renders it (the default)")
    ap.add_argument("--bank", action="store_true", help="write the draft to the ring as an automatic entry and print it")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        sid, state, tp = session_inputs(args.session)
        record = draft(str(Path(args.project)), sid, args.transcript or tp, state)
        if args.bank:
            sys.stdout.write(json.dumps(bank(record, sid)) + "\n")
        elif args.json:
            sys.stdout.write(json.dumps(record) + "\n")
        else:
            lines = render(record)
            if lines:
                sys.stdout.write("\n".join(lines) + "\n")
    except Exception as e:
        print(f"draft failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

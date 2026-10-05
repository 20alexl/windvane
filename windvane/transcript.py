"""The live branch of a Claude Code transcript.

A rewind (Esc Esc, /rewind) fires no hook and writes no record. The
transcript is append-only: the abandoned turns stay in the file and the
next prompt is appended with the parentUuid of the record the user rewound
to, so a rewind shows only as a FORK. Everything windvane derives from the
transcript (the checkpoint to restore, the decisions to mine) has to follow
the live chain: the walk from the last record up its parent links. Measured
on a real rewind, 2026-09-26: after "restore code and conversation" the file
on disk was back at the earlier version while the ring's newest checkpoint,
saved on the abandoned branch under the same session id, was still returned
by the checkpoint restore.

Two more shapes the walk has to know (measured 2026-10-03 on eleven
compacted transcripts, 192 boundaries):

- A compaction. Its ``compact_boundary`` record has parentUuid null and
  names the last preserved record before it as ``logicalParentUuid``; its
  ``compactMetadata.preservedMessages.uuids`` lists the preserved tail of the
  conversation in order. Usually that last record is on disk before the
  boundary. When it was not yet flushed at compaction time it is written
  AFTER the compact summary with the summary as its parent, so the logical
  parent sits on the new chain; the walk then resumes at the newest
  preserved record written before the boundary. No uuid occurs twice in any
  of the eleven files (the preserved tail is not re-appended).
- A tool result whose turn went on with another tool call. The next
  assistant record hangs off the assistant record holding the tool_use, not
  off the result, so the result record sits on a side branch (6,751 of the
  55,863 tool results measured; a checkpoint save result among them read as
  rewound with no rewind at all). A user record whose parent is live and
  which answers a tool_use on a live record is live. A rewind cannot split
  a tool call from its result: it forks at a typed prompt.
- A record can be written before its parent (an attachment flushed early;
  62 records in three files), so a parent is looked up wherever it sits.

Stdlib only: the hooks import this under `python -S`.
"""

from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path
from typing import Iterable, Optional

_TASK_ID = re.compile(rb"task_id: (task_\d+)")
# The banner reads the tail only: a hook has 1-2 s, a transcript can be
# hundreds of MB, and a rewind concerns the recent turns by nature. The
# walk ends where a parent falls outside the window.
TAIL_BYTES = 8_000_000


def _tail_lines(path: str | Path, tail_bytes: Optional[int]) -> list[bytes]:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if tail_bytes is not None and size > tail_bytes:
                fh.seek(size - tail_bytes)
                fh.readline()  # drop the partial line
            data = fh.read()
    except Exception:
        return []
    return data.splitlines()


def _records(path: str | Path, tail_bytes: Optional[int]) -> list[tuple[bytes, dict]]:
    """Every record with a uuid, in file order. The chain runs through the
    system and attachment records too (a prompt's parent is often the
    previous turn's turn_duration record), so no type is skipped here."""
    out = []
    for raw in _tail_lines(path, tail_bytes):
        if b'"uuid"' not in raw:
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if isinstance(d, dict) and d.get("uuid"):
            out.append((raw, d))
    return out


def _blocks(d: dict, kind: str) -> list:
    content = (d.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict) and b.get("type") == kind] if isinstance(content, list) else []


def _resume_at(d: dict, cur: int, where: dict, walked: set) -> Optional[int]:
    """Where the walk goes from record ``d`` at position ``cur``: its parent
    (the latest occurrence before it, else the first after it, never one
    already walked); at a compaction boundary, the newest of its logical
    parent and its preserved records that was written before it."""
    p = d.get("parentUuid")
    if p:
        occ = [j for j in where.get(p, ()) if j not in walked]
        before = [j for j in occ if j < cur]
        if before:
            return before[-1]
        after = [j for j in occ if j > cur]
        return after[0] if after else None
    if d.get("subtype") != "compact_boundary":
        return None
    meta = d.get("compactMetadata")
    kept = meta.get("preservedMessages") if isinstance(meta, dict) else None
    uuids = kept.get("uuids") if isinstance(kept, dict) else None
    for u in [d.get("logicalParentUuid"), *reversed(uuids if isinstance(uuids, list) else [])]:
        before = [j for j in where.get(u, ()) if j < cur and j not in walked] if isinstance(u, str) else []
        if before:
            return before[-1]
    return None


def _walk(recs: list) -> set:
    """Positions in ``recs`` (main-chain records with a uuid, file order) on
    the live branch: from the last user or assistant record up its parent
    links, through every compaction, plus the tool results that sit beside
    the chain (see the module doc). Empty when there is no such record."""
    where: dict = {}
    last = None
    for i, d in enumerate(recs):
        where.setdefault(d["uuid"], []).append(i)
        if d.get("type") in ("user", "assistant"):
            last = i
    walked: set = set()
    cur = last
    while cur is not None and cur not in walked:
        walked.add(cur)
        cur = _resume_at(recs[cur], cur, where, walked)
    if not walked:
        return walked
    live_uuids = {recs[i]["uuid"] for i in walked}
    live_calls = {b.get("id") for i in walked if recs[i].get("type") == "assistant" for b in _blocks(recs[i], "tool_use")}
    for i, d in enumerate(recs):
        if (i not in walked and d.get("type") == "user" and d.get("parentUuid") in live_uuids
                and any(b.get("tool_use_id") in live_calls for b in _blocks(d, "tool_result"))):
            walked.add(i)
    return walked


def _main_chain(records: Iterable[dict]) -> list:
    # a sidechain is a subagent's chain, not this conversation's
    return [d for d in records if isinstance(d, dict) and d.get("uuid") and not d.get("isSidechain")]


def chain_of(records: Iterable[dict]) -> Optional[set]:
    """The uuids on the live branch of ``records`` (file order; system and
    attachment records included, since the chain runs through them), across
    compactions; see ``_walk``. None when there are no records to walk."""
    recs = _main_chain(records)
    walked = _walk(recs)
    return {recs[i]["uuid"] for i in walked} if walked else None


def live_records(path: str | Path, tail_bytes: Optional[int] = TAIL_BYTES) -> list[dict]:
    """The records on the live branch, in file order: the same walk as
    ``chain_of``, returning the records. Read once; [] when the file cannot
    be read or holds no chain."""
    recs = _main_chain(d for _raw, d in _records(path, tail_bytes))
    return [recs[i] for i in sorted(_walk(recs))]


def live_chain(path: str | Path, tail_bytes: Optional[int] = None) -> Optional[set]:
    """The uuids on the live branch of the transcript at ``path`` (the whole
    file by default; ``tail_bytes`` bounds the read for a hook). None when
    the file cannot be read or holds no chain."""
    recs = _records(path, tail_bytes)
    return chain_of(d for _raw, d in recs) if recs else None


# The checkpoint tool (``mcp__windvane__checkpoint``) and its operation that
# saves one; the save result prints ``task_id: task_N``.
_SAVE_TOOL_SUFFIX = "checkpoint"
_SAVE_OPS = ("save",)


def branch_checkpoints(path: str | Path, tail_bytes: Optional[int] = None) -> tuple[set, set]:
    """(task ids SAVED on the live branch, task ids saved anywhere in the
    file): the ``task_id: task_N`` a checkpoint(save) result printed, matched
    to its tool call by id. A checkpoint(restore) result quotes a task id too
    (the first live read counted a rewound checkpoint as live because the
    later restore, on the live branch, printed its id)."""
    recs = _records(path, tail_bytes)
    live = chain_of(d for _raw, d in recs) or set()
    saves: set = set()
    for _raw, d in recs:
        if d.get("type") != "assistant":
            continue
        content = (d.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if (isinstance(block, dict) and block.get("type") == "tool_use"
                    and str(block.get("name") or "").endswith(_SAVE_TOOL_SUFFIX)
                    and (block.get("input") or {}).get("operation") in _SAVE_OPS):
                saves.add(block.get("id"))
    on_branch: set = set()
    everywhere: set = set()
    for raw, d in recs:
        if d.get("type") != "user" or b"task_id: task_" not in raw:
            continue
        content = (d.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if not (isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id") in saves):
                continue
            for m in _TASK_ID.finditer(json.dumps(block).encode()):
                tid = m.group(1).decode()
                everywhere.add(tid)
                if d["uuid"] in live:
                    on_branch.add(tid)
    return on_branch, everywhere


def rewound_away(entry: dict, path: str | Path | None, tail_bytes: Optional[int] = TAIL_BYTES) -> bool:
    """True when the ring entry's save is in this transcript but NOT on its
    live branch: the user rewound past it. An entry the transcript never
    saved (an auto checkpoint from a hook, a save from another session, a
    transcript that cannot be read) is not judged."""
    tid = str((entry or {}).get("task_id") or "")
    if not tid or not path:
        return False
    try:
        on_branch, everywhere = branch_checkpoints(path, tail_bytes)
    except Exception:
        return False
    return tid in everywhere and tid not in on_branch


def transcript_for_session(session_id: str) -> Optional[Path]:
    """The transcript Claude Code writes for a session id, wherever its
    project dir is (~/.claude/projects/<slug>/<session_id>.jsonl)."""
    sid = str(session_id or "").strip()
    if not sid:
        return None
    root = Path.home() / ".claude" / "projects"
    hits = glob.glob(str(root / "*" / f"{sid}.jsonl"))
    return Path(hits[0]) if hits else None

"""The session miner (windvane.mining): the JSONL reader, the session index,
the extractors, cross-session search, patterns and the background runner.

Synthetic transcripts only; every test runs on a temporary store with
WINDVANE_NO_DAEMON set, and the embedding transport is patched to a
deterministic local function wherever vectors are needed.
"""

import importlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from windvane.mining import extractors, patterns, search
from windvane.mining.jsonl_reader import (
    dir_name_to_path,
    extract_assistant_text,
    extract_file_edits,
    extract_thinking,
    extract_tool_uses,
    extract_user_text,
    iter_messages,
    path_to_dir_name,
    read_tail,
)
from windvane.mining.session_index import (
    SessionIndex,
    build_index_for_session,
    merge_session_meta,
)

ROOT = Path(__file__).resolve().parents[1]
MINING = ROOT / "windvane" / "mining"


def _has(module: str, *attrs: str) -> bool:
    try:
        mod = importlib.import_module(module)
    except Exception:
        return False
    return all(hasattr(mod, a) for a in attrs)


needs_paths = pytest.mark.skipif(
    not _has("windvane.paths", "get_windvane_storage_dir", "resolve_project_for_file", "target_project_for_files", "_normalize_path"),
    reason="windvane.paths (port-hooks) not in the tree yet")
needs_capture = pytest.mark.skipif(
    not _has("windvane.capture", "looks_like_decision", "bare", "capture_decision"),
    reason="windvane.capture (port-hooks) not in the tree yet")
needs_proc_lock = pytest.mark.skipif(
    not _has("windvane.proc_lock", "acquire", "held"), reason="windvane.proc_lock (port-hooks) not in the tree yet")


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    monkeypatch.delenv("WINDVANE_SESSION_RETENTION_DAYS", raising=False)
    monkeypatch.setenv("WINDVANE_SEMANTIC", "0")


# ── synthetic transcripts ────────────────────────────────────────────────


def _sid():
    return str(uuid.uuid4())


def _ts(offset_min=0):
    return datetime.fromtimestamp(time.time() + offset_min * 60, tz=timezone.utc).isoformat()


def _user(text, sid, offset_min=0):
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}, "sessionId": sid, "timestamp": _ts(offset_min)})


def _assistant(text, sid, offset_min=0, tool_use=None, thinking=None):
    content = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking})
    content.append({"type": "text", "text": text})
    if tool_use:
        content.append({"type": "tool_use", "id": f"tu_{uuid.uuid4().hex[:8]}", "name": tool_use["name"], "input": tool_use.get("input", {})})
    return json.dumps({"type": "assistant", "message": {"role": "assistant", "content": content}, "sessionId": sid, "timestamp": _ts(offset_min)})


def _result(content, sid, is_error=False, offset_min=0):
    return json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": content, "is_error": is_error}]},
                       "sessionId": sid, "timestamp": _ts(offset_min)})


def _session(tmpdir, sid=None, n=10) -> Path:
    sid = sid or _sid()
    path = Path(tmpdir) / f"{sid}.jsonl"
    lines = []
    for i in range(n):
        lines.append(_user(f"User message {i}: please fix the auth module", sid, i))
        lines.append(_assistant(f"I'll fix the auth module. Here's the change for step {i}.", sid, i,
                                tool_use={"name": "Edit", "input": {"file_path": "/project/src/auth.py", "old_string": f"old_code_{i}", "new_string": f"new_code_{i}"}},
                                thinking=f"Thinking about step {i}: need to update auth logic"))
        lines.append(_result("File edited successfully", sid, offset_min=i))
    lines.append(_user("Run the tests now", sid, n))
    lines.append(_assistant("Running tests.", sid, n, tool_use={"name": "Bash", "input": {"command": "pytest tests/"}}))
    lines.append(_result("TypeError: expected str got int\n  File auth.py line 42", sid, is_error=True, offset_min=n))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ── the reader ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("path,expected", [
    (r"C:\repo", "C--repo"),
    (r"e:\projects", "e--projects"),
    (r"d:\Code\mini_cl", "d--Code-mini_cl"),
    (r"C:\repo\service-b", "C--repo-service-b"),
    ("/home/user/project", "-home-user-project"),
])
def test_path_conversion(path, expected):
    assert path_to_dir_name(path) == expected


def test_dir_names_convert_back_best_effort():
    assert dir_name_to_path("C--projects") == "C:\\projects"
    assert dir_name_to_path("-home-user-project") == "/home/user/project"


def test_the_streaming_parser_and_the_tail(tmp_path: Path):
    f = _session(tmp_path, n=10)
    msgs = list(iter_messages(f))
    assert len(msgs) == 33 and {m.get("type") for _, m in msgs} == {"user", "assistant"}
    stats = {}
    list(iter_messages(f, stats=stats))
    assert stats["lines"] == stats["known_types"] == 33
    assert len(list(iter_messages(f, types={"assistant"}))) == 11
    big = _session(tmp_path, n=50)
    assert len(read_tail(big, n_messages=20)) == 20


def test_the_content_extractors(tmp_path: Path):
    counts = {"user": 0, "text": 0, "tools": 0, "edits": 0, "thinking": 0}
    for _, msg in iter_messages(_session(tmp_path, n=5)):
        counts["user"] += bool(extract_user_text(msg))
        counts["text"] += len(extract_assistant_text(msg))
        counts["tools"] += len(extract_tool_uses(msg))
        counts["edits"] += len(extract_file_edits(msg))
        counts["thinking"] += len(extract_thinking(msg))
    assert counts == {"user": 6, "text": 6, "tools": 6, "edits": 5, "thinking": 5}


# ── the session index ────────────────────────────────────────────────────


def test_the_session_index_persists_and_skips_what_it_saw(tmp_path: Path):
    f = _session(tmp_path, n=5)
    meta = build_index_for_session(f)
    assert meta.session_id and meta.message_count == 18 and meta.files_edited == ["/project/src/auth.py"]
    assert meta.prompt_count == 6 and meta.error_count == 1
    idx = SessionIndex(tmp_path / "index" / "session_index.json")
    idx.update_session(meta)
    idx.save()
    again = SessionIndex(tmp_path / "index" / "session_index.json")
    assert again.get_session(meta.session_id) is not None
    assert again.get_latest_session_summary() is not None
    assert again.needs_processing(f) == (False, 0)


def test_a_grown_session_merges_instead_of_replacing(tmp_path: Path):
    sid = _sid()
    f = _session(tmp_path, sid, n=5)
    idx = SessionIndex(tmp_path / "index" / "session_index.json")
    first = build_index_for_session(f)
    idx.update_session(first)
    idx.save()
    bigger = _session(tmp_path, sid, n=9)
    needs, offset = idx.needs_processing(bigger)
    assert needs and offset > 0
    tail = build_index_for_session(bigger, start_offset=offset)
    tail_count = tail.message_count
    merged = merge_session_meta(idx.get_by_jsonl_file(bigger.name), tail)
    idx.update_session(merged)
    entry = idx.get_by_jsonl_file(bigger.name)
    assert entry["message_count"] == first.message_count + tail_count > first.message_count
    assert set(first.files_edited) <= set(entry["files_edited"])
    assert entry["first_timestamp"] and entry["session_id"]


def test_the_latest_session_is_picked_per_project(tmp_path: Path):
    idx = SessionIndex(tmp_path / "session_index.json")
    idx._data["sessions"] = {
        "s-ui": {"last_timestamp": "2026-09-11T09:00:00Z", "files_edited": ["E:/ws/web/page.tsx", "E:/ws/web/app.css"]},
        "s-api": {"last_timestamp": "2026-09-11T08:00:00Z", "files_edited": ["E:/ws/api/src/a.py", "C:/Users/x/.claude/memory.md"]},
    }
    latest = idx.get_latest_session()
    assert latest is not None and latest["files_edited"][0].endswith("page.tsx")
    s = idx.get_latest_session_summary("E:/ws/api")
    assert s is not None and s["files_edited"] == ["a.py"]
    assert s["file_count"] == 1  # the memory file outside the project is not counted
    assert idx.get_latest_session_summary("E:/ws/other") is None


@needs_paths
def test_a_workspace_wide_last_session_narrows_to_its_main_sub_project(tmp_path: Path):
    root = tmp_path / "ws"
    for name in ("alpha", "beta"):
        (root / name).mkdir(parents=True)
        (root / name / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    idx = SessionIndex(tmp_path / "session_index.json")
    files = [str(root / "alpha" / f"a{i}.py") for i in range(5)] + [str(root / "beta" / "b.py")] + [str(tmp_path / "outside" / "memory.md")]
    idx._data["sessions"] = {"s1": {"session_id": "s1", "last_timestamp": "2026-09-22T10:00:00Z", "files_edited": files, "git_branch": "main"}}
    wide = idx.get_latest_session_summary("")
    assert wide and wide["file_count"] == 7 and wide["project_label"] == ""
    narrowed = idx.get_latest_session_summary("", workspace_root=str(root))
    assert narrowed and narrowed["project_label"] == "alpha" and narrowed["file_count"] == 5
    scoped = idx.get_latest_session_summary(str(root / "beta"), workspace_root=str(root))
    assert scoped and scoped["file_count"] == 1 and scoped["project_label"] == ""


# ── search ───────────────────────────────────────────────────────────────


def test_tool_use_and_result_pairs_become_chunks():
    sid = _sid()
    bash = json.loads(_assistant("Running tests.", sid, tool_use={"name": "Bash", "input": {"command": "pytest tests/ -v"}}))
    ok = json.loads(_result("PASSED: 5 tests\nFAILED: 2 tests\ntest_auth.py::test_login FAILED", sid))
    chunks = search._extract_tool_chunks(bash, ok, sid, "t.jsonl", 1, _ts())
    assert len(chunks) == 1 and "pytest" in chunks[0][0].preview and chunks[0][0].msg_type == "tool"
    err = json.loads(_result("TypeError: expected str got int\n  File auth.py, line 42", sid, is_error=True))
    assert "TypeError" in search._extract_tool_chunks(bash, err, sid, "t.jsonl", 2, _ts())[0][0].preview
    edit = json.loads(_assistant("Fixing auth.", sid, tool_use={"name": "Edit", "input": {
        "file_path": "/project/src/auth.py", "old_string": "def validate(token):", "new_string": "def validate(token: str) -> bool:"}}))
    chunks = search._extract_tool_chunks(edit, json.loads(_result("File edited.", sid)), sid, "t.jsonl", 3, _ts())
    assert len(chunks) == 1 and chunks[0][0].related_files == ["auth.py"] and "validate" in chunks[0][0].preview
    read = json.loads(_assistant("Reading config.", sid, tool_use={"name": "Read", "input": {"file_path": "/project/config.yaml"}}))
    assert search._extract_tool_chunks(read, json.loads(_result("key: value", sid)), sid, "t.jsonl", 4, _ts()) == []
    plain = json.loads(_user("Just a question", sid))
    assert search._extract_tool_chunks(plain, plain, sid, "t.jsonl", 5, _ts()) == []


def test_hits_are_classified_by_what_they_are():
    assert search.classify_chunk("TypeError: bad") == "error"
    assert search.classify_chunk("we decided to use the registry") == "decision"
    assert search.classify_chunk("next step is the parser") == "next-step"
    assert search.classify_chunk("the parser reads the file") == "narration"


def _fake_vec(text, dim=8):
    import hashlib

    h = hashlib.md5(text.encode("utf-8", errors="replace")).digest()
    v = [b / 255.0 + 0.01 for b in h[:dim]]
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


def test_the_sharded_embedding_store(tmp_path: Path, monkeypatch):
    """Build, a watermarked re-run, growth, v1 migration and retention, with
    the embedding transport patched to a deterministic function."""
    np = pytest.importorskip("numpy")
    from windvane import daemon, semantic
    from windvane.semantic.config import embed_signature

    monkeypatch.setattr(semantic, "numpy_module", lambda: np)  # the tier is on

    storage = tmp_path / "store"
    jdir = tmp_path / "jsonl"
    jdir.mkdir()
    hash_dir = storage / "projects" / "abc12345"
    hash_dir.mkdir(parents=True)
    proj = "/proj/v2bench"
    (storage / "manifest.json").write_text(json.dumps({"version": 5, "projects": {search._normalize_path(proj): {"hash": "abc12345"}}}))
    monkeypatch.setattr(search, "resolve_jsonl_dir", lambda p: jdir)
    monkeypatch.setattr(daemon, "embed_batch_via_server", lambda texts: [_fake_vec(t) for t in texts])
    monkeypatch.setattr(daemon, "embed_via_server", lambda t: _fake_vec(t))

    sid = _sid()
    f = _session(jdir, sid, n=5)
    index = SessionIndex(hash_dir / "session_index.json")
    meta = build_index_for_session(f)
    meta.session_id = sid
    index.update_session(meta)
    index.save()
    assert search.build_session_embeddings(proj, index, str(storage)) > 0
    idx_path = hash_dir / "session_embeddings_index.json"
    idx = json.loads(idx_path.read_text())
    assert idx["version"] == 2 and idx["model"] == embed_signature()
    shards = list((hash_dir / "session_embeddings").glob("*.npy"))
    key = next(iter(idx["shards"]))
    assert len(shards) == 1 and np.load(str(shards[0])).shape[0] == len(idx["shards"][key]["chunks"])
    assert search.build_session_embeddings(proj, index, str(storage)) == 0  # watermark: a no-op

    existing = dict(index.sessions[sid])
    grown = _session(jdir, sid, n=9)
    tail = build_index_for_session(grown, start_offset=existing["processed_offset"])
    tail.session_id = sid
    index.update_session(merge_session_meta(existing, tail))
    index.save()
    assert search.build_session_embeddings(proj, index, str(storage)) > 0
    idx = json.loads(idx_path.read_text())
    chunks = [c for s in idx["shards"].values() for c in s["chunks"]]
    keys = [(c["jsonl_file"], c["msg_offset"], c["msg_type"], c["preview"]) for c in chunks]
    assert len(keys) == len(set(keys))
    assert np.load(str(hash_dir / "session_embeddings" / f"{key}.npy")).shape[0] == len(idx["shards"][key]["chunks"])

    assert search.search_sessions(proj, "auth module", limit=5, method="keyword", windvane_storage_dir=str(storage))
    assert search.search_sessions(proj, chunks[0]["preview"][:80], limit=3, method="semantic", windvane_storage_dir=str(storage))

    # v1 flat store of a second project migrates to shards
    hash_dir2 = storage / "projects" / "def67890"
    hash_dir2.mkdir()
    proj2 = "/proj/v1legacy"
    manifest = json.loads((storage / "manifest.json").read_text())
    manifest["projects"][search._normalize_path(proj2)] = {"hash": "def67890"}
    (storage / "manifest.json").write_text(json.dumps(manifest))
    v1 = [{"session_id": "old", "jsonl_file": "old.jsonl", "msg_offset": i + 1, "timestamp": f"2026-01-0{i + 1}T10:00:00Z",
           "msg_type": "user" if i % 2 == 0 else "assistant", "preview": f"legacy chunk {i} about quantum flux capacitors", "related_files": []}
          for i in range(4)]
    vecs = np.array([_fake_vec(c["preview"]) for c in v1], dtype=np.float32)
    np.save(str(hash_dir2 / "session_embeddings"), vecs)
    (hash_dir2 / "session_embeddings_index.json").write_text(json.dumps({"chunks": v1, "model": embed_signature()}))
    index2 = SessionIndex(hash_dir2 / "session_index.json")
    search.build_session_embeddings(proj2, index2, str(storage))
    idx2 = json.loads((hash_dir2 / "session_embeddings_index.json").read_text())
    assert idx2["version"] == 2 and "2026-01" in idx2["shards"]
    assert not (hash_dir2 / "session_embeddings.npy").exists()
    assert np.allclose(np.load(str(hash_dir2 / "session_embeddings" / "2026-01.npy")), vecs)
    assert search.search_sessions(proj2, "quantum flux capacitors", limit=3, method="hybrid", windvane_storage_dir=str(storage))

    monkeypatch.setenv("WINDVANE_SESSION_RETENTION_DAYS", "30")
    search.build_session_embeddings(proj2, index2, str(storage))
    idx2 = json.loads((hash_dir2 / "session_embeddings_index.json").read_text())
    assert "2026-01" not in idx2["shards"] and not (hash_dir2 / "session_embeddings" / "2026-01.npy").exists()


def test_without_the_semantic_tier_search_is_keyword_only(tmp_path: Path, monkeypatch):
    """The daemon's clients answer empty without the tier, so the store
    keeps no vectors and hybrid search still answers by keyword. The tier
    is off here whatever this machine's settings say (conftest)."""
    storage = tmp_path / "store"
    (storage / "projects" / "h1").mkdir(parents=True)
    (storage / "manifest.json").write_text(json.dumps({"projects": {search._normalize_path("/p"): {"hash": "h1"}}}))
    (storage / "projects" / "h1" / "session_embeddings_index.json").write_text(json.dumps({
        "version": 2, "model": "none", "shards": {"2026-09": {"chunks": [
            {"session_id": "s", "jsonl_file": "s.jsonl", "msg_offset": 1, "timestamp": "2026-09-01T00:00:00Z", "msg_type": "user",
             "preview": "switch the alias store to sqlite", "related_files": []}]}}, "session_progress": {}}))
    hits = search.search_sessions("/p", "alias store sqlite", method="hybrid", windvane_storage_dir=str(storage))
    assert hits and hits[0].chunk_text.startswith("switch the alias store")
    from windvane import semantic

    assert semantic.numpy_module() is None
    index = SimpleNamespace(sessions={"s": {"user_message_count": 1, "assistant_message_count": 0}})
    assert search.build_session_embeddings("/p", index, str(storage)) == 0  # no tier, no vectors


def test_keyword_search_matches_whole_words_weighted_by_rarity(tmp_path: Path):
    """"store" is not inside "restore", "the" scores nothing, and a word
    found in one chunk outweighs one found in both: measured on a 200k-chunk
    index, the substring scorer let a median of 267 chunks outscore the
    right one, and unweighted whole words tied nine at the top."""
    storage = tmp_path / "store"
    (storage / "projects" / "h1").mkdir(parents=True)
    (storage / "manifest.json").write_text(json.dumps({"projects": {search._normalize_path("/p"): {"hash": "h1"}}}))
    chunk = {"session_id": "s", "jsonl_file": "s.jsonl", "msg_offset": 1, "timestamp": "2026-09-01T00:00:00Z", "msg_type": "user", "related_files": []}
    (storage / "projects" / "h1" / "session_embeddings_index.json").write_text(json.dumps({
        "version": 2, "model": "none", "shards": {"2026-09": {"chunks": [
            dict(chunk, preview="the restore of the alias from the ring"),
            dict(chunk, preview="moved the alias store to sqlite"),
        ]}}, "session_progress": {}}))
    hits = search.search_sessions("/p", "the alias store", method="keyword", windvane_storage_dir=str(storage))
    assert [h.chunk_text for h in hits] == ["moved the alias store to sqlite", "the restore of the alias from the ring"]
    assert hits[0].score == 1.0  # "the" is not a query word
    assert 0.2 < hits[1].score < 0.5  # "alias" is in both chunks, so it is worth less than "store"
    # An identifier-like query word is found inside a prefixed name.
    ident = search.search_sessions("/p", "compact_now", method="keyword", windvane_storage_dir=str(storage))
    assert ident == []
    (storage / "projects" / "h1" / "session_embeddings_index.json").write_text(json.dumps({
        "version": 2, "model": "none", "shards": {"2026-09": {"chunks": [
            dict(chunk, preview="the tool mcp__windvane__compact_now answered"),
            dict(chunk, preview="compact the summary now"),
        ]}}, "session_progress": {}}))
    ident = search.search_sessions("/p", "compact_now", method="keyword", windvane_storage_dir=str(storage))
    assert [h.chunk_text for h in ident] == ["the tool mcp__windvane__compact_now answered"]
    # A query word with an apostrophe or a hyphen is split the way the previews are.
    (storage / "projects" / "h1" / "session_embeddings_index.json").write_text(json.dumps({
        "version": 2, "model": "none", "shards": {"2026-09": {"chunks": [
            dict(chunk, preview="don't record new memories while sleeping"),
            dict(chunk, preview="a made-up number in the report"),
        ]}}, "session_progress": {}}))
    assert search.search_sessions("/p", "don't record", method="keyword", windvane_storage_dir=str(storage))[0].chunk_text.startswith("don't")
    assert search.search_sessions("/p", "made-up", method="keyword", windvane_storage_dir=str(storage))[0].chunk_text.startswith("a made-up")


def test_a_loaded_index_is_kept_while_its_file_is_unchanged(tmp_path: Path, monkeypatch):
    """The daemon serves many searches: the index is parsed and tokenized
    once per file version, and a rewritten file is read again."""
    storage = tmp_path / "store"
    (storage / "projects" / "h1").mkdir(parents=True)
    (storage / "manifest.json").write_text(json.dumps({"projects": {search._normalize_path("/p"): {"hash": "h1"}}}))
    idx = storage / "projects" / "h1" / "session_embeddings_index.json"
    chunk = {"session_id": "s", "jsonl_file": "s.jsonl", "msg_offset": 1, "timestamp": "2026-09-01T00:00:00Z", "msg_type": "user", "related_files": []}
    idx.write_text(json.dumps({"version": 2, "model": "none", "shards": {"2026-09": {"chunks": [dict(chunk, preview="the alias store moved to sqlite")]}}, "session_progress": {}}))
    loads = []
    real = json.loads
    # The manifest is read on every call; only parses of the index itself count.
    monkeypatch.setattr(search.json, "loads", lambda s, *a, **k: (loads.append(1) if '"shards"' in s else None, real(s, *a, **k))[1])
    kw = dict(method="keyword", windvane_storage_dir=str(storage))
    assert search.search_sessions("/p", "alias store", **kw)[0].chunk_text.endswith("sqlite")
    assert search.search_sessions("/p", "alias", **kw)[0].chunk_text.endswith("sqlite")
    assert len(loads) == 1  # the second search read nothing
    # A month outside since/until is skipped whole; a chunk on the boundary month is filtered by its day.
    assert search.search_sessions("/p", "alias", since="2026-10-01", **kw) == []
    assert search.search_sessions("/p", "alias", since="2026-09-02", **kw) == []
    assert search.search_sessions("/p", "alias", since="2026-09-01", until="2026-09-30", **kw)
    idx.write_text(json.dumps({"version": 2, "model": "none", "shards": {"2026-09": {"chunks": [dict(chunk, preview="the alias store moved to postgres now")]}}, "session_progress": {}}))
    assert search.search_sessions("/p", "alias", **kw)[0].chunk_text.endswith("now")
    assert len(loads) == 2  # the rewritten file was read again


def test_an_inherited_index_ranks_hits_that_name_the_sub_project_first(tmp_path: Path):
    """A sub-project without sessions of its own searches the hub's index,
    which pools every spoke; a hit naming the spoke ranks above the rest."""
    storage = tmp_path / "store"
    (storage / "projects" / "h1").mkdir(parents=True)
    (storage / "manifest.json").write_text(json.dumps({"projects": {search._normalize_path("/w"): {"hash": "h1"}}}))
    chunk = {"session_id": "s", "jsonl_file": "s.jsonl", "msg_offset": 1, "timestamp": "2026-09-01T00:00:00Z", "msg_type": "user", "related_files": []}
    (storage / "projects" / "h1" / "session_embeddings_index.json").write_text(json.dumps({
        "version": 2, "model": "none", "shards": {"2026-09": {"chunks": [
            dict(chunk, preview="switch the alias store to sqlite in the hub"),
            dict(chunk, preview="switch the alias store to sqlite in windvane"),
        ]}}, "session_progress": {}}))
    kw = dict(method="keyword", windvane_storage_dir=str(storage))
    hub = search.search_sessions("/w", "alias store sqlite", **kw)
    assert [h.chunk_text.rsplit(" ", 1)[-1] for h in hub] == ["hub", "windvane"]  # equal scores, index order
    spoke = search.search_sessions("/w/windvane", "alias store sqlite", **kw)
    assert [h.chunk_text.rsplit(" ", 1)[-1] for h in spoke] == ["windvane", "hub"]
    assert spoke[0].score > spoke[1].score


def _git_repo_with_a_reason(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}

    def git(*a):
        subprocess.run(["git", *a], cwd=str(repo), check=True, capture_output=True, env=env, stdin=subprocess.DEVNULL)

    git("init", "-q")
    (repo / "src" / "sync.py").write_text("PACE = 0.5\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "sync: first cut")
    (repo / "src" / "sync.py").write_text(
        "# Fair-access policy: a declared UA, max 10 req/s. We stay well under.\n\nPACE = 0.15  # seconds between requests\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "sync: pace 0.15", "-m", "The rate limit chosen against the published policy.")
    return repo


def test_decisions_read_the_repos_history_and_never_crash_on_a_missing_index(tmp_path: Path):
    repo = _git_repo_with_a_reason(tmp_path)
    store = tmp_path / "store"
    (store / "projects" / "sub").mkdir(parents=True)
    # Registered with a memory store only (no embeddings index).
    (store / "manifest.json").write_text(json.dumps({"projects": {search._normalize_path(str(repo)): {"hash": "sub"}}}), encoding="utf-8")
    res = search.find_decision(str(repo), "PACE 0.15 seconds between requests", windvane_storage_dir=str(store))
    texts = [r.chunk_text for r in res]
    assert any("we stay well under" in t.lower() and "diff:" in t for t in texts), texts
    assert all(r.msg_type == "git" and r.session_id.startswith("git:") for r in res)
    hist = search.git_file_history(str(repo), str(repo / "src" / "sync.py"))
    assert len(hist) == 2 and hist[0].chunk_text.startswith("commit ") and hist[0].related_files == ["src/sync.py"]
    (store / "projects" / "root").mkdir()
    (store / "projects" / "root" / "session_embeddings_index.json").write_text('{"chunks": []}', encoding="utf-8")
    manifest = {"projects": {search._normalize_path(str(tmp_path)): {"hash": "root"}, search._normalize_path(str(repo)): {"hash": "sub"}}}
    got = search._resolve_project_with_inheritance(str(repo), manifest, store, require_file="session_embeddings_index.json")
    assert got is not None and got[0] == search._normalize_path(str(tmp_path)) and got[1].name == "root"


# ── patterns ─────────────────────────────────────────────────────────────


@needs_paths
def test_recurring_errors_are_the_same_concrete_error_with_the_latest_example(tmp_path: Path):
    store = tmp_path / "store"
    ext = store / "projects" / "h1" / "extractions"
    ext.mkdir(parents=True)
    proj = patterns._normalize_path(str(tmp_path / "p"))
    (store / "manifest.json").write_text(json.dumps({"projects": {proj: {"hash": "h1"}}}), encoding="utf-8")

    def ext_file(sid, desc, fix=""):
        (ext / f"{sid}.json").write_text(json.dumps({"session_id": sid, "mistakes": [{"error_type": "FileNotFoundError", "description": desc, "how_to_avoid": fix}]}), encoding="utf-8")

    now = datetime.now(timezone.utc)
    stamp = lambda days: (now.replace(microsecond=0) - __import__("datetime").timedelta(days=days)).isoformat()
    ext_file("s1", "FileNotFoundError: [Errno 2] No such file or directory: 'C:\\\\w\\\\old\\\\judge\\\\all.json'", "old fix")
    ext_file("s2", "FileNotFoundError: [Errno 2] No such file or directory: 'C:\\\\w\\\\old\\\\judge\\\\all.json'")
    ext_file("s3", "FileNotFoundError: [Errno 2] No such file or directory: '/tmp/repin.txt'", "new fix")
    sessions = {"s1": {"last_timestamp": stamp(12), "files_edited": []}, "s2": {"last_timestamp": stamp(10), "files_edited": []},
                "s3": {"last_timestamp": stamp(1), "files_edited": []}}
    rec = patterns.detect_recurring_errors(sessions, str(tmp_path / "p"), str(store))
    assert len(rec) == 1 and rec[0].session_count == 2 and "all.json" in rec[0].example and rec[0].last_seen == stamp(10)
    assert rec[0].fix == "old fix"
    assert patterns._concrete_error_key("No such file: 'C:\\\\w\\\\a\\\\b\\\\x.json' at line 42") == patterns._concrete_error_key("No such file: '/other/dir/x.json' at line 7")


def test_struggle_detection_stats_its_candidates_instead_of_walking_the_tree(tmp_path: Path, monkeypatch):
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    kept = root / "src" / "kept.py"
    kept.write_text("x = 1\n", encoding="utf-8")
    gone = root / "src" / "gone.py"
    sessions = {s: {"files_edited": [str(kept), str(gone)]} for s in ("s1", "s2", "s3")}
    monkeypatch.setattr(patterns, "_error_sessions_by_file", lambda *_a: {"kept.py": {"s1", "s2"}, "gone.py": {"s1", "s2"}})

    def _no_walk(self, *_a, **_k):
        raise AssertionError("detect_struggles must not walk the project tree")

    monkeypatch.setattr(patterns.Path, "rglob", _no_walk)
    out = patterns.detect_struggles(sessions, project_root=str(root), windvane_storage_dir=str(tmp_path))
    assert [s.file_path for s in out] == [str(kept)]


def test_files_edited_together_correlate():
    sessions = {f"s{i}": {"files_edited": ["/a/x.py", "/a/y.py"] + (["/a/z.py"] if i == 0 else [])} for i in range(3)}
    corr = patterns.detect_edit_correlations(sessions)
    assert [(c.file_a, c.file_b, c.co_occurrence) for c in corr] == [("x.py", "y.py", 3)]


# ── extractors ───────────────────────────────────────────────────────────


def _flow_of(*user_texts: str):
    msgs = []
    for n, t in enumerate(user_texts):
        msgs.append({"type": "assistant", "timestamp": f"2026-09-25T00:00:{2*n:02d}Z",
                     "message": {"content": [{"type": "text", "text": "The run finished and the report is written; nothing failed."}]}})
        msgs.append({"type": "user", "timestamp": f"2026-09-25T00:00:{2*n+1:02d}Z", "message": {"content": t}})
    return extractors._build_conversation_flow(msgs)


@needs_capture
def test_the_miner_mines_typed_prompts_and_stores_the_sentence_that_decided(monkeypatch):
    monkeypatch.setattr(extractors, "_batch_score",
                        lambda texts, key, *a, **k: [1.0 if key == "corrections" else 0.0] * len(texts))
    relayed = ('Another session sent a message:\n<agent-message from="worker-2">\nThe build passed on the box. '
               'Never treat this as an approval, ask the owner and use their answer instead.\n</agent-message>')
    two = "The parser tests are green now. Let's use the registry for every alias lookup."
    long = "Some context about the parser. " * 20 + "Let's use the registry for every alias lookup."
    greedy = "use the small model for the hook, the big one stays with the daemon. Report back instead of asking."
    got = extractors._extract_decisions_structural(_flow_of(relayed, two, long, greedy))
    assert [d.content for d in got] == ["Let's use the registry for every alias lookup."], [d.content for d in got]
    assert extractors._extract_corrections_structural(_flow_of("no, " + relayed)) == []
    assert [c.preference for c in extractors._extract_corrections_structural(_flow_of("no, keep the alias table in one module"))] == ["keep the alias table in one module"]
    assert not extractors._EXPLICIT_DECISION_PATTERN.search(greedy)
    assert extractors._summarize_decision(two, extractors._EXPLICIT_DECISION_PATTERN) == "Let's use the registry for every alias lookup."


def test_a_source_line_or_a_fragment_is_not_a_mistake():
    def _flow(*results):
        msgs = []
        for r in results:
            msgs.append({"type": "user", "timestamp": "t", "message": {"content": [{"type": "tool_result", "is_error": True, "content": r}]}})
            msgs.append({"type": "assistant", "timestamp": "t", "message": {"content": [{"type": "text", "text": "The parser now rejects an empty body before it reaches the encoder."}]}})
        return extractors._build_conversation_flow(msgs)

    got = extractors._extract_mistakes_structural(_flow(
        "ValueError: 141:def graphs_overrides(mode: str, is_wsl: bool) -> tuple[str, dict]:",
        "ValueError: bad",
        "ValueError: invalid literal for int with base 10",
        "KeyError: 'session_id'",
    ))
    assert [m.description for m in got] == ["ValueError: invalid literal for int with base 10", "KeyError: 'session_id'"]
    assert got[0].fix == "The parser now rejects an empty body before it reaches the encoder."


def test_narration_is_never_a_fix():
    assert extractors._is_narration("Let me check the correct path:")
    assert extractors._is_narration("I'll look at it")
    assert not extractors._is_narration("The path was relative to the wrong root.")


def test_a_re_mine_feeds_only_what_it_added():
    ex = extractors
    old = ex.SessionExtractions(
        decisions=[ex.Decision(content="use the registry", timestamp="t1", confidence=0.9)],
        mistakes=[ex.Mistake(description="KeyError: x", timestamp="t1", error_type="KeyError")],
    )
    now = ex.SessionExtractions(
        decisions=[ex.Decision(content="use the registry", timestamp="t1", confidence=0.9),
                   ex.Decision(content="drop the cache layer", timestamp="t2", confidence=0.9)],
        mistakes=[ex.Mistake(description="KeyError: x", timestamp="t1", error_type="KeyError"),
                  ex.Mistake(description="KeyError: x", timestamp="t3", error_type="KeyError")],
        corrections=[ex.Correction(user_said="no", preference="never cache aliases", timestamp="t2")],
        session_files=["a.py"],
    )
    fresh = ex._fresh_extractions(now, asdict(old))
    assert [d.content for d in fresh.decisions] == ["drop the cache layer"]
    assert [m.timestamp for m in fresh.mistakes] == ["t3"]
    assert [c.preference for c in fresh.corrections] == ["never cache aliases"]
    assert fresh.session_files == ["a.py"]
    assert ex._fresh_extractions(now, None) is now


def test_the_miner_mines_the_live_branch_only(tmp_path: Path):
    """A rewind leaves the abandoned turn in the file; the next prompt hangs
    off an earlier record. Only the live branch is mined."""
    def rec(uid, parent, kind, content):
        return {"uuid": uid, "parentUuid": parent, "type": kind, "isSidechain": False,
                "timestamp": "2026-09-26T00:00:00Z", "message": {"role": kind, "content": content}}

    recs = [
        rec("r1", None, "user", "let's use sqlite for the alias store"),
        rec("r2", "r1", "assistant", [{"type": "text", "text": "Done; the store is sqlite now."}]),
        {"uuid": "s1", "parentUuid": "r2", "type": "system", "subtype": "turn_duration", "isSidechain": False},
        rec("r3", "s1", "user", "let's use the registry for every alias lookup"),
        rec("r4", "r3", "assistant", [{"type": "text", "text": "Rewound later."}]),
        rec("r5", "s1", "user", "never resolve aliases outside the registry"),
        rec("r6", "r5", "assistant", [{"type": "text", "text": "Noted."}]),
    ]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    msgs = extractors._live_messages(p)
    typed = [m["message"]["content"] for m in msgs if m["type"] == "user" and isinstance(m["message"]["content"], str)]
    assert typed == ["let's use sqlite for the alias store", "never resolve aliases outside the registry"]


def test_a_record_is_slimmed_to_what_the_extractors_read():
    """A session log keeps every tool result in full (one transcript was
    513 MB) and the miner read a grown session whole, as dicts, several
    gigabytes at a time (2026-10-09). Slimmed, a record is bounded: the
    chain fields, cut text, a tool call's path or command, a result's error
    flag and edges."""
    from windvane.mining.jsonl_reader import SLIM_RESULT_EDGE_CHARS, SLIM_TEXT_CHARS, slim_message

    big = "x" * 400_000
    user = {
        "uuid": "u1", "parentUuid": "a0", "type": "user", "isSidechain": False, "timestamp": "t1",
        "sessionId": "s", "gitBranch": "main", "cwd": "E:/p", "requestId": "req", "userType": "external",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu1", "is_error": True,
             "content": "Traceback (most recent call last):\n" + big + "\nKeyError: 'x'"},
            {"type": "tool_result", "tool_use_id": "tu2", "content": [{"type": "text", "text": "ok " + big}, {"type": "image", "source": {"data": big}}]},
        ]},
        "toolUseResult": {"stdout": big, "stderr": "boom\n" + big + "\nValueError: y", "interrupted": False},
    }
    s = slim_message(user)
    assert set(s) == {"uuid", "parentUuid", "type", "isSidechain", "timestamp", "sessionId", "gitBranch", "cwd", "message", "toolUseResult"}
    blocks = s["message"]["content"]
    assert blocks[0]["is_error"] is True and blocks[0]["tool_use_id"] == "tu1"
    assert blocks[0]["content"].startswith("Traceback") and blocks[0]["content"].endswith("KeyError: 'x'")
    assert len(blocks[0]["content"]) <= 2 * SLIM_RESULT_EDGE_CHARS + 10
    assert "is_error" not in blocks[1] and blocks[1]["content"].startswith("ok ") and len(blocks[1]["content"]) <= 2 * SLIM_RESULT_EDGE_CHARS + 10
    assert set(s["toolUseResult"]) == {"stderr"} and s["toolUseResult"]["stderr"].endswith("ValueError: y")
    assert len(json.dumps(s)) < 20_000

    assistant = {
        "uuid": "a1", "parentUuid": "u1", "type": "assistant", "timestamp": "t2",
        "message": {"role": "assistant", "model": "m", "usage": {"input_tokens": 9}, "content": [
            {"type": "thinking", "thinking": "hmm " + big, "signature": big},
            {"type": "text", "text": "Let's use sqlite. " + big},
            {"type": "tool_use", "id": "tu3", "name": "Edit", "input": {"file_path": "/p/a.py", "old_string": big, "new_string": big}},
            {"type": "tool_use", "id": "tu4", "name": "Bash", "input": {"command": "pytest -q " + big, "description": "run tests"}},
        ]},
    }
    a = slim_message(assistant)
    c = a["message"]["content"]
    assert c[0] == {"type": "thinking", "thinking": ("hmm " + big)[:SLIM_TEXT_CHARS]}
    assert c[1]["text"].startswith("Let's use sqlite.") and len(c[1]["text"]) == SLIM_TEXT_CHARS
    assert c[2] == {"type": "tool_use", "id": "tu3", "name": "Edit", "input": {"file_path": "/p/a.py"}}
    assert c[3]["name"] == "Bash" and c[3]["input"]["command"].startswith("pytest -q") and c[3]["input"]["description"] == "run tests"
    assert "usage" not in a["message"] and len(json.dumps(a)) < 20_000
    # A plain prompt keeps its text, cut at the bound; a chain-only record keeps its chain.
    assert slim_message({"type": "user", "message": {"content": "short"}})["message"]["content"] == "short"
    assert slim_message({"uuid": "s1", "parentUuid": "a1", "type": "system", "subtype": "turn_duration", "durationMs": 5}) == \
        {"uuid": "s1", "parentUuid": "a1", "type": "system", "subtype": "turn_duration"}
    # A compaction boundary keeps the links the live-branch walk resumes at
    # (the first slimming dropped them and the walk stopped at the last
    # compaction: 447 live messages of 23,325).
    boundary = {"uuid": "c1", "parentUuid": None, "logicalParentUuid": "a1", "type": "system", "subtype": "compact_boundary",
                "compactMetadata": {"trigger": "manual", "preTokens": 419_189, "preservedMessages": {"uuids": ["u0", "a0"], "count": 2}}}
    assert slim_message(boundary) == {"uuid": "c1", "parentUuid": None, "logicalParentUuid": "a1", "type": "system",
                                      "subtype": "compact_boundary", "compactMetadata": {"preservedMessages": {"uuids": ["u0", "a0"]}}}


def test_the_miner_scores_through_the_bulk_client_which_waits_for_the_model(monkeypatch):
    """The single-text client is the hook path: since 1.0.12 it answers
    nothing while the daemon's model loads. The miner's template embeddings
    went through it one by one, so a run during a daemon reload lost every
    semantic score and extracted no corrections (2026-10-09). The miner
    uses the bulk client, which waits."""
    from windvane import daemon

    extractors._template_cache.clear()
    calls: list = []
    monkeypatch.setattr(daemon, "embed_via_server", lambda t: calls.append(("single", t)) or None)  # loading: nothing
    monkeypatch.setattr(daemon, "embed_batch_via_server", lambda texts: calls.append(("batch", len(texts))) or [_fake_vec(t) for t in texts])
    embs = extractors._get_template_embeddings(["use X instead of Y", "the better approach is"], "t-test")
    assert embs is not None and len(embs) == 2 and calls == [("batch", 2)]
    assert extractors._get_template_embeddings(["use X instead of Y", "the better approach is"], "t-test") is embs  # cached
    # A tier that is off answers empty vectors: no templates, no scores.
    extractors._template_cache.clear()
    monkeypatch.setattr(daemon, "embed_batch_via_server", lambda texts: [[] for _ in texts])
    assert extractors._get_template_embeddings(["use X instead of Y"], "t-off") is None
    extractors._template_cache.clear()


def test_the_live_branch_runs_through_a_compaction_when_slimmed(tmp_path: Path):
    """The walk from the last record crosses a compaction boundary through
    its logical parent; the slimmed records must carry that link."""
    def rec(uid, parent, kind, content):
        return {"uuid": uid, "parentUuid": parent, "type": kind, "isSidechain": False,
                "timestamp": "2026-10-09T00:00:00Z", "message": {"role": kind, "content": content}}

    recs = [
        rec("u1", None, "user", "let's use sqlite for the alias store"),
        rec("a1", "u1", "assistant", [{"type": "text", "text": "Done; the store is sqlite now."}]),
        {"uuid": "c1", "parentUuid": None, "logicalParentUuid": "a1", "type": "system", "subtype": "compact_boundary",
         "isSidechain": False, "compactMetadata": {"trigger": "manual", "preservedMessages": {"uuids": [], "count": 0}}},
        rec("u2", "c1", "user", "This session is being continued from a previous conversation..."),
        rec("u3", "u2", "user", "never resolve aliases outside the registry"),
        rec("a3", "u3", "assistant", [{"type": "text", "text": "Noted."}]),
    ]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    msgs = extractors._live_messages(p)
    assert [m["uuid"] for m in msgs] == ["u1", "a1", "u2", "u3", "a3"]


def test_slimmed_records_extract_the_same(tmp_path: Path):
    """What the extractors read survives the slimming: the same decisions,
    mistakes, corrections, approaches and files come out of the live-branch
    read as out of the full records."""
    from windvane.mining.jsonl_reader import iter_messages as _iter

    sid = _sid()
    p = _session(tmp_path, sid, n=8)
    with open(p, "a", encoding="utf-8") as f:
        f.write(_user("No, don't do that. Always use the registry for alias lookups, never a cache.", sid, 20) + "\n")
        f.write(_assistant("Understood: the registry it is.", sid, 21, tool_use={"name": "Bash", "input": {"command": "pytest -q tests/test_alias.py"}}) + "\n")
        f.write(_result("FAILED tests/test_alias.py::test_a - KeyError: 'alias'\n1 failed, 3 passed", sid, is_error=True, offset_min=22) + "\n")
        f.write(_assistant("Fixed the KeyError by seeding the alias.", sid, 23, tool_use={"name": "Edit", "input": {"file_path": "/project/src/alias.py", "old_string": "a", "new_string": "b"}}) + "\n")
    full = [m for _, m in _iter(p, types={"user", "assistant"})]
    slim = extractors._live_messages(p)
    assert len(slim) == len(full)
    a, b = asdict(extractors.extract_all(full)), asdict(extractors.extract_all(slim))
    for k in ("decisions", "mistakes", "approaches", "corrections", "session_files"):
        assert a[k] == b[k], k
    assert b["session_files"] == ["/project/src/auth.py", "/project/src/alias.py"]


def test_a_live_tick_re_extracts_a_session_only_once_it_has_grown_enough(tmp_path: Path, monkeypatch):
    """Every live tick re-read a 513 MB transcript whole for a turn or two of
    growth (2026-10-09). The pipeline's min_growth asks for more; the
    session-end run keeps taking any growth."""
    from windvane.mining import jsonl_reader as jr

    project = tmp_path / "proj"
    project.mkdir()
    claude_projects = tmp_path / "claude_projects"
    jdir = claude_projects / path_to_dir_name(str(project))
    jdir.mkdir(parents=True)
    monkeypatch.setattr(jr, "_get_claude_projects_dir", lambda: claude_projects)
    store = tmp_path / "store"
    (store / "projects" / "p1").mkdir(parents=True)
    norm = str(project.resolve()).replace("\\", "/")
    if len(norm) >= 2 and norm[1] == ":":
        norm = norm[0].lower() + norm[1:]
    (store / "manifest.json").write_text(json.dumps({"projects": {norm: {"hash": "p1", "name": "proj"}}}), encoding="utf-8")

    sid = _sid()
    f = _session(jdir, sid, n=6)
    idx = SessionIndex(tmp_path / "index" / "session_index.json")
    idx.update_session(build_index_for_session(f))
    extraction_file = store / "projects" / "p1" / "extractions" / f"{sid}.json"

    extractors.run_extraction_pipeline(str(project), idx, str(store), min_growth=10)
    first = json.loads(extraction_file.read_text(encoding="utf-8"))
    # The fixture writes n+1 prompts, n+1 replies and n+1 tool results (user
    # records too): 3(n+1) main messages.
    assert first["main_message_count"] == 21

    # Two more turns (6 main messages): under the live tick's bar, so the
    # file is left as it is; the session-end run (min_growth 1) takes them.
    f = _session(jdir, sid, n=8)
    idx.update_session(build_index_for_session(f))
    extractors.run_extraction_pipeline(str(project), idx, str(store), min_growth=10)
    assert json.loads(extraction_file.read_text(encoding="utf-8"))["main_message_count"] == 21
    extractors.run_extraction_pipeline(str(project), idx, str(store), min_growth=1)
    assert json.loads(extraction_file.read_text(encoding="utf-8"))["main_message_count"] == 27
    # Grown by the bar or more, the live tick re-extracts too.
    f = _session(jdir, sid, n=13)
    idx.update_session(build_index_for_session(f))
    extractors.run_extraction_pipeline(str(project), idx, str(store), min_growth=10)
    assert json.loads(extraction_file.read_text(encoding="utf-8"))["main_message_count"] == 42


def _workspace(tmp_path: Path):
    ws = tmp_path / "ws"
    a, b = ws / "proj-a", ws / "proj-b"
    for p in (ws, a, b):
        (p / ".git").mkdir(parents=True, exist_ok=True)
    (a / "src").mkdir(exist_ok=True)
    (b / "src").mkdir(exist_ok=True)
    return ws, a, b


def _registered(tmp_path: Path, monkeypatch, with_b=True):
    from windvane.paths import _normalize_path

    monkeypatch.setenv("WINDVANE_NON_PROJECT_DIRS", ".scratch")
    store = tmp_path / "store"
    ws, a, b = _workspace(tmp_path)
    (store / "projects").mkdir(parents=True)
    rows = ((ws, "hws"), (a, "haa")) + (((b, "hbb"),) if with_b else ())
    manifest = {"version": 3, "projects": {_normalize_path(str(p)): {"hash": h, "name": p.name} for p, h in rows}}
    (store / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return store, ws, a, b


def _entries(store: Path, h: str) -> list:
    f = store / "projects" / h / "memory.json"
    return [e["content"] for e in json.loads(f.read_text(encoding="utf-8")).get("entries", [])] if f.exists() else []


@needs_paths
@needs_capture
@pytest.mark.skipif(not _has("windvane.store", "MemoryStore"), reason="windvane.store not importable yet (port-store's module, which needs port-hooks' modules)")
def test_the_miner_files_a_no_file_entry_where_the_sessions_edits_point(tmp_path: Path, monkeypatch):
    store, ws, a, b = _registered(tmp_path, monkeypatch)
    from windvane import capture

    monkeypatch.setattr(capture, "capture_decision", lambda text, server_only=False: text)
    ex = extractors.SessionExtractions(
        decisions=[extractors.Decision(content="use the registry for every alias lookup", confidence=0.9)],
        corrections=[extractors.Correction(user_said="x", preference="never resolve aliases outside the registry")],
        session_files=[str(a / "src" / "x.py"), str(a / "src" / "y.py"), str(b / "src" / "z.py")],
    )
    extractors._feed_to_memory_store(str(ws), ex, str(store))
    assert not _entries(store, "hws")
    assert any("registry for every alias" in c for c in _entries(store, "haa"))
    assert any("USER PREFERENCE: never resolve aliases" in c for c in _entries(store, "haa"))


@needs_paths
@pytest.mark.skipif(not _has("windvane.store", "MemoryStore"), reason="windvane.store not importable yet (port-store's module, which needs port-hooks' modules)")
def test_an_entry_whose_files_cast_no_vote_follows_the_sessions_edits(tmp_path: Path, monkeypatch):
    store, ws, a, b = _registered(tmp_path, monkeypatch)
    ex = extractors.SessionExtractions(
        mistakes=[extractors.Mistake(description="KeyError: 'plant'", error_type="KeyError", related_files=["tools/restage.py"])],
        session_files=[str(a / "src" / "x.py")],
    )
    extractors._feed_to_memory_store(str(ws), ex, str(store))
    assert any("KeyError: 'plant'" in c for c in _entries(store, "haa"))
    assert not (store / "projects" / "hws" / "memory.json").exists()


@needs_paths
@needs_capture
@pytest.mark.skipif(not _has("windvane.store", "MemoryStore"), reason="windvane.store not importable yet (port-store's module, which needs port-hooks' modules)")
def test_one_sentence_is_stored_once_across_the_hook_and_the_miner(tmp_path: Path, monkeypatch):
    from windvane import capture
    from windvane.store import MemoryStore

    store_dir, ws, a, _b = _registered(tmp_path, monkeypatch, with_b=False)
    store = MemoryStore(storage_dir=str(store_dir))
    store.remember_discovery(str(a), "DECISION: (from user) use the registry for every alias lookup", category="decision", source="auto-prompt", auto_embed=False)
    store.remember_discovery(str(ws), "DECISION: (from user) drop the cache layer for the alias path", category="decision", source="auto-prompt", auto_embed=False)
    monkeypatch.setattr(capture, "capture_decision", lambda text, server_only=False: text)
    ex = extractors.SessionExtractions(
        decisions=[extractors.Decision(content="use the registry for every alias lookup", confidence=0.9),
                   extractors.Decision(content="drop the cache layer for the alias path", confidence=0.9),
                   extractors.Decision(content="never resolve aliases outside the registry", confidence=0.9)],
        corrections=[extractors.Correction(user_said="x", preference="use the registry for every alias lookup"),
                     extractors.Correction(user_said="x", preference="never resolve aliases outside the registry"),
                     extractors.Correction(user_said="x", preference="keep the alias table in one module")],
        session_files=[str(a / "src" / "x.py"), str(a / "src" / "y.py")],
    )
    extractors._feed_to_memory_store(str(ws), ex, str(store_dir))
    assert _entries(store_dir, "hws") == ["DECISION: (from user) drop the cache layer for the alias path"]
    assert _entries(store_dir, "haa") == ["DECISION: (from user) use the registry for every alias lookup",
                                          "DECISION: never resolve aliases outside the registry",
                                          "USER PREFERENCE: keep the alias table in one module"]


# ── the background runner ────────────────────────────────────────────────


def test_no_dropped_module_is_referenced():
    dropped = re.compile(r"\b(?:commitments|cross_project|outcomes|predictive|reflect|rotation|claude_engram|scorer_server|"
                         r"handoff_store|decision_gate|transcript_chain|embed_config|embed_worker|tools\.memory|hooks\.paths|hooks\.intent)\b")
    for f in sorted(MINING.glob("*.py")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if "import" in line:
                assert not dropped.search(line), f"{f.name}:{n}: {line.strip()}"


def test_the_phase_list_has_no_outcome_weights():
    src = (MINING / "background.py").read_text(encoding="utf-8")
    assert "write_weights" not in src and "injection_weights" not in src
    for phase in ("indexing", "extracting", "embedding", "patterns", "memory_maintenance", "code_index"):
        assert f'"{phase}"' in src, phase
    for hygiene in ("cleanup_memories", "archive_stale_mistakes", "prune_task_files", "sync_lessons", "embed_all_memories"):
        assert hygiene in src, hygiene


def test_the_phase_meter_reports_the_peak_inside_a_phase_not_its_end():
    pytest.importorskip("psutil")
    from windvane.mining.background import PhaseMeter

    meter = PhaseMeter(interval=0.02)
    meter.start("grow")
    ballast = bytearray(200_000_000)
    time.sleep(0.2)
    del ballast
    time.sleep(0.1)
    meter.start("after")
    peaks = meter.stop()
    assert peaks["grow"] >= 150, peaks
    assert peaks["after"] < peaks["grow"] - 100


def test_the_schema_canary():
    from windvane.mining.background import _schema_canary

    def ns(specs):
        return SimpleNamespace(sessions={f"s{i}": {"line_count": lines, "known_type_count": known, "last_timestamp": ts}
                                         for i, (lines, known, ts) in enumerate(specs)})

    assert _schema_canary(ns([(200, 196, f"2026-06-0{i + 1}T10:00:00Z") for i in range(8)])) == ""
    warn = _schema_canary(ns([(200, 196, f"2026-06-0{i + 1}T10:00:00Z") for i in range(6)] + [(200, 30, f"2026-06-2{i}T10:00:00Z") for i in range(3)]))
    assert "recognition collapsed" in warn and "update windvane" in warn
    assert _schema_canary(ns([(200, 196, f"2026-06-0{i + 1}T10:00:00Z") for i in range(6)] + [(10, 1, f"2026-06-2{i}T10:00:00Z") for i in range(3)])) == ""
    assert _schema_canary(ns([(200, 30, "2026-06-01T10:00:00Z")] * 3)) == ""


@needs_paths
@needs_proc_lock
def test_a_post_session_run_right_after_another_becomes_a_live_tick(tmp_path: Path, monkeypatch):
    from windvane.mining import background as bg

    store = tmp_path / "store"
    store.mkdir()
    spawned = []
    monkeypatch.setattr(bg.subprocess, "Popen", lambda cmd, **kw: spawned.append((cmd, kw)))
    assert bg.start_mining_background("e:/w", mode="post_session", windvane_storage_dir=str(store)) is False
    assert spawned == []  # WINDVANE_NO_DAEMON: no background process from a test run
    monkeypatch.delenv("WINDVANE_NO_DAEMON")
    (store / "mining_status.json").write_text(json.dumps({"status": "completed", "mode": "post_session", "completed": time.time() - 10}), encoding="utf-8")
    assert bg.start_mining_background("e:/w", mode="post_session", windvane_storage_dir=str(store))
    cmd, kw = spawned[-1]
    assert cmd[cmd.index("--mode") + 1] == "live"
    assert cmd[1:3] == ["-m", "windvane.mining.background"] and Path(kw["cwd"]) == ROOT
    (store / "mining_status.json").write_text(json.dumps({"status": "completed", "mode": "post_session", "completed": time.time() - 7200}), encoding="utf-8")
    bg.start_mining_background("e:/w", mode="post_session", windvane_storage_dir=str(store))
    assert spawned[-1][0][spawned[-1][0].index("--mode") + 1] == "post_session"
    # An empty storage dir means the configured store, never a literal default.
    bg.start_mining_background("e:/w", mode="index_only")
    cmd = spawned[-1][0]
    assert Path(cmd[cmd.index("--storage") + 1]) == store


@needs_paths
@needs_proc_lock
def test_only_one_of_several_miners_started_together_gets_the_lock(tmp_path: Path):
    store = tmp_path / "store"
    store.mkdir()
    child = (
        "import os, time\n"
        "from pathlib import Path\n"
        "from windvane.mining import background as bg\n"
        "go = Path(os.environ['WINDVANE_DIR']) / 'go'\n"
        "while not go.exists():\n"
        "    time.sleep(0.001)\n"
        "print('ACQUIRED' if bg._acquire_lock() else 'blocked')\n"
        "time.sleep(1)\n"
    )
    env = dict(os.environ, WINDVANE_DIR=str(store), PYTHONPATH=str(ROOT))
    procs = [subprocess.Popen([sys.executable, "-c", child], env=env, stdout=subprocess.PIPE, text=True) for _ in range(4)]
    time.sleep(1.5)
    (store / "go").write_text("1")
    outs = [p.communicate(timeout=60)[0].strip() for p in procs]
    assert outs.count("ACQUIRED") == 1, outs


@needs_paths
@needs_proc_lock
def test_a_run_on_an_unregistered_project_completes_empty(tmp_path: Path):
    from windvane.mining import background as bg

    store = tmp_path / "store"
    store.mkdir()
    (store / "manifest.json").write_text(json.dumps({"projects": {}}), encoding="utf-8")
    bg.run_mining(str(tmp_path / "nowhere"), "post_session", str(store))
    status = json.loads((store / "mining_status.json").read_text(encoding="utf-8"))
    assert status["status"] == "completed" and status["result"]["sessions"] == 0
    assert not bg.is_mining_running()  # the lock went with the run

"""``windvane.brief``: the banner's pieces, rendered by the hooks' own
functions for a subagent's prompt and a compaction."""

import json
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    store = tmp_path / "store"
    monkeypatch.setenv("WINDVANE_DIR", str(store))
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    return store


def test_render_joins_the_blocks_with_one_blank_line():
    from windvane.brief import render

    brief = {"rules": ["Rules (1, proj):", "  [r1] Never push"], "files": {"a.py": ["AUTO-CHECK: a"]}, "checkpoint": []}
    assert render(brief) == "Rules (1, proj):\n  [r1] Never push\n\nAUTO-CHECK: a"
    assert render({"rules": [], "files": {}, "checkpoint": []}) == ""


def _hooks():
    return pytest.importorskip("windvane.hooks.common", reason="the banner pieces are the hooks port's")


def _brief_store(tmp_path, monkeypatch, sid):
    from windvane.store import MemoryStore

    common = _hooks()
    store = tmp_path / "store"
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "pyproject.toml").write_bytes(b"[project]\nname = 'proj'\n")
    (store / "projects" / "hp").mkdir(parents=True)
    (store / "sessions").mkdir()
    norm = MemoryStore._normalize_path(str(proj))
    (store / "manifest.json").write_bytes(json.dumps({"projects": {norm: {"hash": "hp"}}}).encode())
    now = time.time()
    entries = [
        {"id": "r1", "category": "rule", "content": "Never push without the word", "relevance": 9, "created_at": now},
        {"id": "m1", "category": "mistake", "content": "loader.py read the whole file into memory", "created_at": now},
        {"id": "m2", "category": "mistake", "content": "parser.py dropped the header row", "created_at": now},
    ]
    (store / "projects" / "hp" / "memory.json").write_bytes(json.dumps({"entries": entries}).encode())
    ring = [{"task_id": "task_9", "kind": "manual", "created": now - 60, "session_id": sid,
             "summary": "wire the brief", "task_description": "Phase 2 brief CLI",
             "current_step": "the compact hook", "completed_steps": ["agents hook"],
             "next_steps": ["the compact hook", "smoke test"], "files_in_progress": ["src/loader.py"],
             "warnings": ["never push"], "context_needed": ["the mod design"], "project_path": str(proj)}]
    (store / "projects" / "hp" / "handoff_history.json").write_bytes(json.dumps({"handoffs": ring}).encode())
    monkeypatch.setattr(common, "_session_id", "", raising=False)
    monkeypatch.chdir(tmp_path)  # build() changes directory; restored at teardown
    return proj


def test_brief_renders_rules_and_file_mistakes_as_the_hooks_do(tmp_path, monkeypatch, capsys):
    from windvane import brief

    sid = "aaaaaaaa-0000-4000-8000-0000000000b1"
    proj = _brief_store(tmp_path, monkeypatch, sid)
    got = brief.build(str(proj), sid, ["src/loader.py", "src/other.py"])
    assert got["rules"] == ["Rules (1, proj):", "  [r1] Never push without the word"]
    assert got["files"] == {"src/loader.py": ["AUTO-CHECK: Past mistakes with src/loader.py:",
                                              "  - loader.py read the whole file into memory"]}
    assert got["checkpoint"] == []
    assert brief.main(["--project", str(proj), "--session", sid, "--files", "src/loader.py"]) == 0
    assert capsys.readouterr().out == (
        "Rules (1, proj):\n  [r1] Never push without the word\n\n"
        "AUTO-CHECK: Past mistakes with src/loader.py:\n  - loader.py read the whole file into memory\n"
    )


def test_brief_checkpoint_is_the_compaction_banners_restore(tmp_path, monkeypatch, capsys):
    from windvane import brief

    common = _hooks()
    sid = "aaaaaaaa-0000-4000-8000-0000000000b2"
    proj = _brief_store(tmp_path, monkeypatch, sid)
    assert brief.main(["--project", str(proj), "--session", sid, "--checkpoint", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    text = "\n".join(got["checkpoint"])
    assert "task_9" in text
    for piece in ("Phase 2 brief CLI", "the compact hook", "agents hook", "smoke test", "src/loader.py", "never push", "wire the brief"):
        assert piece in text, piece
    pdir = common.get_project_dir()
    chosen, skipped = common._banner_checkpoint(pdir, pdir, "compact", False, "")
    assert chosen["task_id"] == "task_9" and got["checkpoint"] == common._format_restored_full(chosen, skipped)
    assert got["rules"] == common._rules_block(pdir)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert brief.main(["--project", str(empty)]) == 0
    assert capsys.readouterr().out == ""

"""Rules: rule entries, detectors, inheritance, and the pack windvane seeds."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    store = tmp_path / "store"
    monkeypatch.setenv("WINDVANE_DIR", str(store))
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    for k in ("WINDVANE_DEFAULT_RULES", "WINDVANE_STRICT_PACK", "WINDVANE_STRUCTURE"):
        monkeypatch.delenv(k, raising=False)
    return store


def _project(tmp_path, name="proj"):
    p = tmp_path / name
    p.mkdir(parents=True, exist_ok=True)
    (p / "pyproject.toml").write_bytes(b"[project]\nname = 'x'\n")
    return p


# ── the pack as decided ─────────────────────────────────────────────────────

DEFAULT_IDS = [
    "p-destructive", "p-outbound", "p-kill", "p-search", "p-verify", "p-prereq", "p-checkpoint", "p-secrets",
    "p-remember", "p-onefunction", "p-names", "p-lying", "p-swallow", "p-determinism", "p-mocks", "p-perf",
]
STRICT_IDS = [
    "p-direct", "p-try", "p-pushback", "p-sequential", "p-quality", "p-plan", "p-decided", "p-gates",
    "p-delegate", "p-numbers", "p-learning",
]


def test_the_pack_has_the_decided_tiers_and_detectors():
    from windvane import rules

    assert [r["id"] for r in rules.DEFAULT_RULES] == DEFAULT_IDS
    assert [r["id"] for r in rules.STRICT_RULES] == STRICT_IDS
    assert rules.PACK_VERSION == 1
    detectors = {r["id"]: r.get("detector") for r in rules.RULES if r.get("detector")}
    assert detectors == {
        "p-destructive": rules.DESTRUCTIVE_DETECTOR,
        "p-outbound": rules.OUTBOUND_DETECTOR,
        "p-kill": rules.KILL_BY_NAME_DETECTOR,
    }
    by_id = {r["id"]: r for r in rules.RULES}
    assert by_id["p-determinism"]["content"] == (
        "Same inputs, same outputs where replay or testing depends on it: no wall-clock reads and no "
        "unseeded randomness on a decision path."
    )
    # the merged rows ride in p-perf / p-outbound / p-verify; dropped rows are absent
    assert "measure before optimizing" in by_id["p-perf"]["content"].lower()
    assert "keep private things private" in by_id["p-outbound"]["content"].lower()
    assert "unmocked end-to-end path" in by_id["p-verify"]["content"]
    text = " ".join(r["content"] + " " + r["reason"] for r in rules.RULES).lower()
    for gone in ("session-logs", ".learnings", "pathlib", "windows and linux", "squash"):
        assert gone not in text, gone
    for r in rules.RULES:
        assert r["reason"] and "your workspace" not in r["reason"].lower()


def test_the_detectors_match_what_they_name():
    from windvane import rules

    def hit(det, cmd):
        return re.search(det["command"], cmd) is not None

    assert hit(rules.DESTRUCTIVE_DETECTOR, "rm -rf build")
    assert hit(rules.DESTRUCTIVE_DETECTOR, "git push --force origin main")
    assert hit(rules.DESTRUCTIVE_DETECTOR, "git reset --hard HEAD~1")
    assert not hit(rules.DESTRUCTIVE_DETECTOR, "git rm --cached secrets.env")
    assert hit(rules.KILL_BY_NAME_DETECTOR, "taskkill /F /IM python.exe")
    assert not hit(rules.KILL_BY_NAME_DETECTOR, "taskkill /PID 4242")
    assert hit(rules.OUTBOUND_DETECTOR, "gh pr create --fill")
    assert hit(rules.OUTBOUND_DETECTOR, "curl -X POST https://example.invalid/hook")
    assert not hit(rules.OUTBOUND_DETECTOR, "git status")
    for det in (rules.DESTRUCTIVE_DETECTOR, rules.KILL_BY_NAME_DETECTOR, rules.OUTBOUND_DETECTOR):
        assert det["unattended"] == "deny" and det["tools"] == ["Bash", "PowerShell"]


# ── seeding ─────────────────────────────────────────────────────────────────


def test_the_default_tier_seeds_once_and_the_strict_tier_on_request(tmp_path):
    from windvane import rules
    from windvane.store import MemoryStore

    proj = str(_project(tmp_path))
    rep = rules.seed_rules(proj)
    assert rep["tiers"] == ["default"] and rep["added"] == DEFAULT_IDS and rep["skipped"] == []
    store = MemoryStore()
    seeded = store.get_rules(proj)
    assert len(seeded) == 16
    with_det = {r.content.split(" (Reason:")[0][:40]: r.detector["note"] for r in seeded if r.detector}
    assert len(with_det) == 3
    assert rules.seed_rules(proj)["already_seeded"] is True

    strict = rules.seed_rules(proj, strict=True)
    assert strict["tiers"] == ["strict"] and strict["added"] == STRICT_IDS
    assert len(MemoryStore().get_rules(proj)) == 27
    assert rules.seed_rules(proj, strict=True)["already_seeded"] is True


def test_a_rule_an_ancestor_already_has_is_skipped_and_adopts_the_detector(tmp_path):
    from windvane import rules
    from windvane.store import MemoryStore

    ws = _project(tmp_path, "ws")
    sub = _project(tmp_path / "ws", "app")
    store = MemoryStore()
    store.add_rule(str(ws), "Don't run destructive commands without asking. trash > rm.")
    store.add_rule(str(sub), "Search first, then read only the relevant files.")
    rep = rules.seed_rules(str(sub))
    assert "p-destructive" in rep["skipped"] and "p-search" in rep["skipped"]
    assert len(rep["added"]) == 14
    ws_rule = MemoryStore().get_rules(str(ws))[0]
    assert ws_rule.detector == rules.DESTRUCTIVE_DETECTOR  # adopted where it lives
    assert ws_rule.id in rep["detectors_attached"]


def test_the_seed_cli_prints_one_json_line(tmp_path, _store):
    proj = _project(tmp_path)
    env = dict(os.environ, WINDVANE_DIR=str(_store), WINDVANE_NO_DAEMON="1", PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    run = subprocess.run(
        [sys.executable, "-m", "windvane.rules", "seed", "--project", str(proj), "--strict"],
        capture_output=True, env=env, timeout=120,
    )
    out = run.stdout.decode().strip().splitlines()
    assert run.returncode == 0, run.stderr.decode(errors="replace")
    assert len(out) == 1
    rep = json.loads(out[0])
    assert rep["tiers"] == ["default", "strict"] and len(rep["added"]) == 27 and rep["pack_version"] == 1


def test_session_start_seeds_per_the_settings(tmp_path, monkeypatch):
    from windvane import rules

    proj = str(_project(tmp_path))
    monkeypatch.setenv("WINDVANE_DEFAULT_RULES", "0")
    assert rules.run_at_session_start(proj) == []
    monkeypatch.setenv("WINDVANE_DEFAULT_RULES", "1")
    lines = rules.run_at_session_start(proj)
    assert lines and lines[0].startswith("Rules seeded: 16 added")
    monkeypatch.setenv("WINDVANE_STRICT_PACK", "1")
    assert rules.run_at_session_start(proj)[0].startswith("Rules seeded: 11 added")
    # a home-like directory with no project marker is never seeded
    bare = tmp_path / "bare"
    bare.mkdir()
    assert rules.run_at_session_start(str(bare)) == []


def test_the_structure_is_created_only_where_missing(tmp_path):
    from windvane import rules

    proj = _project(tmp_path)
    (proj / "CLAUDE.md").write_bytes(b"# mine\n")
    created = rules.ensure_structure(str(proj))
    assert created == [".learnings/ERRORS.md", ".learnings/LEARNINGS.md", "session-logs/"]
    assert (proj / "CLAUDE.md").read_bytes() == b"# mine\n"
    assert rules.ensure_structure(str(proj)) == []
    (tmp_path / "plain").mkdir()
    assert rules.ensure_structure(str(tmp_path / "plain")) == []


# ── rule entries ────────────────────────────────────────────────────────────


def test_list_rules_includes_inherited_workspace_rules(tmp_path):
    from windvane.store import MemoryStore

    store = MemoryStore()
    ws, a = tmp_path / "ws", tmp_path / "ws" / "a"
    a.mkdir(parents=True)
    store.add_rule(str(ws), "Never write a file with platform line endings")
    store.add_rule(str(a), "Edit files with the Edit tool")
    assert [(r.content, src) for r, src in store.get_rules_with_inheritance(str(a))] == [
        ("Edit files with the Edit tool", ""),
        ("Never write a file with platform line endings", store._normalize_path(str(ws))),
    ]
    assert [src for _, src in store.get_rules_with_inheritance(str(ws))] == [""]


def test_a_similar_rule_is_not_duplicated_and_adopts_a_detector(tmp_path):
    from windvane.store import MemoryStore

    store = MemoryStore()
    proj = str(tmp_path / "p")
    ok, msg = store.add_rule(proj, "Never push without the word", reason="pushes are public")
    assert ok and msg.startswith("Rule added with id=")
    det = {"tools": ["Bash"], "command": r"\bgit\s+push\b"}
    ok, msg = store.add_rule(proj, "Never push without the word", reason="pushes are public", detector=det)
    assert not ok and msg.endswith("detector attached")
    rule = store.get_rules(proj)[0]
    assert rule.detector == det and rule.content == "Never push without the word (Reason: pushes are public)"
    store.remember_discovery(proj, "a plain discovery about the cache", auto_embed=False)
    disc = [e for e in store.get_project(proj).entries if e.category == "discovery"][0]
    assert store.set_detector(proj, disc.id, det) == (False, f"Memory {disc.id} is a discovery, not a rule")
    assert store.set_detector(proj, rule.id, None) == (True, f"Detector cleared on rule {rule.id}")


def test_the_detector_helpers_are_reachable_from_rules():
    compliance = pytest.importorskip("windvane.compliance", reason="windvane.compliance is the hooks port's module")
    from windvane import rules

    assert rules.compile_detector is compliance.compile_detector
    assert rules.rules_with_detectors is compliance.rules_with_detectors

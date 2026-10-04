"""windvane.compliance: rules with hand-written detectors matched against
tool calls, and the pack's own detectors (windvane.rules)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from windvane import compliance as c


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path: Path):
    for k in list(os.environ):
        if k.startswith("WINDVANE_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    cfg = tmp_path / "claude-config"
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))


def _pm(entries):
    return {"entries": entries}


# ---------------------------------------------------------------------------
# Detector shape and matching
# ---------------------------------------------------------------------------


def test_detector_shape():
    assert c.normalize_detector({}) is None and c.normalize_detector(None) is None
    assert c.normalize_detector({"note": "x"}) is None
    assert c.normalize_detector({"tools": "Bash", "command": " rm ", "note": "n"}) == {"tools": ["Bash"], "command": "rm", "note": "n"}
    compiled, err = c.compile_detector({"tools": ["Bash"], "command": "rm (-r"})
    assert compiled is None and "regex" in err
    compiled, err = c.compile_detector({"paths": ["design/**"]})
    assert compiled is not None and err == ""
    compiled, err = c.compile_detector({"tools": ["Agent"]})
    assert compiled is not None and err == ""
    assert c.compile_detector({}) == (None, "empty")


def test_matching():
    cmd, _ = c.compile_detector({"tools": ["Bash", "PowerShell"], "command": r"\brm\s+-rf\b"})
    assert c.call_matches(cmd, "Bash", {"command": "rm -rf build"}) is not None
    assert c.call_matches(cmd, "Bash", {"command": "RM -RF build"}) is not None
    assert c.call_matches(cmd, "PowerShell", {"command": "rm -rf build"}) is not None
    assert c.call_matches(cmd, "Edit", {"file_path": "rm -rf"}) is None
    assert c.call_matches(cmd, "Bash", {"command": "ls -la"}) is None
    anyshell, _ = c.compile_detector({"command": r"git push"})
    assert c.call_matches(anyshell, "PowerShell", {"command": "git push"}) is not None
    assert c.call_matches(anyshell, "mcp__x__y", {"command": "git push"}) is None, "a command regex never sees a non-shell tool"
    paths, _ = c.compile_detector({"paths": ["design/**", "*.pem"]})
    assert c.call_matches(paths, "Edit", {"file_path": "design/plan.md"}) is not None
    assert c.call_matches(paths, "Write", {"file_path": "/home/me/proj/design/plan.md"}) is not None
    assert c.call_matches(paths, "Write", {"file_path": r"C:\proj\design\plan.md"}) is not None
    assert c.call_matches(paths, "Edit", {"file_path": "/etc/keys/server.pem"}) is not None
    assert c.call_matches(paths, "Edit", {"file_path": "src/app.py"}) is None
    assert c.call_matches(paths, "Bash", {"command": "cat design/plan.md"}) is None
    tools, _ = c.compile_detector({"tools": ["Agent", "Workflow"]})
    assert c.call_matches(tools, "Workflow", {"script": "x"}) == "tool Workflow"
    assert c.call_matches(tools, "Bash", {"command": "x"}) is None
    inp, _ = c.compile_detector({"tools": ["mcp__mail__send"], "input": r"@"})
    assert c.call_matches(inp, "mcp__mail__send", {"to": "a@b.c"}) is not None


DESTRUCTIVE_YES = [
    "rm -rf build", "rm old.txt", "sudo rm -r /tmp/x", "git reset --hard HEAD~1", "git push --force origin main",
    "git push -f", "git clean -fdx", "git branch -D feature", "Remove-Item -Recurse -Force .\\out",
    "rmdir /s /q build", "psql -c 'DROP TABLE users'", "taskkill /IM python.exe", "pkill -f train",
    "kill -9 1234", "dd if=/dev/zero of=/dev/sda",
]
DESTRUCTIVE_NO = ["ls -la", "cat file", "git status", "git rm --cached secrets.txt", "trash old.txt", "grep -rn rm .",
                  "npm run build", "git push", "python -m pytest", "echo remove", "git diff HEAD", "rm --help"]


def _pack(name):
    rules = pytest.importorskip("windvane.rules", reason="windvane.rules is another agent's module")
    d, err = c.compile_detector(getattr(rules, name))
    assert d is not None and not err, name
    return d


@pytest.mark.parametrize("cmd", DESTRUCTIVE_YES)
def test_the_destructive_detector_catches(cmd):
    assert c.call_matches(_pack("DESTRUCTIVE_DETECTOR"), "Bash", {"command": cmd}) is not None


@pytest.mark.parametrize("cmd", DESTRUCTIVE_NO)
def test_the_destructive_detector_leaves_alone(cmd):
    assert c.call_matches(_pack("DESTRUCTIVE_DETECTOR"), "Bash", {"command": cmd}) is None


def test_the_kill_by_name_and_outbound_detectors():
    k = _pack("KILL_BY_NAME_DETECTOR")
    for s in ["taskkill /IM python.exe /F", "Stop-Process -Name node", "pkill -f train.py", "killall node"]:
        assert c.call_matches(k, "Bash", {"command": s}) is not None, s
    for s in ["taskkill /PID 1234", "kill 1234", "Stop-Process -Id 42", "ps aux | grep python"]:
        assert c.call_matches(k, "Bash", {"command": s}) is None, s
    o = _pack("OUTBOUND_DETECTOR")
    for s in ["git push", "git push origin main", "gh pr create --fill", "gh pr comment 12 --body x",
              "npm publish", "curl -X POST https://api.x/y -d '{}'", "docker push img"]:
        assert c.call_matches(o, "Bash", {"command": s}) is not None, s
    for s in ["git fetch", "git pull", "gh pr view 12", "gh pr list", "curl https://x/y", "git commit -m x", "git log"]:
        assert c.call_matches(o, "Bash", {"command": s}) is None, s


# ---------------------------------------------------------------------------
# Rules in scope, recording, text
# ---------------------------------------------------------------------------


def test_rules_in_scope():
    rules = c.rules_with_detectors(_pm([
        {"id": "r1", "category": "rule", "content": "No rm", "detector": {"tools": ["Bash"], "command": r"\brm\b"}},
        {"id": "r2", "category": "rule", "content": "Be direct"},
        {"id": "r3", "category": "rule", "content": "Broken", "detector": {"command": "("}},
        {"id": "r4", "category": "rule", "content": "Archived", "archived_at": 1.0, "detector": {"command": "x"}},
        {"id": "m1", "category": "mistake", "content": "not a rule", "detector": {"command": "x"}},
        {"id": "r1", "category": "rule", "content": "No rm (inherited twice)", "detector": {"command": "rm"}},
    ]))
    assert [r["id"] for r in rules] == ["r1", "r2", "r3"]
    by = {r["id"]: r for r in rules}
    assert by["r1"]["compiled"] is not None and by["r1"]["error"] == ""
    assert by["r2"]["detector"] is None and by["r2"]["error"] == ""
    assert by["r3"]["compiled"] is None and "regex" in by["r3"]["error"]
    hits = c.match_call(rules, "Bash", {"command": "rm -rf x"})
    assert len(hits) == 1 and hits[0]["rule_id"] == "r1" and "rm" in hits[0]["what"]
    assert c.match_call(rules, "Bash", {"command": "ls"}) == []


def test_recording_verdicts_health_and_the_summary():
    pm = _pm([
        {"id": "r1", "category": "rule", "content": "No rm", "detector": {"tools": ["Bash"], "command": r"\brm\b", "note": "rm"}},
        {"id": "r3", "category": "rule", "content": "Broken", "detector": {"command": "("}},
        {"id": "r2", "category": "rule", "content": "Advisory"},
    ])
    rules = c.rules_with_detectors(pm)
    state: dict = {}
    hits = c.match_call(rules, "Bash", {"command": "rm -rf x"})
    new = c.record(state, rules, hits, "Bash", {"command": "rm -rf x"}, tool_use_id="t1", turn=4, permission_mode="bypassPermissions")
    assert len(new) == 1 and new[0]["turn"] == 4 and new[0]["verdict"] == "unattended"
    again = c.record(state, rules, hits, "Bash", {"command": "rm -rf x"}, tool_use_id="t1", turn=4, permission_mode="bypassPermissions")
    assert again == [] and len(state["compliance"]["matches"]) == 1
    assert c.record(state, rules, hits, "Bash", {"command": "rm -rf x"}, tool_use_id="t2", turn=5, permission_mode="default")[0]["verdict"] == "prompted"
    assert c.record(state, rules, hits, "Bash", {"command": "rm -rf x"}, tool_use_id="t3", turn=6, permission_mode="plan")[0]["verdict"] == "plan"
    new4 = c.record(state, rules, hits, "Bash", {"command": "rm -rf x"}, tool_use_id="t4", turn=6, permission_mode="auto", agent_id="a1")
    assert new4[0]["verdict"] == "unattended" and new4[0]["subagent"]
    h = state["compliance"]["health"]
    assert h["r1"]["hits"] == 4 and h["r1"]["ok"]
    assert h["r3"]["ok"] is False and "regex" in h["r3"]["error"]
    assert "r2" not in h
    s = c.summary(state, pm)
    assert s["with_detector"] == 2 and s["advisory"] == 1 and s["broken"] == 1
    assert s["unattended"] == 2 and s["prompted"] == 1 and len(s["matches"]) == 4
    assert c.summary(state)["rules"] == [] and len(c.summary(state)["matches"]) == 4
    for i in range(c.MATCHES_KEEP + 20):
        c.record(state, rules, hits, "Bash", {"command": "rm x"}, tool_use_id=f"x{i}", turn=7, permission_mode="default")
    assert len(state["compliance"]["matches"]) == c.MATCHES_KEEP


def test_the_injected_rule_text():
    rules = c.rules_with_detectors(_pm([{"id": "r1", "category": "rule", "content": "No rm", "detector": {"tools": ["Bash"], "command": r"\brm\b"}}]))
    hits = c.match_call(rules, "Bash", {"command": "rm -rf x"})
    t = c.rule_text(hits, "bypassPermissions")
    assert t.startswith("<windvane-rule>") and t.endswith("</windvane-rule>")
    assert "[r1] No rm" in t and "command ~" in t
    assert "No permission prompt stands before this call" in t
    t2 = c.rule_text(hits, "default", context="the last prompt reads as approval")
    assert "A permission prompt stands between you and the call" in t2 and "reads as approval" in t2
    assert c.rule_text([], "default") == ""


def test_the_setting_turns_it_off(tmp_path: Path, monkeypatch):
    proj = tmp_path / "proj-opt"
    (proj / ".windvane").mkdir(parents=True)
    assert c.enabled(str(proj)), "on by default"
    (proj / ".windvane" / "config.json").write_text(json.dumps({"compliance": False}), encoding="utf-8")
    assert not c.enabled(str(proj))
    monkeypatch.setenv("WINDVANE_COMPLIANCE", "on")
    assert c.enabled(str(proj)), "the environment wins over the project file"
    monkeypatch.setenv("WINDVANE_COMPLIANCE", "off")
    assert not c.enabled(str(proj))
    monkeypatch.delenv("WINDVANE_COMPLIANCE")
    (tmp_path / "store").mkdir(parents=True, exist_ok=True)
    (tmp_path / "store" / "config.json").write_text(json.dumps({"compliance": False}), encoding="utf-8")
    assert not c.enabled(str(tmp_path / "other")), "the user file applies to every project"


def test_unattended_deny_is_autonomy_mode_only(monkeypatch):
    d = c.normalize_detector({"command": "x", "unattended": "DENY"})
    assert d is not None and d["unattended"] == "deny"
    assert "unattended" not in (c.normalize_detector({"command": "x", "unattended": "maybe"}) or {})
    compiled, _ = c.compile_detector({"command": "x"})
    assert compiled is not None and compiled["unattended"] == "record"
    rules = c.rules_with_detectors(_pm([
        {"id": "r1", "category": "rule", "content": "No rm", "detector": {"tools": ["Bash"], "command": r"\brm\b", "unattended": "deny"}},
        {"id": "r2", "category": "rule", "content": "Log pushes", "detector": {"tools": ["Bash"], "command": r"git push"}},
    ]))
    hits = c.match_call(rules, "Bash", {"command": "rm -rf x && git push"})
    assert {h["rule_id"]: h["unattended"] for h in hits} == {"r1": "deny", "r2": "record"}
    assert c.should_deny(hits, "bypassPermissions") == [], "outside autonomy mode nothing is denied, even in bypass mode"
    assert [h["rule_id"] for h in c.should_deny(hits, "default", {"run": {"auto": {"status": "running"}}})] == ["r1"], "a running goal is autonomy mode"
    monkeypatch.setenv("WINDVANE_AUTONOMY", "1")
    denied = c.should_deny(hits, "bypassPermissions")
    assert [h["rule_id"] for h in denied] == ["r1"]
    t = c.deny_text(denied)
    assert t.startswith("windvane: refused by rule [r1]") and "checkpoint tool, operation save" in t and "PushNotification" in t and "Do not work around it" in t
    assert "(+1 more rule)" in c.deny_text(hits) and c.deny_text([]) == ""


def test_the_packs_ask_first_detectors_deny_unattended():
    rules = pytest.importorskip("windvane.rules", reason="windvane.rules is another agent's module")
    assert all(x["unattended"] == "deny" for x in (rules.DESTRUCTIVE_DETECTOR, rules.KILL_BY_NAME_DETECTOR, rules.OUTBOUND_DETECTOR))


def test_the_run_report_renders_the_compliance_section(tmp_path: Path, monkeypatch):
    storage = pytest.importorskip("windvane.storage", reason="windvane.storage is the parent agent's module")
    from windvane import report as rr

    pm = _pm([{"id": "r1", "category": "rule", "content": "No recursive deletes without asking",
               "detector": {"tools": ["Bash"], "command": r"\brm\s+-rf\b", "note": "rm -rf"}}])
    monkeypatch.setattr(storage, "load_project_memory", lambda _p: pm)
    rules = c.rules_with_detectors(pm)
    state: dict = {"run": {"started_at": time.time() - 60}}
    c.record(state, rules, c.match_call(rules, "Bash", {"command": "rm -rf build"}), "Bash", {"command": "rm -rf build"},
             tool_use_id="tu1", turn=1, permission_mode="bypassPermissions")
    proj = tmp_path / "proj"
    proj.mkdir()
    rep = rr.collect("s-compliance", str(proj), state)
    assert isinstance(rep["compliance"], dict) and rep["compliance"]["with_detector"] == 1
    md = rr.render_md(rep)
    assert "## Rules compliance (1 with a detector, 0 advisory, 0 broken)" in md
    assert "| unattended |" in md and "rm -rf build" in md

"""The cold tier: what ages out, what never does, and that nothing is lost."""

import ast
import json
import time
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parent.parent / "engine" / "windvane"
AUTO_DECISION_RELEVANCE = 7  # the miner and the work log mint decisions at this
MANUAL_REMEMBER_RELEVANCE = 5  # memory(remember) default


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    monkeypatch.delenv("WINDVANE_ARCHIVE_DAYS", raising=False)
    return tmp_path / "store"


def _ms():
    from windvane.store import MemoryStore

    return MemoryStore()


def _archivable(store, *, category, relevance, stale_days):
    from windvane.store import MemoryEntry

    e = MemoryEntry(content=f"{category} r{relevance} d{stale_days}", category=category, relevance=relevance)
    e.last_accessed = time.time() - stale_days * 86400
    return store._is_archivable(e)


# ── the exemption sits above every default ──────────────────────────────────


def test_defaults_age_out_and_promoted_memories_stay_hot():
    s = _ms()
    assert _archivable(s, category="decision", relevance=AUTO_DECISION_RELEVANCE, stale_days=30)
    assert _archivable(s, category="discovery", relevance=MANUAL_REMEMBER_RELEVANCE, stale_days=30)
    assert not _archivable(s, category="decision", relevance=AUTO_DECISION_RELEVANCE, stale_days=1)
    assert not _archivable(s, category="discovery", relevance=8, stale_days=30)
    assert not _archivable(s, category="discovery", relevance=10, stale_days=999)
    for cat in ("rule", "mistake", "lesson"):
        assert not _archivable(s, category=cat, relevance=1, stale_days=999), cat


def test_the_exemption_stays_above_every_mint_site():
    """Read the real mint sites: every age-eligible relevance minted in the
    engine stays below the exemption, so a default never becomes immortal."""
    from windvane.store import ARCHIVE_EXEMPT_RELEVANCE

    assert ARCHIVE_EXEMPT_RELEVANCE > AUTO_DECISION_RELEVANCE > MANUAL_REMEMBER_RELEVANCE
    protected = {"rule", "mistake", "lesson"}
    minted: dict = {}
    sources = [ENGINE / "log.py", ENGINE / "tools.py", ENGINE / "remember.py"]
    extractors = ENGINE / "mining" / "extractors.py"
    if extractors.exists():
        sources.append(extractors)
    for f in sources:
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg and isinstance(k.value, ast.Constant)}
            if "relevance" not in kw:
                continue
            cat = kw["category"].value if "category" in kw else "?"
            if cat not in protected:
                minted.setdefault(cat, set()).add(kw["relevance"].value)
    flat = sorted({r for rs in minted.values() for r in rs})
    assert flat and all(r < ARCHIVE_EXEMPT_RELEVANCE for r in flat), minted


# ── archive, search, restore ────────────────────────────────────────────────


def _age(store, proj, days):
    for e in store.get_project(proj).entries:
        e.last_accessed = time.time() - days * 86400
        e.created_at = time.time() - days * 86400
    store._dirty_projects.add(store._normalize_path(proj))
    store._save()


def test_archive_moves_old_entries_and_restore_brings_one_back(tmp_path):
    s = _ms()
    proj = str(tmp_path / "p")
    s.remember_discovery(proj, "the old cache layout used redis keys", auto_embed=False)
    s.remember_discovery(proj, "MISTAKE: dropped the index", category="mistake", relevance=9, auto_embed=False)
    s.add_rule(proj, "Never push without the word")
    _age(s, proj, 40)
    preview = s.archive_old_memories(proj, dry_run=True)
    assert preview["archived_count"] == 1 and len(s.get_project(proj).entries) == 3
    done = s.archive_old_memories(proj, dry_run=False)
    assert done["archived_count"] == 1
    assert {e.category for e in s.get_project(proj).entries} == {"mistake", "rule"}
    stats = s.get_archive_stats(proj)
    assert stats["hot_total"] == 2 and stats["archive_total"] == 1
    hit = s.search_archive(proj, query="redis cache")
    assert len(hit) == 1 and hit[0].archived_at
    ok, msg = s.restore_from_archive(proj, hit[0].id)
    assert ok and hit[0].id in {e.id for e in _ms().get_project(proj).entries}
    assert s.search_archive(proj) == []


def test_stale_machine_mistakes_archive_but_manual_and_recurring_stay(tmp_path):
    s = _ms()
    proj = str(tmp_path / "p")
    s.remember_discovery(proj, "MISTAKE: TypeError: one-off typo in the loader", category="mistake", source="auto-detected", relevance=7, auto_embed=False)
    s.remember_discovery(proj, "MISTAKE: logged by hand, keep this one", category="mistake", source="work_tracker", relevance=9, auto_embed=False)
    s.remember_discovery(proj, "MISTAKE: KeyError: missing column in parser.py", category="mistake", source="session_mining", related_files=["src/parser.py"], auto_embed=False)
    _age(s, proj, 30)
    rep = s.archive_stale_mistakes(proj, recent_files={"E:/x/src/parser.py"}, dry_run=False)
    assert rep["archived_count"] == 1 and "one-off typo" in rep["entries"][0]["preview"]
    left = {e.content for e in s.get_project(proj).entries}
    assert left == {"MISTAKE: logged by hand, keep this one", "MISTAKE: KeyError: missing column in parser.py"}


# ── cleanup: duplicates, decay, archive before drop ─────────────────────────


def test_cleanup_merges_near_duplicates_and_archives_before_decay(tmp_path):
    s = _ms()
    proj = str(tmp_path / "p")
    s.remember_discovery(proj, "the alias store is sqlite and lives in store.py today", auto_embed=False)
    p = s.get_project(proj)
    from windvane.store import MemoryEntry

    # a near-duplicate that remember would have refused, written directly
    p.entries.append(MemoryEntry(id="dup1", content="The alias store is SQLite and lives in store.py today", category="discovery", relevance=6))
    p.entries.append(MemoryEntry(id="old1", content="an old discovery nobody has read in months, long enough", category="discovery",
                                 relevance=4, last_accessed=time.time() - 60 * 86400))
    p.entries.append(MemoryEntry(id="short", content="too short", category="discovery"))
    s._dirty_projects.add(p.project_path)
    s._save()
    report = s.cleanup_memories(proj, dry_run=True)
    assert [d["entry_id"] for d in report["duplicates_found"]] == ["dup1"]
    assert [a["entry_id"] for a in report["archived"]] == ["old1"]
    assert [b["entry_id"] for b in report["broken_found"]] == ["short"]
    assert len(s.get_project(proj).entries) == 4  # dry run changes nothing
    s.cleanup_memories(proj, dry_run=False)
    hot = s.get_project(proj).entries
    assert [e.id for e in hot if e.id != "dup1"] == [hot[0].id] and len(hot) == 1
    assert hot[0].relevance == 6  # merged: the higher relevance and its text
    s._archive_loaded = False
    assert [e.id for e in s.search_archive(proj)] == ["old1"]  # archived, not deleted


# ── consolidation lists groups and never removes anything ───────────────────


def test_consolidation_lists_groups_and_removes_nothing(tmp_path):
    s = _ms()
    proj = str(tmp_path / "ws" / "proj")
    for i in range(12):
        s.remember_discovery(proj, f"DECISION: use approach {i} because reason {i}", source="test", relevance=7,
                             category="decision", tags=["decision"], auto_embed=False)
    s.remember_discovery(proj, "RULE: always do X", relevance=9, category="rule", tags=["decision"], auto_embed=False)
    s.remember_discovery(proj, "MISTAKE: broke Y", relevance=8, category="mistake", tags=["decision"], auto_embed=False)
    before = {e.id for e in s.get_project(proj).entries}
    for dry in (True, False):
        report = s.consolidate_memories(proj, dry_run=dry)
        groups = {g["tag"]: g["count"] for g in report["groups_found"]}
        assert groups.get("decision") == 12  # the rule and the mistake are never members
        assert report["consolidated"] == [] and "nothing was merged" in report["summary"]
    assert {e.id for e in s.get_project(proj).entries} == before
    assert s.consolidate_memories(proj, tag="nope")["summary"] == "No groups found that need consolidation"


def test_clusters_form_from_shared_tags(tmp_path):
    s = _ms()
    proj = str(tmp_path / "p")
    for i in range(3):
        s.remember_discovery(proj, f"the database migration number {i} is reversible and tested", auto_embed=False)
    s.cleanup_memories(proj, dry_run=False, apply_decay=False)
    listing = s.get_clusters(proj)
    names = {c["name"] for c in listing["clusters"]}
    assert "Database Memories" in names and listing["total_memories"] == 3
    cid = next(c["id"] for c in listing["clusters"] if c["name"] == "Database Memories")
    assert s.get_clusters(proj, cid)["cluster"]["memory_count"] == 3

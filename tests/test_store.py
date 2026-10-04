"""The memory store: the entry model and its disk format, project
registration, the freshness guard, search and the injection scoring."""

import json
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    store = tmp_path / "store"
    monkeypatch.setenv("WINDVANE_DIR", str(store))
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    monkeypatch.delenv("WINDVANE_SEMANTIC", raising=False)
    return store


def _ms(store_dir=None):
    from windvane.store import MemoryStore

    return MemoryStore(str(store_dir)) if store_dir else MemoryStore()


# ── the entry model and the disk format ─────────────────────────────────────

ENTRY_KEYS = {
    "content", "category", "created_at", "source", "relevance", "id", "last_accessed",
    "access_count", "tags", "related_files", "cluster_id", "archived_at", "detector",
}
PROJECT_KEYS = {
    "project_name", "summary", "language", "framework", "key_files", "key_directories", "entries",
    "recent_searches", "last_updated", "file_memory_index", "tag_memory_index", "clusters", "last_cleanup",
}


def test_memory_json_keeps_the_format_older_stores_wrote(tmp_path, _store):
    m = _ms()
    proj = str(tmp_path / "proj")
    added, msg = m.remember_discovery(proj, "The cache is sqlite, see src/cache.py", relevance=6, auto_embed=False)
    assert added and "id=" in msg
    norm = m._normalize_path(proj)
    data = json.loads((m._project_dir(norm) / "memory.json").read_text(encoding="utf-8"))
    assert set(data) == PROJECT_KEYS  # project_path is the manifest key, not stored
    entry = data["entries"][0]
    assert set(entry) == ENTRY_KEYS
    assert entry["related_files"] == ["src/cache.py"] and entry["category"] == "discovery"
    manifest = json.loads((_store / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["projects"][norm]["name"] == "proj"


def test_a_store_written_by_an_older_install_opens_unchanged(tmp_path, _store):
    """An imported store: extra keys are ignored, a missing id is backfilled,
    a missing field takes its default, and nothing is lost."""
    from windvane.store import MemoryStore

    proj = MemoryStore._normalize_path(str(tmp_path / "old"))
    (_store / "projects" / "h0").mkdir(parents=True)
    (_store / "manifest.json").write_text(json.dumps({"version": 3, "projects": {proj: {"hash": "h0", "name": "old"}}}))
    now = time.time()
    entries = [
        {"id": "r1", "category": "rule", "content": "Never push without the word", "relevance": 9,
         "created_at": now, "detector": {"tools": ["Bash"], "command": "git push"}, "some_future_key": 1},
        {"category": "mistake", "content": "MISTAKE: loader.py read the whole file", "created_at": now},
    ]
    (_store / "projects" / "h0" / "memory.json").write_text(
        json.dumps({"project_name": "old", "entries": entries, "clusters": {"c1": {"cluster_id": "c1", "name": "X", "memory_ids": ["r1"]}}})
    )
    m = MemoryStore()
    p = m.get_project(proj)
    assert p is not None and len(p.entries) == 2
    rule, mistake = p.entries
    assert rule.detector == {"tools": ["Bash"], "command": "git push"}
    assert mistake.id and mistake.relevance == 5 and mistake.tags == []
    assert p.clusters["c1"].memory_ids == ["r1"]
    # the backfilled id was written back
    on_disk = json.loads((_store / "projects" / "h0" / "memory.json").read_text(encoding="utf-8"))["entries"]
    assert all(e.get("id") for e in on_disk)


def test_a_single_file_store_of_the_first_versions_migrates(tmp_path, _store):
    from windvane.store import MemoryStore

    _store.mkdir(parents=True)
    path = str(tmp_path / "legacy")
    (_store / "memory.json").write_text(json.dumps({
        "version": 1,
        "projects": {path: {"project_path": path, "project_name": "legacy",
                            "entries": [{"content": "DECISION: use sqlite for the alias store", "category": "decision",
                                         "created_at": 1.0}]}},
    }))
    m = MemoryStore()
    p = m.get_project(path)
    assert p is not None and p.entries[0].id and "decision" in p.entries[0].tags
    assert (_store / "manifest.json").exists() and (_store / "memory.json.v2backup").exists()


# ── writers, search, manage ─────────────────────────────────────────────────


def test_remember_dedupes_tags_and_files(tmp_path):
    m = _ms()
    proj = str(tmp_path / "p")
    ok, _ = m.remember_discovery(proj, "Auth tokens are validated in auth/middleware.py", auto_embed=False)
    assert ok
    dup, msg = m.remember_discovery(proj, "Auth tokens are validated in auth/middleware.py", relevance=8, auto_embed=False)
    assert not dup and "Duplicate" in msg
    e = m.get_project(proj).entries[0]
    assert e.access_count == 2 and e.relevance == 8
    assert "auth" in e.tags and "auth/middleware.py" in e.related_files
    assert m.search_memories(proj, file_path="auth/middleware.py")[0].id == e.id


def test_search_recent_modify_delete_promote_batch(tmp_path):
    m = _ms()
    proj = str(tmp_path / "p")
    for i, text in enumerate(("sqlite is the alias store", "redis is out of scope", "the parser drops BOM")):
        m.remember_discovery(proj, text, relevance=5 + i, auto_embed=False)
        time.sleep(0.01)
    assert [e.content for e in m.get_recent_memories(proj, limit=2)] == ["the parser drops BOM", "redis is out of scope"]
    hits = m.search_memories(proj, query="sqlite alias")
    assert len(hits) == 1 and hits[0].content.startswith("sqlite")
    mid = hits[0].id
    assert m.modify_memory(proj, mid, content="sqlite is the alias store, see store.py") == (True, "Modified: content")
    assert "store.py" in m._get_entry_by_id(m.get_project(proj), mid).related_files
    ok, msg = m.promote_to_rule(proj, mid, reason="decided")
    assert ok and m.get_rules(proj)[0].id == mid and m.get_rules(proj)[0].relevance == 8
    assert m.promote_to_rule(proj, mid) == (False, "Already a rule")
    assert m.batch_delete(proj, category="rule")[0] == 0  # protected
    n, _ = m.batch_delete(proj, category="discovery")
    assert n == 2
    assert m.delete_memory(proj, mid) == (True, f"Deleted memory {mid}")
    assert m.delete_memory(proj, mid)[0] is False
    assert m.get_project(proj).entries == []


def test_hybrid_search_drops_the_zero_score_tail(tmp_path, monkeypatch):
    """A no-match query answered with unrelated entries at 0.000: the
    score-based half contributes candidates for ANY query and an entry with
    no vector reranks to 0.0. A zero is not a result."""
    m = _ms()
    proj = str(tmp_path / "proj")
    for text in ("MISTAKE: rmtree ate the fixture", "DECISION: sqlite over json", "MISTAKE: CRLF on write_text"):
        m.remember_discovery(proj, text, category="mistake", relevance=8, auto_embed=False)
    monkeypatch.setattr(m, "_get_embedding", lambda text: [1.0, 0.0, 0.0])
    assert m.hybrid_search(proj, query="auto-compaction fires below the output reserve") == []
    assert all(score > 0 for _, score in m.hybrid_search(proj, query=""))


def test_vectors_stay_json_without_the_semantic_tier(tmp_path, monkeypatch):
    """No numpy in the core: without the semantic tier vectors are JSON, and
    a pending vector is merged under the configured model's signature."""
    m = _ms()
    assert m._np is None
    proj = str(tmp_path / "p")
    m.remember_discovery(proj, "vectors are optional here", auto_embed=False)
    eid = m.get_project(proj).entries[0].id
    monkeypatch.setattr(m, "_get_embedding", lambda text: [0.6, 0.8])
    m.embed_memory(eid, "vectors are optional here", project_path=proj)
    pdir = m._project_dir(m._normalize_path(proj))
    pending = json.loads((pdir / "embeddings_pending.json").read_text(encoding="utf-8"))
    assert pending["vectors"] == {eid: [0.6, 0.8]}
    from windvane.store import MemoryStore

    fresh = MemoryStore()
    fresh.get_project(proj)  # loading merges the pending file under the same signature
    assert json.loads((pdir / "embeddings.json").read_text(encoding="utf-8")) == {eid: [0.6, 0.8]}
    assert not (pdir / "embeddings_pending.json").exists()
    monkeypatch.setattr(fresh, "_get_embedding", lambda text: [0.6, 0.8])
    assert [(e.id, round(s, 6)) for e, s in fresh.vector_search(proj, "anything")] == [(eid, 1.0)]


# ── freshness: two processes on one store ───────────────────────────────────


def _rule_texts(store, proj):
    return {r.id: r.content.split(" (Reason:")[0] for r in store.get_rules(proj)}


def test_a_loaded_store_sees_and_never_clobbers_another_writer(tmp_path, _store):
    proj = str(tmp_path / "proj-a")
    Path(proj).mkdir()
    a = _ms(_store)
    a.remember_project(proj, summary="a")
    assert a.add_rule(proj, "Rule one from A", reason="bench")[0]
    b = _ms(_store)
    assert "Rule one from A" in _rule_texts(b, proj).values()
    assert b.add_rule(proj, "Rule two from B", reason="bench")[0]
    ids_a = _rule_texts(a, proj)
    assert "Rule two from B" in ids_a.values()  # A, already loaded, sees it
    two = next(i for i, c in ids_a.items() if c == "Rule two from B")
    assert a.delete_memory(proj, two)[0]
    assert "Rule two from B" not in _rule_texts(b, proj).values()

    b.add_rule(proj, "Rule three from B", reason="bench")
    a.add_rule(proj, "Rule four from A", reason="bench")
    final = set(_rule_texts(_ms(_store), proj).values())
    assert {"Rule three from B", "Rule four from A"} <= final and "Rule two from B" not in final

    proj2 = str(tmp_path / "proj-b")
    Path(proj2).mkdir()
    b.remember_project(proj2, summary="b")
    b.add_rule(proj2, "Rule in the new project", reason="bench")
    assert a.get_project(proj2) is not None  # the manifest is re-read
    assert "Rule in the new project" in _rule_texts(a, proj2).values()
    assert "Rule four from A" in _rule_texts(_ms(_store), proj).values()

    c = _ms(_store)
    c.get_project(proj)
    a.add_rule(proj, "Rule five from A", reason="bench")  # disk moves on
    c_proj = c._projects[c._normalize_path(proj)]
    c_proj.summary = "touched by C on a stale base"
    c._dirty_projects.add(c._normalize_path(proj))
    c._save()
    assert "Rule five from A" in _rule_texts(_ms(_store), proj).values()  # merged, not lost
    assert _ms(_store).get_project(proj).summary == "touched by C on a stale base"

    norm = a._normalize_path(proj)
    a.get_project(proj)
    before = a._project_stamps.get(norm)
    a.get_project(proj)
    assert a._project_stamps.get(norm) == before and isinstance(before, tuple) and len(before) == 2


# ── injection scoring ───────────────────────────────────────────────────────

SEEDS = [
    ("Auth uses JWT tokens validated in middleware", "discovery", 8, ["auth", "security"], ["auth/middleware.py"]),
    ("MISTAKE: removed session check from auth, broke all routes", "mistake", 9, ["auth", "mistake"], ["auth/middleware.py"]),
    ("Auth tokens expire after 24 hours", "discovery", 6, ["auth"], ["auth/tokens.py"]),
    ("Database uses PostgreSQL with pgbouncer connection pooling", "discovery", 7, ["database"], ["db/pool.py"]),
    ("MISTAKE: migration failed because column was NOT NULL without default", "mistake", 9, ["database", "mistake"], ["db/migrations.py"]),
    ("Always use parameterized queries", "rule", 9, ["database", "security", "rule"], ["db/queries.py"]),
    ("React components use strict TypeScript", "discovery", 6, ["frontend"], ["src/App.tsx"]),
    ("DECISION: use Tailwind instead of styled-components", "decision", 7, ["frontend", "decision"], ["src/styles/"]),
    ("API rate limiting is 100 req/min per user", "discovery", 5, ["api"], ["api/routes.py"]),
    ("MISTAKE: forgot to validate request body in POST /users", "mistake", 9, ["api", "mistake"], ["api/routes.py"]),
    ("Project uses monorepo with turborepo", "discovery", 4, [], []),
    ("CI runs on GitHub Actions", "context", 3, [], [".github/workflows/"]),
    ("Always run tests before committing", "rule", 9, ["testing", "rule"], []),
]


def _seeded(proj, seeds=SEEDS):
    m = _ms()
    for content, category, relevance, tags, files in seeds:
        m.remember_discovery(proj, content, category=category, relevance=relevance, tags=tags, related_files=files, auto_embed=False)
    return m


@pytest.mark.parametrize(
    "file_path,tags,expected",
    [
        ("auth/middleware.py", ["auth"], ("auth",)),
        ("db/migrations.py", ["database"], ("migration",)),
        ("db/queries.py", ["database"], ("parameterized",)),
        ("api/routes.py", ["api"], ("api", "request body")),
        ("src/App.tsx", ["frontend"], ("react",)),
        ("auth/tokens.py", ["auth"], ("auth",)),
    ],
)
def test_the_right_memory_ranks_first(tmp_path, file_path, tags, expected):
    m = _seeded(str(tmp_path / "bench_scoring"))
    (top, _score), *_ = m.score_and_rank(str(tmp_path / "bench_scoring"), {"file_path": file_path, "tags": tags}, limit=3)
    assert any(k in top.content.lower() for k in expected), top.content


def test_the_category_bonus_orders_rule_mistake_discovery(tmp_path):
    m = _ms()
    proj = str(tmp_path / "bonus")
    for content, cat in (("Auth handler processes requests", "discovery"),
                         ("MISTAKE: auth handler crashed on empty token", "mistake"),
                         ("Always validate tokens in auth handler", "rule")):
        m.remember_discovery(proj, content, category=cat, relevance=5, related_files=["auth.py"], auto_embed=False)
    assert [e.category for e, _ in m.score_and_rank(proj, {"file_path": "auth.py"}, limit=3)] == ["rule", "mistake", "discovery"]


INJECTION_SEEDS = [
    ("Auth uses JWT tokens validated in middleware", "discovery", 8, ["auth", "security"], ["auth/middleware.py"]),
    ("MISTAKE: removed session check from auth, broke all routes", "mistake", 9, ["auth", "mistake"], ["auth/middleware.py"]),
    ("Auth tokens expire after 24 hours by default", "discovery", 6, ["auth"], ["auth/tokens.py"]),
    ("Always check token expiry before processing requests", "rule", 9, ["auth", "rule"], ["auth/middleware.py"]),
    ("DECISION: use bcrypt for password hashing over argon2", "decision", 7, ["auth", "security"], ["auth/passwords.py"]),
    ("Auth rate limiting: 5 failed attempts locks account", "discovery", 5, ["auth"], ["auth/rate_limit.py"]),
    ("MISTAKE: forgot to hash password before storing", "mistake", 9, ["auth"], ["auth/passwords.py"]),
    ("OAuth2 flow uses PKCE for mobile clients", "discovery", 6, ["auth", "oauth"], ["auth/oauth.py"]),
    ("Database uses PostgreSQL with pgbouncer pooling", "discovery", 7, ["database"], ["db/pool.py"]),
    ("MISTAKE: migration failed, column NOT NULL without default", "mistake", 9, ["database", "mistake"], ["db/migrations.py"]),
    ("Always use parameterized queries to prevent SQL injection", "rule", 9, ["database", "security", "rule"], ["db/queries.py"]),
    ("Connection pool size is 20 for production", "discovery", 5, ["database"], ["db/pool.py"]),
    ("DECISION: use Alembic for migrations over raw SQL", "decision", 7, ["database"], ["db/migrations.py"]),
    ("Database indices on user_id and created_at columns", "discovery", 4, ["database"], ["db/models.py"]),
    ("Always wrap multi-table updates in transactions", "rule", 8, ["database", "rule"], ["db/queries.py"]),
    ("MISTAKE: N+1 query in user list endpoint", "mistake", 8, ["database", "api"], ["db/queries.py", "api/users.py"]),
    ("API rate limiting is 100 req/min per user", "discovery", 5, ["api"], ["api/routes.py"]),
    ("MISTAKE: forgot to validate request body in POST /users", "mistake", 9, ["api", "mistake"], ["api/routes.py", "api/users.py"]),
    ("Always return proper error codes with messages", "rule", 8, ["api", "rule"], ["api/routes.py"]),
    ("API versioning uses URL prefix /v1/, /v2/", "discovery", 6, ["api"], ["api/routes.py"]),
    ("DECISION: use FastAPI over Flask for new endpoints", "decision", 7, ["api"], ["api/routes.py"]),
    ("Pagination defaults to 20 items per page", "discovery", 4, ["api"], ["api/pagination.py"]),
    ("API health check at /health returns service status", "discovery", 3, ["api"], ["api/health.py"]),
    ("React components use strict TypeScript", "discovery", 6, ["frontend"], ["src/App.tsx"]),
    ("DECISION: use Tailwind instead of styled-components", "decision", 7, ["frontend", "css"], ["src/styles/"]),
    ("MISTAKE: forgot SSR hydration mismatch check", "mistake", 8, ["frontend"], ["src/App.tsx"]),
    ("Use React.memo for expensive list renders", "rule", 7, ["frontend", "rule"], ["src/components/"]),
    ("Frontend state managed with Zustand", "discovery", 5, ["frontend", "state"], ["src/store.ts"]),
    ("MISTAKE: bundled devDependencies in production build", "mistake", 7, ["frontend"], ["package.json"]),
    ("Dark mode toggle stored in localStorage", "discovery", 3, ["frontend"], ["src/theme.ts"]),
    ("CI runs on GitHub Actions", "discovery", 4, ["ci"], [".github/workflows/ci.yml"]),
    ("Always run tests before deploying", "rule", 9, ["ci", "testing", "rule"], [".github/workflows/"]),
    ("Deploy uses blue-green strategy", "discovery", 5, ["ci", "deploy"], [".github/workflows/deploy.yml"]),
    ("MISTAKE: CI passed but deploy failed due to missing env var", "mistake", 8, ["ci"], [".github/workflows/deploy.yml"]),
    ("Docker images tagged with git SHA", "discovery", 4, ["ci", "docker"], ["Dockerfile"]),
    ("Project uses monorepo with turborepo", "discovery", 4, [], []),
    ("Always run linter before committing", "rule", 9, ["rule"], []),
    ("Log format is structured JSON", "discovery", 3, ["logging"], ["config/logging.py"]),
    ("MISTAKE: committed .env file with production secrets", "mistake", 10, ["security"], [".env", ".gitignore"]),
    ("Environment variables loaded from .env via python-dotenv", "discovery", 5, ["config"], ["config/settings.py"]),
]

INJECTION_CASES = [
    ("auth/middleware.py", ["auth"], ["session check", "token expiry"], ["PostgreSQL", "React", "Tailwind"]),
    ("db/queries.py", ["database"], ["parameterized", "SQL injection"], ["React", "OAuth", "Tailwind"]),
    ("db/migrations.py", ["database"], ["migration", "NOT NULL"], ["JWT", "React"]),
    ("api/routes.py", ["api"], ["validate"], ["JWT", "React"]),
    ("src/App.tsx", ["frontend"], ["React", "hydration"], ["PostgreSQL", "migration", "parameterized"]),
    ("auth/sessions.py", ["auth"], ["auth"], ["React", "PostgreSQL"]),
    ("db/models.py", ["database"], ["migration"], ["React", "OAuth"]),
    ("api/middleware.py", ["api"], [], ["validate", "React"]),
    ("auth/middleware.py", ["auth"], ["Always check token"], []),
    ("src/components/Button.tsx", ["frontend"], [], ["PostgreSQL", "migration", "SQL injection"]),
    ("db/pool.py", ["database"], [], ["React", "Tailwind", "SSR hydration"]),
    ("auth/handler.py", ["auth"], ["auth"], ["React", "Tailwind"]),
    ("src/App.tsx", ["frontend"], ["React"], ["PostgreSQL", "migration"]),
    ("services/auth_service.py", ["auth", "security"], ["auth"], ["React", "Tailwind"]),
    ("scripts/deploy.sh", ["devops"], [], []),
]


def test_injection_surfaces_the_file_relevant_memories(tmp_path):
    """The cases run in order on one store, as a session's edits do: each
    ranking bumps the access counts the next one reads."""
    proj = str(tmp_path / "bench_injection")
    m = _seeded(proj, INJECTION_SEEDS)
    failures = []
    for file_path, tags, must, must_not in INJECTION_CASES:
        text = " ".join(e.content.lower() for e, _ in m.score_and_rank(proj, {"file_path": file_path, "tags": tags}, limit=3))
        missing = [k for k in must if k.lower() not in text]
        leaked = [k for k in must_not if k.lower() in text]
        if missing or leaked:
            failures.append((file_path, missing, leaked, text))
    assert failures == []


def test_path_matching_is_path_aware():
    """A shared basename across diverging paths is not a match; a generic
    basename needs a full-path signal; a specific filename matches by name."""
    from windvane.store import _file_match_score as S

    svc_b = "/repo/service-b/myapp/core/training/losses/__init__.py"
    svc_a = "/repo/service-a/myapp/core/training/losses/__init__.py"
    eng_b, eng_a = "/repo/service-b/myapp/imagination/engine.py", "/repo/service-a/myapp/imagination/engine.py"
    gate = 0.5
    assert S(svc_b, [svc_a], "") < gate
    assert S(svc_b, [], f"import from '{svc_a}'") < gate
    assert S(svc_b, ["__init__.py"], "") < gate
    assert S(eng_b, [eng_a], "") < gate
    assert S("/ws/proj/README.md", [], "FileNotFoundError: src/README.md") < gate
    assert S(svc_b, ["/other/proj/server.py"], "server crash") == 0.0
    assert S(svc_b, [svc_b], "") == 1.0
    assert S(svc_b, ["service-b/myapp/core/training/losses/__init__.py"], "") == 1.0
    assert S(eng_b, ["engine.py"], "") >= gate
    assert S(eng_b, [], "touched engine.py earlier") >= gate


def test_sub_projects_keep_their_memories_and_inherit_rules(tmp_path):
    ws = tmp_path / "workspace"
    backend, frontend = ws / "backend", ws / "frontend"
    for d in (backend, frontend):
        d.mkdir(parents=True)
    m = _ms()
    m.add_rule(str(ws), "Always run linter before committing")
    m.remember_discovery(str(backend), "Backend uses FastAPI", relevance=7, related_files=["main.py"], auto_embed=False)
    m.remember_discovery(str(frontend), "Frontend uses React with Vite", relevance=7, related_files=["src/App.tsx"], auto_embed=False)
    back = " ".join(e.content for e, _ in m.score_and_rank(str(backend), {"file_path": str(backend / "main.py")}, limit=10))
    assert "FastAPI" in back and "React" not in back
    front = " ".join(e.content for e, _ in m.score_and_rank(str(frontend), {"file_path": str(frontend / "src/App.tsx")}, limit=10))
    assert "React" in front and "FastAPI" not in front
    assert [src for _, src in m.get_rules_with_inheritance(str(backend))] == [m._normalize_path(str(ws))]


def test_the_response_renders_compactly():
    from windvane.store import Response, WorkLog

    text = Response(
        status="success",
        reasoning="Found 2",
        data={"memories": [{"id": "a", "content": "Auth uses JWT", "relevance": 8, "tags": ["auth"]}], "count": 2},
        warnings=["careful"],
        suggestions=["one", "two", "three"],
    ).to_formatted_string()
    assert text.splitlines() == ["Found 2", "  ! careful", "[a] (8) Auth uses JWT #auth", "count: 2", "  > one", "  > two"]
    failed = Response(status="failed", reasoning="Project not found", work_log=WorkLog(what_failed=["lookup"]))
    assert failed.to_formatted_string() == "failed: Project not found\nFAILED: lookup"

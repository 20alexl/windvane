"""What the engine stores goes through the same redaction as the door. Every
secret here is a fake shape."""

import json
from pathlib import Path

import pytest

from windvane.privacy import clean, redact

LONG = "abcdEFGH1234abcdEFGH1234"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLWJ5dGVz"


@pytest.mark.parametrize("text,expected", [
    (f"api_key = {LONG}", "api_key = [redacted]"),
    # A JSON key closes its quote before the colon.
    (f'{{"password": "{LONG}"}}', '{"password": "[redacted]"}'),
    (f"'secret': '{LONG}'", "'secret': '[redacted]'"),
    # The whole dotted value goes, not its first segment.
    (f"Authorization: Bearer {LONG}.second.third", "Authorization: Bearer [redacted]"),
    (f"token={LONG}.rest", "token=[redacted]"),
    # A token by its shape, with no key word before it.
    (f"the cookie held {JWT} today", "the cookie held [redacted] today"),
    ("-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----", "[redacted]"),
    ("AKIAABCDEFGHIJKLMNOP is the id", "[redacted] is the id"),
])
def test_secrets_are_redacted(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize("text", [
    "password = settings.database_password",
    'password = os.environ["DB_PASSWORD"]',
    "uses bearer authentication for the API",
    "the Bearer token is sent in the header",
    "token = settings.TOKEN",
    "tokenizer: a word splitter of some length",
    "commit 0123456789abcdef0123456789abcdef01234567",
])
def test_source_that_names_a_secret_is_not_one(text):
    assert redact(text) == text


def test_half_a_surrogate_pair_becomes_the_replacement_character():
    """Text clipped through the middle of an emoji arrives with half a pair,
    which cannot be encoded: the store's id hash raised on it, and the memory
    was lost."""
    assert redact("clipped \ud83d here") == "clipped � here"
    assert redact("whole \U0001f600 emoji") == "whole \U0001f600 emoji"
    assert clean({"a": ["x\udc00"]}) == {"a": ["x�"]}


def test_structured_values_are_cleaned_by_their_key_and_their_text():
    out = clean({"password": "hunter2", "notes": [f"api_key={LONG}"], "token": 5, "tokens": "many", "n": None})
    assert out == {"password": "[redacted]", "notes": ["api_key=[redacted]"], "token": 5, "tokens": "many", "n": None}


def test_the_door_and_the_engine_redact_alike():
    """The two copies of the patterns, kept in step by hand: the door runs in
    the mod and the engine has no TypeScript."""
    from windvane import privacy

    door = (Path(__file__).resolve().parent.parent / "hooks" / "door.ts").read_text(encoding="utf-8")
    assert f"/{privacy._PREFIXED_KEY.pattern}/g" in door
    assert f"/{privacy._PEM_BLOCK.pattern}/g" in door
    assert r"""\b((?:api[_ -]?key|secret|token|password|passwd)\b["']?\s*[:=]\s*["']?|bearer\s+)""" in door
    assert r"""[A-Za-z0-9_\-/+=]{16,}(?:\.[A-Za-z0-9_\-/+=]+)*""" in door
    assert r"""\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}""" in door


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    from windvane.store import MemoryStore

    return MemoryStore()


def _on_disk(root: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in root.rglob("*.json"))


def test_new_memories_and_rules_store_no_secret(store, tmp_path):
    proj = str(tmp_path / "p")
    assert store.remember_discovery(proj, f"The service key is api_key = {LONG}", auto_embed=False)[0]
    assert store.add_rule(proj, f"Use the key with token={LONG} in staging")[0]
    disk = _on_disk(tmp_path / "store")
    assert LONG not in disk and "api_key = [redacted]" in disk


def test_an_old_memory_is_not_rewritten_on_the_next_save(store, tmp_path):
    """Redaction covers new writes. Text already stored stays as it was: a
    silent rewrite of the whole store could not be undone."""
    proj = str(tmp_path / "p")
    assert store.remember_discovery(proj, "first fact about the parser", auto_embed=False)[0]
    entry = store.get_project(proj).entries[0]
    entry.content = f"older text with api_key = {LONG}"
    store._dirty_projects.add(store.get_project(proj).project_path)
    store._save()
    assert store.remember_discovery(proj, "second fact about the lexer", auto_embed=False)[0]
    assert LONG in _on_disk(tmp_path / "store")


def test_a_banked_checkpoint_stores_no_secret(store, tmp_path):
    from windvane import checkpoints as ck

    ring = tmp_path / "ring"
    ring.mkdir()
    record = {"kind": "manual", "task_description": f"rotate the key, api_key = {LONG}",
              "metadata": {"password": "hunter2"}, "completed_steps": ["one"], "pending_steps": ["two"]}
    ck.write_handoff(record, [ring])
    disk = _on_disk(ring)
    assert LONG not in disk and "hunter2" not in disk
    assert record["task_description"].endswith("[redacted]")
    assert json.loads(next(ring.glob("*.json")).read_text(encoding="utf-8"))


def test_the_task_file_of_a_saved_checkpoint_is_redacted_too(tmp_path, monkeypatch):
    """The ring was cleaned on its way in, and the per-task file written just
    before it was not."""
    from windvane import checkpoints as ck

    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    guard = ck.ContextGuard()
    secret = "password = " + "abcdefghijklmnopqrstuvwx"
    guard.save_checkpoint(
        task_description=f"Rotate the login, {secret}",
        current_step=f"Step 1, {secret}",
        completed_steps=[f"found {secret}"],
        pending_steps=[f"remove {secret}"],
        files_involved=["auth.py"],
        key_decisions=[f"keep {secret} out"],
        project_path=str(tmp_path / "p"),
        handoff_summary=f"left {secret}",
        handoff_context_needed=[secret],
        handoff_warnings=[secret],
    )
    written = list((tmp_path / "store").rglob("task_*.json"))
    assert written
    for path in written:
        assert "abcdefgh" not in path.read_text(encoding="utf-8")
    everything = "\n".join(p.read_text(encoding="utf-8") for p in (tmp_path / "store").rglob("*") if p.is_file())
    assert "abcdefgh" not in everything  # the rings and HANDOFF.md too


def test_every_other_way_in_is_redacted(tmp_path, store):
    from windvane import checkpoints as ck

    secret = "password = " + "abcdefghijklmnopqrstuvwx"
    project = str(tmp_path / "p")
    store.add_priority(f"remember {secret}", project)
    store.remember_discovery(project, "a fact to change later on", auto_embed=False)
    target = next(e for e in store.get_project(project).entries if "change later" in e.content)
    assert store.modify_memory(project_path=project, memory_id=target.id, content=f"changed, {secret}")[0]
    plain = store.remember_discovery(project, "a fact to promote into a rule", auto_embed=False)
    assert plain[0]
    other = next(e for e in store.get_project(project).entries if "promote" in e.content)
    assert store.promote_to_rule(project, other.id, reason=f"since {secret}")[0]
    ring = tmp_path / "ring"
    ring.mkdir()
    ck.write_handoff({"kind": "manual", "task_id": "t1", "summary": "first"}, [ring])
    ck.replace_handoff({"task_id": "t1", "summary": f"amended, {secret}"}, [ring])
    everything = _on_disk(tmp_path)
    assert "amended" in everything and "changed" in everything
    assert "abcdefgh" not in everything

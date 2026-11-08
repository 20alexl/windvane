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


def test_structured_values_are_cleaned_by_their_key_and_their_text():
    out = clean({"password": "hunter2", "notes": [f"api_key={LONG}"], "token": 5, "tokens": "many", "n": None})
    assert out == {"password": "[redacted]", "notes": ["api_key=[redacted]"], "token": 5, "tokens": "many", "n": None}


def test_the_door_and_the_engine_redact_alike():
    """The two copies of the patterns, kept in step by hand: the door runs in
    the mod and the engine has no TypeScript."""
    door = (Path(__file__).resolve().parent.parent / "hooks" / "door.ts").read_text(encoding="utf-8")
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

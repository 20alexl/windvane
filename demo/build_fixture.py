"""Build demo/fixture/*.txt from windvane's REAL output.

The demo shows what the model reads: windvane's hook output reaches the
model, never the screen. Every windvane line in the demo is produced here by
the shipped engine, driven the way Claude Code drives it, against a small
project and a temporary store. demo/play.py only prints these files.

    python demo/build_fixture.py        # rewrite demo/fixture/
    python3 demo/build_fixture.py --seed-live /tmp/wv-demo /tmp/wv-demo/config
                                        # the live recording's world (demo/live_setup.sh)

What runs, in one temporary directory (deleted at the end):
  - the hook events, in-process through ``windvane.events.dispatch(event,
    stdin_json)`` with the stdin Claude Code sends (the daemon's path);
  - the tools, through ``windvane.tools.run`` (what the plugin's registered
    tools call), with CLAUDE_CODE_SESSION_ID set as Claude Code exports it;
  - the compaction brief, as ``python -m windvane.brief`` in a subprocess,
    wrapped the way hooks/compact.ts wraps it.
The plugin's mod writes the context mirror (sessions/<sid>.ctx.json) and its
marker; that TypeScript does not run here, so this script writes the same
records the mod writes (hooks/register.ts) and the `.briefed` marker
hooks/compact.ts writes after it placed the brief.

Isolation: WINDVANE_DIR, HOME, USERPROFILE and CLAUDE_CONFIG_DIR point into
the temporary directory before windvane is imported, every CLAUDE* and
WINDVANE* variable of the calling shell is dropped, no daemon starts and no
miner runs. The fixture files are written verbatim (they hold the temporary
paths); fixture/paths.json names those paths so play.py can rewrite them.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEMO = Path(__file__).resolve().parent
REPO = DEMO.parent
FIXTURE = DEMO / "fixture"

PREV_SID = "0f3c9a4e-0000-4000-8000-00000000d0e1"
SID = "7b21d5c8-0000-4000-8000-00000000d0e2"

# The files play.py shows, in the order it shows them.
BEATS = (
    "session-start",     # SessionStart(startup): rules seeded, the checkpoint restored
    "headsup",           # UserPromptSubmit in the heads-up band
    "checkpoint-now",    # UserPromptSubmit in the checkpoint band
    "checkpoint-saved",  # checkpoint(save) with no fields: the reply
    "compact-brief",     # the message placed inside the compacted conversation
    "compact-banner",    # SessionStart(compact) with the brief already placed
    "pre-edit",          # PreToolUse Edit on items_api/api.py
    "rule",              # PreToolUse Bash `git reset --hard HEAD~1`
)

# The window the mirror reports. On a 200K window the compaction point is
# 200K, the trigger 168K (minus the output reserve), the checkpoint band
# 158K and the heads-up 148K.
WINDOW = 200_000

PYPROJECT = """[project]
name = "items-api"
version = "0.1.0"
"""

README = """# items-api

A small items API: `items_api/api.py` serves GET /items, `items_api/db.py`
owns the connection.
"""

API_PY = '''"""GET /items with page-based paging."""

from items_api.db import count_items, fetch_items

PAGE_SIZE = 20


def paginate(page: int, size: int = PAGE_SIZE) -> dict:
    total = count_items()
    pages = -(-total // size)
    start = (page - 1) * size
    return {"items": fetch_items(start, size), "page": page, "pages": pages}
'''

DB_PY = '''"""The one module that opens a connection."""

import sqlite3

_DB = "items.db"


def _conn():
    return sqlite3.connect(_DB)


def count_items() -> int:
    with _conn() as c:
        return c.execute("select count(*) from items").fetchone()[0]


def fetch_items(start: int, size: int) -> list:
    with _conn() as c:
        rows = c.execute("select id, name from items order by id limit ? offset ?", (size, start))
        return [dict(id=r[0], name=r[1]) for r in rows]
'''

TEST_PY = '''from items_api.api import paginate


def test_last_page_is_served(items_db):
    assert paginate(3, size=20)["items"]
'''

CURSOR_PY = '''"""Opaque cursors for GET /items."""

import base64


def encode_cursor(last_id: int) -> str:
    return base64.urlsafe_b64encode(str(last_id).encode()).decode()


def decode_cursor(cursor: str) -> int:
    return int(base64.urlsafe_b64decode(cursor.encode()).decode())
'''

# The API document the live take reads before its edit. It is sized so one
# read of it carries the session's fill into the checkpoint band of a 100K
# compaction window (demo/live_setup.sh): some 20,000 tokens on top of a
# session's own 30-odd thousand, and small enough that the Read tool
# returns it whole, so the model has no remainder to remark on. The tool
# stopped a take's read at about 64,000 characters counting its line
# prefixes (1,201 of 1,714 lines), so the document stays under 56,000
# characters and 1,150 lines. The door's budget is raised for the take so
# the read reaches the context whole.
API_DOC_MIN_CHARS = 48_000
API_DOC_MAX_CHARS = 56_000
API_DOC_MAX_LINES = 1_150

# plural, singular, what it is, two fields of its own (name, type, rule).
_RESOURCES = (
    ("items", "item", "a thing the shop sells, with a name, a price and the stock on hand",
     ("sku", "string", "The stock-keeping unit, unique per shop, up to 40 characters."),
     ("price_cents", "integer", "The current price in cents; `/prices` keeps the history.")),
    ("tags", "tag", "a label an item carries; many items share one tag",
     ("color", "string", "A six-digit hex color the storefront shows behind the label."),
     ("item_count", "integer", "How many active items carry the tag. Read-only.")),
    ("collections", "collection", "a curated set of items shown together on the storefront",
     ("position", "integer", "Where the collection sits on the home page; lower is higher."),
     ("item_ids", "array of integers", "The items in display order; an archived item is skipped.")),
    ("prices", "price", "a dated price for one item in one currency",
     ("currency", "string", "An ISO 4217 code such as `EUR`."),
     ("valid_from", "string", "The moment the price takes effect; the newest past price wins.")),
    ("stock", "stock level", "the quantity of one item in one warehouse",
     ("warehouse_id", "integer", "The warehouse that holds the stock."),
     ("quantity", "integer", "Units on hand; a negative value is refused.")),
    ("suppliers", "supplier", "a company that restocks items",
     ("lead_days", "integer", "Working days from order to delivery, as agreed."),
     ("contact_email", "string", "Where purchase orders are sent; validated as an address.")),
    ("orders", "order", "a customer's purchase of one or more items",
     ("customer_id", "integer", "The account that placed the order."),
     ("total_cents", "integer", "The sum of the lines at the prices of the moment. Read-only.")),
    ("invoices", "invoice", "the billing record for an order",
     ("due_at", "string", "When payment is due; 30 days after issue by default."),
     ("paid", "boolean", "Whether the invoice was settled in full.")),
    ("customers", "customer", "an account that places orders",
     ("email", "string", "The login and the address receipts go to; unique."),
     ("country", "string", "An ISO 3166-1 alpha-2 code, used for tax and shipping rates.")),
)

_FIELDS = (
    ("id", "integer", "Assigned by the server and never reused."),
    ("name", "string", "Up to 120 characters; leading and trailing spaces are removed."),
    ("slug", "string", "Lowercase letters, digits and hyphens, unique within the resource."),
    ("status", "string", "One of `draft`, `active` or `archived`."),
    ("created_at", "string", "An RFC 3339 timestamp in UTC, set by the server."),
    ("updated_at", "string", "An RFC 3339 timestamp in UTC, moved by every write."),
    ("owner_id", "integer", "The account that created the record."),
    ("notes", "string", "Free text up to 4,000 characters, kept but not searched."),
)

_INTRO = """# items-api reference

Every endpoint is under `/v1`. Requests and responses are JSON, encoded as
UTF-8. Times are UTC in RFC 3339 form. Ids are integers. A request body larger
than one megabyte is refused with 413 before it is read.

## Paging

List endpoints take `page=` (from 1) and `per_page=` (1 to 100, default 20) and
answer with `items`, `page` and `pages`. Clients must keep `page=` working: the
storefront and the back office both send it, and the back office links to a
page number in its own URLs. Cursor paging is planned for GET /items so that a
list does not skip or repeat rows while items are being added or archived
between two requests; the plan is an opaque `cursor=` parameter that encodes
the last id seen, answered with `next_cursor`, with `page=` kept as it is.

## Authentication

Send `Authorization: Bearer <token>`. A token is scoped to one account and one
of the roles `read`, `write` or `admin`. A missing or expired token answers
401, a token without the needed role 403. Tokens are issued in the back office
and can be revoked there; a revoked token fails within a minute.

## Errors

An error answer has `error.code`, `error.message` and, for a refused write,
`error.fields`, a map from field name to the reason it was refused. The
message is for a developer reading a log, not for showing to a customer.

| Status | Code | When |
|---|---|---|
| 400 | `bad_request` | A field is missing, or of the wrong type, or the body is not JSON |
| 401 | `unauthenticated` | No token, or an expired or revoked one |
| 403 | `forbidden` | The token's role does not allow the request |
| 404 | `not_found` | No record with that id, or none the token may see |
| 409 | `conflict` | The write would duplicate a unique field |
| 413 | `too_large` | The body is over one megabyte |
| 422 | `invalid` | The fields parse but the record would not be valid |
| 429 | `rate_limited` | Over the limit; `Retry-After` says how many seconds to wait |

## Rate limits

Each token may make 600 requests a minute, counted over a sliding window.
Every answer carries `X-RateLimit-Remaining`. A list request with `per_page=100`
counts once, so a client that reads in large pages stays well inside the
limit. Exports are not counted against it.

## Idempotency

A POST may carry `Idempotency-Key`, any string up to 128 characters. The first
request with a key is executed; a repeat with the same key and the same body
within 24 hours answers with the stored result and does nothing again. A repeat
with the same key and a different body answers 409.

## Webhooks

A webhook is called with a POST whose body is `{"event": "<name>", "data":
<record>, "at": "<time>"}`. The call is signed: `X-Signature` is the HMAC-SHA256
of the body with the webhook's secret. A delivery that does not answer 2xx
within ten seconds is retried after one minute, ten minutes and one hour, then
dropped and recorded in `/audit`.
"""

_CHANGELOG = """## Changelog

### 1.4.0

- Idempotency keys on every POST.
- `updated_since` on every list, so a client can poll for changes without
  reading every page.

### 1.3.0

- Webhook deliveries are signed, and failed deliveries are retried three times.
- `/exports` builds files in the background and answers 202 with the job.

### 1.2.0

- `/audit` records every write with the token's account and the diff.
- Archiving replaces deletion everywhere; an archived record stays readable.

### 1.1.0

- `sort` with a leading `-` for descending order.
- Rate limits are per token, not per account.

### 1.0.0

- The first stable version: items, tags, collections, prices, stock, suppliers,
  orders, shipments, invoices, customers and reviews.
"""


def _record(singular: str, extras: list, n: int) -> str:
    fields = {
        "id": n,
        "name": f"{singular} {n}",
        "slug": f"{singular}-{n}",
        "status": "active" if n % 3 else "draft",
        "created_at": f"2026-09-{(n % 28) + 1:02d}T09:{n % 60:02d}:00Z",
        "updated_at": f"2026-10-{(n % 4) + 1:02d}T15:{(n * 7) % 60:02d}:00Z",
        "owner_id": 100 + n % 7,
        "notes": "",
    }
    for field, kind, _rule in extras:
        if kind == "integer":
            fields[field] = 10 * n + 4
        elif kind == "boolean":
            fields[field] = n % 2 == 0
        elif kind.startswith("array"):
            fields[field] = [n, n + 1] if "integers" in kind else [f"{singular}.created", f"{singular}.updated"]
        elif kind == "object":
            fields[field] = {"status": ["draft", "active"]}
        else:
            fields[field] = f"{field}-{n}"
    return json.dumps(fields)


def api_doc() -> str:
    """The items-api reference as the fixture project documents it."""
    out = [_INTRO]
    for plural, singular, blurb, *extras in _RESOURCES:
        a_singular = ("an " if singular[0] in "aeiou" else "a ") + singular
        out += [
            f"## {plural}",
            "",
            f"{a_singular[0].upper()}{a_singular[1:]} is {blurb}. Records are never deleted: archiving",
            f"{a_singular} takes it out of every list and keeps it readable by id, and every write to",
            f"{a_singular} is recorded in `/audit` with the account that made it. The fields every {singular}",
            f"carries are listed under Fields; `{extras[0][0]}` and `{extras[1][0]}` are its own.",
            "",
            f"### GET /{plural}",
            "",
            f"Lists {plural}, newest first unless `sort` says otherwise. Without `status`, only",
            f"`draft` and `active` records are listed.",
            "",
            "| Parameter | Type | Meaning |",
            "|---|---|---|",
            "| `page` | integer | The page, from 1 |",
            "| `per_page` | integer | Rows per page, 1 to 100 |",
            "| `status` | string | Only records in this status, `archived` included when asked |",
            "| `updated_since` | string | Only records changed after this time |",
            "| `q` | string | Only records whose name or slug starts with this, per word |",
            "| `sort` | string | `created_at`, `updated_at` or `name`, with a leading `-` for descending |",
            "",
            "```json",
            "{",
            f'  "items": [{_record(singular, extras, 1)}],',
            '  "page": 1,',
            '  "pages": 4',
            "}",
            "```",
            "",
            f"### GET /{plural}/{{id}}",
            "",
            f"One {singular} with every field. 404 when there is no such id or the token's",
            f"account may not see it; the two cases are not told apart.",
            "",
            f"### POST /{plural}",
            "",
            f"Creates {a_singular}. `name` is required and `slug` is derived from it when absent.",
            f"Answers 201 with the record as stored, so a client reads back the fields the server",
            f"set. A `slug` that another {singular} already has answers 409.",
            "",
            "| Field | Type | Rule |",
            "|---|---|---|",
            *(f"| `{field}` | {kind} | {rule} |" for field, kind, rule in _FIELDS[1:4]),
            *(f"| `{field}` | {kind} | {rule} |" for field, kind, rule in extras),
            f"| `notes` | string | {_FIELDS[7][2]} |",
            "",
            "```json",
            _record(singular, extras, 2),
            "```",
            "",
            f"### PATCH /{plural}/{{id}}",
            "",
            "Changes the fields sent and leaves the rest. A field set to `null` is cleared when",
            "it is optional and refused with 422 when it is not. `id`, `created_at`,",
            "`updated_at` and `owner_id` cannot be sent. The answer is the whole record.",
            "",
            f"### DELETE /{plural}/{{id}}",
            "",
            f"Archives the {singular}: `status` becomes `archived` and the answer is 204. The",
            f"record stays readable by id and leaves every list unless `status=archived` is",
            f"asked for. There is no way to remove a {singular} for good through the API.",
            "",
            f"### Events",
            "",
            f"`{singular}.created`, `{singular}.updated` and `{singular}.archived` are delivered to",
            f"every webhook subscribed to them, with the {singular} as `data`.",
            "",
            "### Fields",
            "",
            "| Field | Type | Rule |",
            "|---|---|---|",
            *(f"| `{field}` | {kind} | {rule} |" for field, kind, rule in _FIELDS),
            *(f"| `{field}` | {kind} | {rule} |" for field, kind, rule in extras),
            "",
            "### Refusals by field",
            "",
            *(f"- `{field}`: {rule.rstrip('.')}; anything else answers 422 with the field named." for field, _kind, rule in (*_FIELDS[1:4], *extras)),
            "",
            "### Permissions",
            "",
            f"A `read` token lists and reads {plural}. A `write` token also creates and changes",
            f"them. Archiving a {singular} needs `admin`, as does reading one that another account",
            f"owns; a `read` or `write` token sees its own account's {plural} only.",
            "",
            "### Common questions",
            "",
            f"- Why does a {singular} I just created not appear in the list? The list is read from a",
            f"  replica that can lag by a second or two; read it by id, which is always current.",
            f"- Can I change `{extras[0][0]}` after creation? Yes, with PATCH, under the rule above;",
            f"  the change is recorded in `/audit` like any other.",
            f"- How do I find {plural} changed since my last poll? Pass `updated_since` with the",
            f"  `updated_at` of the newest record you hold, and page through the answer.",
            "",
            "### Examples",
            "",
            "```bash",
            f'curl -H "Authorization: Bearer $TOKEN" "https://api.example.test/v1/{plural}?per_page=50&sort=-updated_at"',
            f'curl -H "Authorization: Bearer $TOKEN" -X POST -d \'{{"name": "new {singular}"}}\' https://api.example.test/v1/{plural}',
            f'curl -H "Authorization: Bearer $TOKEN" -X PATCH -d \'{{"status": "active"}}\' https://api.example.test/v1/{plural}/2',
            "```",
            "",
        ]
    out.append(_CHANGELOG)
    text = "\n".join(out)
    lines = text.count("\n")
    assert len(text) >= API_DOC_MIN_CHARS, f"the API document is {len(text)} characters, under {API_DOC_MIN_CHARS}"
    assert len(text) <= API_DOC_MAX_CHARS, f"the API document is {len(text)} characters, over {API_DOC_MAX_CHARS}"
    assert lines <= API_DOC_MAX_LINES, f"the API document has {lines} lines, over {API_DOC_MAX_LINES}"
    return text


# --------------------------------------------------------------------------
# The temporary world: set up before windvane is imported.
# --------------------------------------------------------------------------


class World:
    def __init__(self, root: Path, config: "Path | None" = None):
        self.root = root
        self.home = root / "home"
        self.store = root / "store"
        self.config = config or self.home / ".claude"
        self.project = root / "proj"

    def isolate(self) -> None:
        for k in list(os.environ):
            if k.upper().startswith(("CLAUDE", "WINDVANE")):
                del os.environ[k]
        os.environ.update(
            WINDVANE_DIR=str(self.store),
            WINDVANE_NO_DAEMON="1",
            WINDVANE_LIVE_MINE="0",
            CLAUDE_CONFIG_DIR=str(self.config),
            HOME=str(self.home),
            USERPROFILE=str(self.home),
            PYTHONIOENCODING="utf-8",
        )
        tmp = Path(tempfile.gettempdir()).resolve()
        real_home = Path(os.path.expanduser("~")).resolve()
        for p in (self.store, self.home, self.project, self.config):
            p = p.resolve()
            assert tmp in p.parents, f"{p} is not under the temp dir"
        assert self.home.resolve() == real_home, "HOME was not redirected"
        sys.path.insert(0, str(REPO))

    def project_files(self) -> None:
        files = {
            "pyproject.toml": PYPROJECT,
            "README.md": README,
            "docs/API.md": api_doc(),
            "items_api/__init__.py": "",
            "items_api/api.py": API_PY,
            "items_api/db.py": DB_PY,
            "tests/test_api.py": TEST_PY,
        }
        for rel, body in files.items():
            p = self.project / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body.encode("utf-8"))
        self.config.mkdir(parents=True, exist_ok=True)
        (self.store / "sessions").mkdir(parents=True, exist_ok=True)

    def git(self, *args: str) -> None:
        empty = self.root / "gitconfig"
        empty.write_bytes(b"")
        env = dict(os.environ, GIT_CONFIG_GLOBAL=str(empty), GIT_CONFIG_NOSYSTEM="1",
                   GIT_AUTHOR_NAME="demo", GIT_AUTHOR_EMAIL="",
                   GIT_COMMITTER_NAME="demo", GIT_COMMITTER_EMAIL="")
        subprocess.run(["git", *args], cwd=str(self.project), env=env, check=True,
                       capture_output=True, stdin=subprocess.DEVNULL, timeout=60)

    def two_commits(self) -> None:
        self.git("init", "-q", "-b", "main")
        self.git("add", "pyproject.toml", "README.md", "items_api/__init__.py", "items_api/db.py")
        self.git("commit", "-q", "-m", "items table and the connection module")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "GET /items with page-based paging")

    def file(self, rel: str) -> str:
        return str(self.project / rel)


# --------------------------------------------------------------------------
# A Claude Code session transcript: append-only records on one parent chain.
# --------------------------------------------------------------------------


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"


class Transcript:
    def __init__(self, path: Path, sid: str, cwd: str):
        self.path, self.sid, self.cwd = path, sid, cwd
        self.parent = None
        self.n = 0
        self.lines: list[str] = []

    def _rec(self, typ: str, content, t: float) -> None:
        self.n += 1
        uid = f"{self.sid[:8]}-{self.n:04d}"
        rec = {"uuid": uid, "parentUuid": self.parent, "type": typ, "isSidechain": False,
               "sessionId": self.sid, "cwd": self.cwd, "gitBranch": "main", "timestamp": _iso(t),
               "message": {"role": typ, "content": content}}
        self.parent = uid
        self.lines.append(json.dumps(rec))

    def prompt(self, text: str, t: float) -> None:
        self._rec("user", text, t)

    def tool(self, cid: str, name: str, inp: dict, result: str, t: float) -> None:
        self._rec("assistant", [{"type": "tool_use", "id": cid, "name": name, "input": inp}], t)
        self._rec("user", [{"type": "tool_result", "tool_use_id": cid, "content": [{"type": "text", "text": result}]}], t)

    def say(self, text: str, t: float) -> None:
        self._rec("assistant", [{"type": "text", "text": text}], t)

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(("\n".join(self.lines) + "\n").encode("utf-8"))


# --------------------------------------------------------------------------
# The engine's entry points, as Claude Code and the plugin call them.
# --------------------------------------------------------------------------


def hook(world: World, event: str, payload: dict) -> str:
    """One hook event in-process; the additionalContext it emitted ('' when silent)."""
    from windvane import events

    payload = {"cwd": str(world.project), **payload}
    prev = os.getcwd()
    os.chdir(world.project)
    try:
        out = events.dispatch(event, json.dumps(payload)).strip()
    finally:
        os.chdir(prev)
    if not out:
        return ""
    try:
        got = json.loads(out.splitlines()[-1])
    except ValueError:
        return out
    return str((got.get("hookSpecificOutput") or {}).get("additionalContext") or "")


def tool(world: World, sid: str, name: str, arguments: dict) -> str:
    """One call of a registered tool, as the plugin serves it."""
    from windvane import tools
    from windvane.events import common

    common._session_id = ""
    os.environ["CLAUDE_CODE_SESSION_ID"] = sid
    prev = os.getcwd()
    os.chdir(world.project)
    try:
        got = tools.run({"tool": name, "arguments": arguments})
    finally:
        os.chdir(prev)
        common._session_id = ""
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
    assert not got.get("isError"), got
    return str(got.get("text") or "")


def mirror(world: World, sid: str, used: int) -> None:
    """The record the mod writes every 10 s (hooks/register.ts), and its marker."""
    sessions = world.store / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    ts = time.time()
    rec = {"session_id": sid, "ts": ts, "source": "mod", "plugin": "windvane",
           "total_input_tokens": used, "context_window_size": WINDOW,
           "used_percentage": round(100.0 * used / WINDOW, 1),
           "model_id": "claude-fable-5-1", "model_name": "claude-fable-5-1"}
    (sessions / f"{sid}.ctx.json").write_bytes(json.dumps(rec).encode("utf-8"))
    (sessions / f"{sid}.mod").write_bytes(json.dumps({"plugin": "windvane", "ts": ts}).encode("utf-8"))


def compact_brief(world: World, sid: str) -> str:
    """What hooks/compact.ts places after the compaction summary: the brief
    CLI's rules and checkpoint blocks, joined as joinBlocks joins them, in
    the <windvane-compact> tags. Then the `.briefed` marker it writes."""
    env = dict(os.environ, PYTHONPATH=str(REPO), CLAUDE_CODE_SESSION_ID=sid)
    r = subprocess.run([sys.executable, "-m", "windvane.brief", "--project", str(world.project),
                        "--session", sid, "--json", "--checkpoint"],
                       cwd=str(world.project), env=env, capture_output=True, text=True,
                       encoding="utf-8", stdin=subprocess.DEVNULL, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(r.stdout)
    body = "\n\n".join("\n".join(b) for b in (got.get("rules") or [], got.get("checkpoint") or []) if b)
    assert body, "the brief came back empty"
    marker = world.store / "sessions" / f"{sid}.briefed"
    marker.write_bytes(json.dumps({"plugin": "windvane", "ts": time.time()}).encode("utf-8"))
    return f"<windvane-compact>\n{body}\n</windvane-compact>"


# --------------------------------------------------------------------------
# The scene.
# --------------------------------------------------------------------------


def _no_miner() -> None:
    # A hook test never spawns the miner; neither does the demo.
    from windvane.mining import background

    background.start_mining_background = lambda *a, **k: False  # type: ignore[assignment]
    background.is_mining_running = lambda *a, **k: True  # type: ignore[assignment]


def seed_yesterday(world: World, now: float) -> None:
    """Yesterday's session, as the store keeps it: a past mistake for the api
    file, a remembered fact about it, and a closing checkpoint dated 20 hours
    back. The rules are not seeded here: the first SessionStart seeds them."""
    proj = str(world.project)
    api, db, test = (world.file(r) for r in ("items_api/api.py", "items_api/db.py", "tests/test_api.py"))
    tool(world, PREV_SID, "log", {"operation": "mistake", "project_path": proj, "file_path": api,
                                  "description": "paginate() in api.py skipped the last page",
                                  "how_to_avoid": "ceil the page count"})
    tool(world, PREV_SID, "memory", {"operation": "remember", "project_path": proj,
                                     "content": "items_api/api.py: old clients still send page=; keep it working until v2"})
    # Yesterday's closing checkpoint, written by the engine's ring writer
    # (what checkpoint(save) writes, dated to when that session ended: the
    # save's own clock is bound at import and cannot be set back).
    from windvane import checkpoints as ck
    from windvane import repo_state

    y = now - 20 * 3600
    ck.write_handoff({
        "kind": "manual", "task_id": f"task_{int(y)}", "created": y, "session_id": PREV_SID,
        "project_path": proj, "commit": repo_state.head(proj),
        "task_description": "Page-based pagination for GET /items",
        "summary": "Paging shipped and the last page is served; cursor paging is next",
        "completed_steps": ["page= and size= on GET /items", "Last page fixed"],
        "next_steps": ["Switch GET /items to cursor pagination", "Keep page= working for old clients"],
        "files_in_progress": [api, db, test],
    }, [ck.project_ring_dir(proj), ck.global_ring_dir()])


def scene(world: World) -> dict[str, str]:
    _no_miner()
    proj = str(world.project)
    api, db, cursor = (world.file(r) for r in ("items_api/api.py", "items_api/db.py", "items_api/cursor.py"))
    now = time.time()
    out: dict[str, str] = {}

    seed_yesterday(world, now)

    # ---- Today's session ---------------------------------------------------
    tpath = world.config / "projects" / "proj" / f"{SID}.jsonl"
    live = Transcript(tpath, SID, proj)
    t = now - 2 * 3600
    mirror(world, SID, 24_000)
    out["session-start"] = hook(world, "session_start_json", {
        "session_id": SID, "source": "startup", "hook_event_name": "SessionStart",
        "permission_mode": "default", "transcript_path": str(tpath)})

    ask = "Switch GET /items to cursor pagination"
    live.prompt(ask, t)
    live.write()
    hook(world, "prompt_json", {"session_id": SID, "hook_event_name": "UserPromptSubmit", "prompt": ask,
                                "transcript_path": str(tpath)})
    for n, subject in enumerate(("Add encode_cursor / decode_cursor helpers",
                                 "Wire the cursor into paginate()",
                                 "Cursor round-trip tests"), start=1):
        live.tool(f"tc{n}", "TaskCreate", {"subject": subject, "description": subject},
                  f"Task #{n} created successfully: {subject}", t + 10 * n)
    live.tool("tu1", "TaskUpdate", {"taskId": "1", "status": "in_progress"}, "Updated task #1 status", t + 60)
    world_cursor = world.project / "items_api" / "cursor.py"
    world_cursor.write_bytes(CURSOR_PY.encode("utf-8"))
    hook(world, "pre_edit_json", {"session_id": SID, "hook_event_name": "PreToolUse", "tool_name": "Write",
                                  "tool_input": {"file_path": cursor, "content": CURSOR_PY}})
    live.tool("w1", "Write", {"file_path": cursor, "content": CURSOR_PY}, "ok", t + 300)
    hook(world, "post_edit_json", {"session_id": SID, "hook_event_name": "PostToolUse", "tool_name": "Write",
                                   "tool_input": {"file_path": cursor}, "tool_response": {"filePath": cursor}})
    live.tool("e1", "Edit", {"file_path": db, "old_string": "order by id limit ? offset ?",
                             "new_string": "where id > ? order by id limit ?"}, "ok", t + 600)
    hook(world, "post_edit_json", {"session_id": SID, "hook_event_name": "PostToolUse", "tool_name": "Edit",
                                   "tool_input": {"file_path": db}, "tool_response": {"filePath": db}})
    live.tool("tu2", "TaskUpdate", {"taskId": "1", "status": "completed"}, "Updated task #1 status", t + 700)
    live.tool("tu3", "TaskUpdate", {"taskId": "3", "status": "in_progress"}, "Updated task #3 status", t + 710)
    live.tool("b1", "Bash", {"command": "pytest -q"}, "..............\n14 passed in 0.52s", t + 900)
    hook(world, "bash_json", {"session_id": SID, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                              "tool_input": {"command": "pytest -q"},
                              "tool_response": {"stdout": "..............\n14 passed in 0.52s", "stderr": ""}})
    live.tool("tu4", "TaskUpdate", {"taskId": "3", "status": "completed"}, "Updated task #3 status", t + 920)
    live.say("The cursor helpers are written and the round-trip tests pass (14 passed).\n\n"
             "Wiring the cursor into paginate() in items_api/api.py is next.", t + 930)
    live.write()

    # The context fills. The heads-up rides on the next prompt...
    mirror(world, SID, 150_000)
    ask = "Good. Keep going."
    live.prompt(ask, t + 3600)
    live.write()
    out["headsup"] = hook(world, "prompt_json", {"session_id": SID, "hook_event_name": "UserPromptSubmit",
                                                 "prompt": ask, "transcript_path": str(tpath)})
    live.tool("tu5", "TaskUpdate", {"taskId": "2", "status": "in_progress"}, "Updated task #2 status", t + 3660)
    live.say("Starting on paginate(): it takes a cursor and keeps page= for old clients.\n\n"
             "Wiring the cursor into paginate() is next; the docs note for page= after that.", t + 3700)
    live.write()

    # ...and CHECKPOINT NOW on the one after.
    mirror(world, SID, 161_000)
    ask = "Now wire the cursor into the endpoint."
    live.prompt(ask, t + 5400)
    live.write()
    out["checkpoint-now"] = hook(world, "prompt_json", {"session_id": SID, "hook_event_name": "UserPromptSubmit",
                                                        "prompt": ask, "transcript_path": str(tpath)})

    # The note says to finish the step first: the model does the edit, marks
    # the task done, then answers with a bare checkpoint(save): the
    # recorder's draft, which now holds the finished work.
    live.tool("e2", "Edit", {"file_path": api, "old_string": "def paginate(page: int, size: int):",
                             "new_string": "def paginate(page: int, size: int, cursor: str | None = None):"},
              "ok", t + 5420)
    hook(world, "post_edit_json", {"session_id": SID, "hook_event_name": "PostToolUse", "tool_name": "Edit",
                                   "tool_input": {"file_path": api}, "tool_response": {"filePath": api}})
    live.tool("tu6", "TaskUpdate", {"taskId": "2", "status": "completed"}, "Updated task #2 status", t + 5440)
    live.write()
    saved = tool(world, SID, "checkpoint", {"operation": "save"})
    out["checkpoint-saved"] = saved
    live.tool("ck1", "mcp__windvane__checkpoint", {"operation": "save"}, saved, t + 5460)
    hook(world, "post_batch_json", {"session_id": SID, "hook_event_name": "PostToolBatch", "tool_calls": [
        {"tool_name": "mcp__windvane__checkpoint", "tool_input": {"operation": "save"}, "tool_use_id": "ck1",
         "tool_response": saved}]})
    # The turn ends on the closing lines; the turn-end hook brings the record
    # just saved up to them, so the handoff carries this turn's own words.
    closing = ("The cursor is wired into paginate() and page= still works for old clients. "
               "Next is the docs note for page= in docs/API.md, then the cursor tests in tests/test_api.py.")
    live.say(closing, t + 5470)
    live.write()
    hook(world, "stop_json", {"session_id": SID, "hook_event_name": "Stop", "stop_hook_active": False,
                              "last_assistant_message": closing, "transcript_path": str(tpath)})

    # The compaction: PreCompact, the mod's brief inside the compacted
    # conversation, PostCompact, then SessionStart(compact).
    hook(world, "pre_compact_json", {"session_id": SID, "hook_event_name": "PreCompact", "trigger": "auto",
                                     "transcript_path": str(tpath)})
    out["compact-brief"] = compact_brief(world, SID)
    hook(world, "post_compact_json", {"session_id": SID, "hook_event_name": "PostCompact", "trigger": "auto",
                                      "compact_summary": "", "transcript_path": str(tpath)})
    time.sleep(0.05)
    mirror(world, SID, 38_000)
    out["compact-banner"] = hook(world, "session_start_json", {
        "session_id": SID, "source": "compact", "hook_event_name": "SessionStart",
        "permission_mode": "default", "transcript_path": str(tpath)})

    # The model reaches for the file it broke yesterday.
    out["pre-edit"] = hook(world, "pre_edit_json", {
        "session_id": SID, "hook_event_name": "PreToolUse", "tool_name": "Edit",
        "tool_input": {"file_path": api,
                       "old_string": "def paginate(page: int, size: int = PAGE_SIZE) -> dict:",
                       "new_string": "def paginate(page: int = 1, size: int = PAGE_SIZE, cursor: str = \"\") -> dict:"}})

    # Then for a hard reset, in auto mode: no permission prompt stands before it.
    out["rule"] = hook(world, "pre_bash_json", {
        "session_id": SID, "hook_event_name": "PreToolUse", "tool_name": "Bash",
        "permission_mode": "auto", "tool_use_id": "toolu_demo_reset",
        "tool_input": {"command": "git reset --hard HEAD~1"}})
    return out


def build(dest: Path) -> dict[str, str]:
    root = Path(tempfile.mkdtemp(prefix="windvane-demo-")).resolve()
    world = World(root)
    try:
        world.isolate()
        world.project_files()
        world.two_commits()
        raw = scene(world)
        paths = {"root": str(root), "project": str(world.project), "home": str(world.home),
                 "store": str(world.store)}
    finally:
        shutil.rmtree(root, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    texts: dict[str, str] = {}
    for beat in BEATS:
        text = raw.get(beat, "").replace("\r\n", "\n").rstrip() + "\n"
        assert text.strip(), f"beat {beat} came back empty"
        (dest / f"{beat}.txt").write_bytes(text.encode("utf-8"))
        texts[beat] = text
    (dest / "paths.json").write_bytes((json.dumps(paths, indent=2) + "\n").encode("utf-8"))
    return texts


def seed_live(root: Path, config: Path) -> None:
    """The world a live recording starts from (demo/live_setup.sh): the
    project with its two commits under ``root``/proj and yesterday's store
    under ``root``/store. Nothing is deleted; the caller starts from an empty
    ``root``."""
    world = World(root.resolve(), config.resolve())
    world.isolate()
    world.project_files()
    world.two_commits()
    _no_miner()
    seed_yesterday(world, time.time())


def main(argv: list[str]) -> int:
    if argv[:1] == ["--seed-live"]:
        # build_fixture.py --seed-live <root> <claude config dir>
        seed_live(Path(argv[1]), Path(argv[2]))
        return 0
    texts = build(FIXTURE)
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    for beat in BEATS:
        print(f"--- {beat} ---\n{texts[beat]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

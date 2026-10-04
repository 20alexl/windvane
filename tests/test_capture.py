"""windvane.capture: the one judgement behind every stored decision.

The shape gate, the regex tier and the shared capture are scored on a
neutral corpus no session wrote (CORPUS below: 120 decisions, 140 not), and
pinned with floors a little under what they score, so a regression shows
and a small corpus edit does not. Never tuned on any one session's
sentences. The semantic tier needs the daemon and is skipped here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from windvane import capture
from windvane.capture import looks_like_correction, looks_like_decision, why_not


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


@pytest.fixture
def regex_only(monkeypatch):
    """The scorer off: the regex tier and the shape gate decide."""
    monkeypatch.setattr(capture, "score_decision_semantic", lambda text, server_only=False: (0.0, ""))


# ---------------------------------------------------------------------------
# The shape gate
# ---------------------------------------------------------------------------


def test_the_decision_gate_is_about_form():
    assert looks_like_decision("DECISION: from now on always use the repository pattern for data access")
    assert looks_like_decision("DECISION: (from user) leave the trash folders for now")
    assert looks_like_decision("DECISION: (confirmed) I'll switch the parser to the streaming reader. Then the report follows with numbers 1 2 3.")
    assert not looks_like_decision("DECISION: (from user) also what shell is still running?") and why_not("what shell is running?") == "question"
    assert why_not("DECISION: ok looks good") in ("acknowledgement", "too short")
    assert why_not("DECISION: Count: 2911 outcomes: .=2907 s=4, 0 FAILED/ERROR.") == "count, table or commit report"
    assert why_not("DECISION: `scripts/system_map.py --check`: the map is current") == "starts like code or a path"
    assert not looks_like_decision("DECISION: Once phase 3 is complete")
    assert not looks_like_decision("DECISION: (confirmed) Checkpoint saved (task_1). Here is where the goal stands; use the ring.")
    assert looks_like_correction("USER PREFERENCE: no, keep the old name, I meant the other module")
    assert not looks_like_correction("USER PREFERENCE: ok so give me the status and where everything is at")


def test_bare_strips_the_prefixes_and_keeps_a_confirmed_proposal_only():
    assert capture.bare("DECISION: (from user) use the registry") == "use the registry"
    assert capture.bare("USER PREFERENCE: keep it short") == "keep it short"
    assert capture.bare("DECISION: (confirmed) I'll use the queue. Then 3 passed.") == "I'll use the queue."
    assert capture.bare("") == ""


def test_a_yes_plus_a_one_off_instruction_is_not_a_decision():
    for s in ("approved, go ahead with steps 1 to 3 and hold step 4", "ok run it, but not on the shared box",
              "approved. leave item five for later", "go ahead with everything except the rename",
              "all recommendations accepted, don't touch the deploy script yet"):
        assert why_not(s) == "a one-off instruction", s
        assert not looks_like_decision(s) and not looks_like_correction(s)
    assert looks_like_decision("DECISION: (from user) leave the trash folders for now")
    assert looks_like_decision("from now on every pull request needs a test")


def test_an_assessment_and_a_three_word_fragment_are_not_stored():
    assert why_not("should be good for the browser now") == "a status assessment"
    assert not looks_like_decision("DECISION: should be fine after the restart")
    assert looks_like_decision("DECISION: errors should be logged with the request id")
    assert not looks_like_correction("USER PREFERENCE: no, keep those.")
    assert looks_like_correction("USER PREFERENCE: no, keep those helper lines.")


def test_an_address_or_a_secret_is_never_a_decision():
    assert why_not("let's use the paid plan, account someone@example.com") == "carries an address or a secret"
    assert why_not("switch to the new key: api_key=[redacted]") == "carries an address or a secret"
    assert why_not("always use the vendor token x7k2m9q4r8t1v5w3z6p0 for the feed") == "carries an address or a secret"
    assert looks_like_decision("always use the capture corpus as the arbiter for the gate")
    assert looks_like_decision("from now on pin torch to 2.4.1 in requirements.txt")


def test_a_relayed_message_a_hash_led_line_and_a_paste_are_not_decisions():
    assert why_not('relayed: <agent-message from="worker-2"> use the registry for every lookup') == "markup or machine text"
    assert why_not('Another session sent a message:\n<agent-message from="worker-2">\nuse the registry') != ""
    assert why_not("(box unreachable.)\n168d0b7d: gate tests moved to the report module") == "a multi-line paste"
    assert why_not("168d0b7d: gate tests moved to the report module, always") != ""
    assert not looks_like_decision("DECISION: let's use the registry\nfor every alias lookup")
    assert looks_like_decision("DECISION: let's use the registry for every alias lookup")
    assert looks_like_decision("use:\n- FastAPI for routes\n- SQLAlchemy for ORM"), "a list under one lead is one choice"


def test_an_approval_verdict_a_hedge_and_an_unmarked_question_are_not_decisions():
    """Three shapes a store review found (2026-09-25): a verdict on a plan
    whose content is not in the sentence, a leaning held loosely, and a
    question typed without its mark."""
    assert why_not("the caching plan is approved") == "an approval verdict"
    assert why_not("approve the three fixes and the rename") == "an approval verdict"
    assert why_not("the fix set is approved, proceed") == "an approval verdict"
    assert why_not("would it be better to split the module") == "question"
    assert why_not("any reason not to use the queue here") == "question"
    assert why_not("which one do you prefer for the store") == "question"
    for s in ("I think we should probably use the queue here", "we could go with sqlite for now, or not",
              "if you think so, use the cache layer"):
        assert not looks_like_decision(s), s
    assert not looks_like_correction("which one do you prefer for the store")
    # a rule with an approval in front of it is still a rule; "do not" is not a question opener
    assert looks_like_decision("approved: from now on always pin the parser version")
    assert looks_like_decision("do not use sleep in the tests, use proper waits")
    assert looks_like_decision("when in doubt, use the registry for the lookup")


# ---------------------------------------------------------------------------
# The regex tier
# ---------------------------------------------------------------------------


def test_the_regex_tier_keeps_the_typed_words():
    """The typo corrector rewrote real short words into trigger words
    ("one" to "use", "pip" to "pick", "stack" to "stick") and the extractor
    returned that lowercased working text, so a stored decision could read
    as nothing the person typed (2026-09-23)."""
    f = capture._fix_typo
    assert f("one") == "one" and f("pip") == "pip" and f("ever") == "ever"
    assert f("step") == "step" and f("chance") == "chance" and f("remote") == "remote"
    assert f("swtich") == "switch" and f("plase") == "please" and f("useing") == "using"
    assert f("impliment") == "implement"
    assert f("strict") == "strict" and f("using") == "using", "common words are never corrected"
    assert capture._normalize_typos("Plase SWTICH to uv.") == "please switch to uv."
    score, text = capture._score_decision_intent("Let's use PostgreSQL instead of SQLite for the main store, the one every reader opens.")
    assert score >= 0.6 and text.startswith("Let's use PostgreSQL instead of SQLite")
    score, text = capture._score_decision_intent("stop using console.log for debugging, use the logger")
    assert score >= 0.6 and text == "stop using console.log"


def test_a_rule_that_opens_the_sentence_clears_the_regex_tier():
    for s in ("from now on every pull request needs a test", "always pin dependency versions in the lockfile",
              "don't use sleep in tests, use proper waits"):
        assert capture._score_decision_intent(s)[0] >= 0.6, s
    for s in ("always been like this in staging", "never mind the failing job", "don't worry about the lint warning"):
        assert capture._score_decision_intent(s)[0] < 0.6, s


def test_cut_words_cuts_at_a_word():
    assert capture._cut_words("  a  b   c ", 10) == "a b c"
    assert capture._cut_words("one two three four", 9) == "one two…"
    assert capture._cut_words("", 5) == ""


# The first bench's 40 prompts: the regex tier alone at the raw 0.45 cut.
DECISION_PROMPTS = [
    ("let's use PostgreSQL instead of SQLite for the database", True),
    ("switch to TypeScript for the frontend components", True),
    ("from now on, always use strict mode in TypeScript files", True),
    ("don't use var anymore, use const and let instead", True),
    ("please replace the old auth middleware with passport.js", True),
    ("we should adopt the repository pattern for data access", True),
    ("going forward prefer composition over inheritance", True),
    ("I want to rewrite the API layer in Go instead of Python", True),
    ("stop using console.log for debugging, use the logger", True),
    ("just use redis for caching, it is simpler", True),
    ("get rid of the old jQuery code and use vanilla JS", True),
    ("never import from the internal package directly", True),
    ("we should migrate from REST to GraphQL for the API", True),
    ("always validate user input at the API boundary", True),
    ("drop Python 3.8 support, only support 3.10+", True),
    ("use dependency injection for all service classes", True),
    ("keep the monorepo structure, don't split into separate repos", True),
    ("refactor the auth module to use the strategy pattern", True),
    ("avoid using any in TypeScript, use proper types", True),
    ("replace moment.js with date-fns for date handling", True),
    ("fix the bug in auth.py", False),
    ("what does this function do?", False),
    ("can you explain how the router works?", False),
    ("hello", False),
    ("looks good, ship it", False),
    ("should we use Redis or Memcached?", False),
    ("how about we try a different approach?", False),
    ("maybe we could use GraphQL instead?", False),
    ("what if we switched to MongoDB?", False),
    ("/commit", False),
    ("run the tests please", False),
    ("check the error logs for me", False),
    ("I'm getting a TypeError on line 42", False),
    ("the CI pipeline is broken again", False),
    ("nice work on the refactor", False),
    ("can you review this pull request?", False),
    ("where is the config file located?", False),
    ("how do I set up the dev environment?", False),
    ("what are the dependencies for this project?", False),
    ("could you clean up the imports in this file?", False),
]


def test_the_regex_tier_alone_is_precise():
    """Measured 2026-10-04: recall 0.60, precision 1.00 at 0.45. The regex
    tier is the fallback; the shared capture below is what stores."""
    pos = [p for p, d in DECISION_PROMPTS if d]
    neg = [p for p, d in DECISION_PROMPTS if not d]
    tp = sum(1 for p in pos if capture._score_decision_intent(p)[0] >= 0.45)
    fp = sum(1 for p in neg if capture._score_decision_intent(p)[0] >= 0.45)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / len(pos)
    assert precision >= 0.95 and recall >= 0.55, (precision, recall)


# ---------------------------------------------------------------------------
# The shared capture
# ---------------------------------------------------------------------------


def test_one_capture_rule(regex_only):
    kept = capture.capture_decision("let's use postgres instead of sqlite for the main store")
    assert kept.startswith("let's use postgres instead of sqlite")
    assert capture.capture_decision("what shell is still running?") == ""
    assert capture.capture_decision("ok looks good, thanks") == ""
    assert capture.capture_decision("(from user) x") == ""
    assert capture.capture_decision("") == "" and capture.capture_decision(None) == ""  # type: ignore[arg-type]
    # The sentence that decided is stored, not the prompt.
    kept = capture.capture_decision("The parser tests are green now. From now on always use the registry for every alias lookup.")
    assert kept == "From now on always use the registry for every alias lookup"
    # Below the threshold nothing is stored, whatever the gate would say.
    assert capture.capture_decision("The parser tests are green now. Let's use the registry for every alias lookup.") == ""


def test_the_semantic_score_wins_when_higher_and_still_passes_the_gate(monkeypatch):
    monkeypatch.setattr(capture, "score_decision_semantic", lambda text, server_only=False: (0.95, "we should keep the alias table in one module"))
    assert capture.capture_decision("hmm, so we should keep the alias table in one module") == "we should keep the alias table in one module"
    monkeypatch.setattr(capture, "score_decision_semantic", lambda text, server_only=False: (0.95, "what about the alias table?"))
    assert capture.capture_decision("hmm, what about the alias table here?") == "", "a high score never skips the gate"


def test_a_long_capture_is_cut_at_a_word(monkeypatch):
    long = "from now on always " + "validate the input " * 30
    monkeypatch.setattr(capture, "score_decision_semantic", lambda text, server_only=False: (0.9, long))
    out = capture.capture_decision(long)
    assert out and len(out) <= capture.CAPTURE_MAX_CHARS + 1 and out.endswith("…") and not out[:-1].endswith(" ")
    # The regex tier's own extraction is bounded to one clause.
    monkeypatch.setattr(capture, "score_decision_semantic", lambda text, server_only=False: (0.0, ""))
    assert len(capture.capture_decision(long)) <= 140


def test_no_daemon_means_no_semantic_score():
    assert capture.score_decision_semantic("let's use PostgreSQL instead of SQLite") == (0.0, "")
    assert capture.score_decision_semantic("short") == (0.0, "")


def test_the_template_cache_is_read_when_its_signature_matches(tmp_path: Path, monkeypatch):
    from windvane.semantic.config import embed_signature

    cache_file = tmp_path / "embeddings" / "decision_templates.json"
    cache_file.parent.mkdir(parents=True)
    monkeypatch.setattr(capture, "_TEMPLATE_CACHE", cache_file)
    monkeypatch.setattr(capture, "_CACHE_DIR", cache_file.parent)
    good = {"model": embed_signature(), "decision_count": len(capture.DECISION_TEMPLATES),
            "non_decision_count": len(capture.NON_DECISION_TEMPLATES), "decision_embeddings": [[1.0]], "non_decision_embeddings": [[0.0]]}
    cache_file.write_text(json.dumps(good), encoding="utf-8")
    assert capture._get_or_build_template_cache() == good
    # A cache from another model rebuilds; without the semantic extra that is None.
    cache_file.write_text(json.dumps(dict(good, model="some-other-model")), encoding="utf-8")
    monkeypatch.setattr(capture, "_try_import_sentence_transformers", lambda: None)
    assert capture._get_or_build_template_cache() is None
    assert capture.build_template_cache() is False


def test_the_cache_lives_in_the_store():
    from windvane.config import store_dir

    assert capture._resolve_cache_dir() == store_dir() / "embeddings"


def test_the_names_the_daemon_reads():
    assert capture.DECISION_THRESHOLD == 0.45 and capture.AMBIGUITY_MARGIN == 0.025
    assert capture.CAPTURE_THRESHOLD == 0.6


@pytest.mark.skip(reason="needs the windvane daemon with the semantic tier loaded; the corpus floors below run regex-only")
def test_the_semantic_tier_on_the_corpus():
    pass


# ---------------------------------------------------------------------------
# The neutral corpus: (prompt, is_decision, category)
# ---------------------------------------------------------------------------

CORPUS = [
    ("let's use PostgreSQL instead of SQLite for the database", True, "clear_switch"),
    ("switch to TypeScript for the frontend", True, "clear_switch"),
    ("let's use pnpm instead of yarn for package management", True, "clear_switch"),
    ("switch to Vitest instead of Jest for the test runner", True, "clear_switch"),
    ("let's move to FastAPI instead of Flask", True, "clear_switch"),
    ("replace moment.js with date-fns for date handling", True, "clear_switch"),
    ("swap the YAML config for TOML", True, "clear_switch"),
    ("migrate from REST to GraphQL for the API", True, "clear_switch"),
    ("convert the class components to hooks", True, "clear_switch"),
    ("upgrade from Node 16 to Node 20", True, "clear_switch"),
    ("move to Docker Compose instead of manual scripts", True, "clear_switch"),
    ("change to using async/await instead of callbacks", True, "clear_switch"),
    ("rewrite the CLI in Rust instead of Python", True, "clear_switch"),
    ("let's go with SQLAlchemy over raw SQL", True, "clear_switch"),
    ("switch the cache from Redis to Memcached", True, "clear_switch"),
    ("adopt Tailwind instead of CSS modules", True, "clear_switch"),
    ("replace the custom ORM with Prisma", True, "clear_switch"),
    ("let's use uv instead of pip for dependency management", True, "clear_switch"),
    ("migrate the test suite from unittest to pytest", True, "clear_switch"),
    ("switch to using pathlib instead of os.path", True, "clear_switch"),
    ("from now on always use strict mode in TypeScript files", True, "convention"),
    ("going forward prefer composition over inheritance", True, "convention"),
    ("always validate user input at the API boundary", True, "convention"),
    ("from now on use named exports in TypeScript", True, "convention"),
    ("going forward prefer early returns over nested ifs", True, "convention"),
    ("always add type hints to public functions", True, "convention"),
    ("from now on every PR needs a test", True, "convention"),
    ("always use structured logging, not print statements", True, "convention"),
    ("going forward all errors must include a stack trace", True, "convention"),
    ("always wrap database calls in transactions", True, "convention"),
    ("from now on use dataclasses instead of plain dicts", True, "convention"),
    ("always pin dependency versions in requirements.txt", True, "convention"),
    ("going forward put business logic in services not routes", True, "convention"),
    ("always use UTC for timestamps", True, "convention"),
    ("from now on commit messages follow conventional commits", True, "convention"),
    ("always use constants instead of magic numbers", True, "convention"),
    ("going forward all configs should come from environment variables", True, "convention"),
    ("always write docstrings for public APIs", True, "convention"),
    ("from now on use snake_case for Python file names", True, "convention"),
    ("always check return values, don't ignore errors", True, "convention"),
    ("don't use var anymore, use const and let instead", True, "negation"),
    ("stop using console.log for debugging, use the logger", True, "negation"),
    ("never import from the internal package directly", True, "negation"),
    ("avoid using any in TypeScript, use proper types", True, "negation"),
    ("don't use global state, pass dependencies explicitly", True, "negation"),
    ("stop doing inline SQL, use the query builder", True, "negation"),
    ("never commit secrets to the repo", True, "negation"),
    ("don't use sleep in tests, use proper waits", True, "negation"),
    ("avoid mutable default arguments in Python", True, "negation"),
    ("stop importing everything with wildcard imports", True, "negation"),
    ("don't use bare except clauses", True, "negation"),
    ("never hardcode URLs, use config", True, "negation"),
    ("stop using string concatenation for SQL", True, "negation"),
    ("don't add type: ignore without a comment explaining why", True, "negation"),
    ("avoid circular imports by restructuring", True, "negation"),
    ("never use eval() on user input", True, "negation"),
    ("don't catch exceptions and silently pass", True, "negation"),
    ("stop using os.system, use subprocess", True, "negation"),
    ("get rid of the old jQuery code and use vanilla JS", True, "negation"),
    ("remove the deprecated API endpoints", True, "negation"),
    ("use the repository pattern for data access", True, "architecture"),
    ("implement event sourcing for the order service", True, "architecture"),
    ("use the saga pattern for the checkout flow", True, "architecture"),
    ("implement CQRS for the reporting module", True, "architecture"),
    ("use dependency injection for all service classes", True, "architecture"),
    ("implement a message queue between the services", True, "architecture"),
    ("use the adapter pattern for third-party integrations", True, "architecture"),
    ("refactor the auth module to use the strategy pattern", True, "architecture"),
    ("keep the monorepo structure, don't split into separate repos", True, "architecture"),
    ("implement a circuit breaker for external API calls", True, "architecture"),
    ("use the mediator pattern for component communication", True, "architecture"),
    ("implement caching at the service layer not the route", True, "architecture"),
    ("use a factory for creating database connections", True, "architecture"),
    ("implement the outbox pattern for reliable messaging", True, "architecture"),
    ("use a gateway API instead of direct service calls", True, "architecture"),
    ("the API should return 404 not 400 for missing resources", True, "implicit"),
    ("errors go to stderr, not stdout", True, "implicit"),
    ("the default timeout should be 30 seconds", True, "implicit"),
    ("passwords need at least 12 characters", True, "implicit"),
    ("retries should use exponential backoff", True, "implicit"),
    ("the batch size should be 1000 rows", True, "implicit"),
    ("the log format should be JSON", True, "implicit"),
    ("only admin users can delete records", True, "implicit"),
    ("the response needs to include pagination metadata", True, "implicit"),
    ("rate limit at 100 requests per minute per user", True, "implicit"),
    ("cache TTL should be 5 minutes for user data", True, "implicit"),
    ("the health check endpoint should be at /health", True, "implicit"),
    ("connection pool size should be 20", True, "implicit"),
    ("all timestamps in the API should be ISO 8601", True, "implicit"),
    ("keep the token expiry at 1 hour", True, "implicit"),
    ("I've been looking at the performance and the current approach is too slow. Replace the N+1 queries with a batch loader.", True, "multi_sentence"),
    ("After thinking about it, I want to use Redis for caching. The in-memory approach won't scale.", True, "multi_sentence"),
    ("The team discussed it yesterday. Let's go with microservices for the payment module.", True, "multi_sentence"),
    ("I read the Flask vs FastAPI comparison. Switch to FastAPI for the new endpoints.", True, "multi_sentence"),
    ("SQLite is fine for dev but production needs Postgres. Use PostgreSQL going forward.", True, "multi_sentence"),
    ("The old auth is a security risk. Rewrite it using the passport.js middleware.", True, "multi_sentence"),
    ("We've been fighting with webpack for weeks. Move to Vite for the build system.", True, "multi_sentence"),
    ("After benchmarking both options, adopt Rust for the hot path. Python stays for orchestration.", True, "multi_sentence"),
    ("The monolith is getting unmaintainable. Let's split the user service out first.", True, "multi_sentence"),
    ("I checked with ops and they can support it. Use Kubernetes for the deployment.", True, "multi_sentence"),
    ("The current setup leaks memory under load. Replace the custom pool with pgbouncer.", True, "multi_sentence"),
    ("Tests are too slow at 15 minutes. Switch to parallel test execution with pytest-xdist.", True, "multi_sentence"),
    ("The REST API is getting bloated with versions. Move to GraphQL for the mobile clients.", True, "multi_sentence"),
    ("Docker images are 2GB. Use multi-stage builds to get them under 200MB.", True, "multi_sentence"),
    ("After reviewing the options, implement OpenTelemetry for distributed tracing.", True, "multi_sentence"),
    ("use postgres", True, "edge_case"),
    ("nuke the old cache layer and use Redis", True, "edge_case"),
    ("just yeet the jQuery and go native", True, "edge_case"),
    ("please use `pydantic` for all models", True, "edge_case"),
    ("use:\n- FastAPI for routes\n- SQLAlchemy for ORM\n- Alembic for migrations", True, "edge_case"),
    ("I want you to use black for formatting", True, "edge_case"),
    ("go ahead and use ruff instead of flake8", True, "edge_case"),
    ("let's just stick with the current database", True, "edge_case"),
    ("drop Python 3.8 support, only support 3.10+", True, "edge_case"),
    ("please adopt conventional commits for this repo", True, "edge_case"),
    ("we're going with Option B from the RFC", True, "edge_case"),
    ("lock in React 18 for the frontend stack", True, "edge_case"),
    ("pick Celery over RQ for the task queue", True, "edge_case"),
    ("commit to using TypeScript for all new files", True, "edge_case"),
    ("prefer functional components, no more class components", True, "edge_case"),
    ("fix the bug in auth.py", False, "task"),
    ("add error handling to the parser", False, "task"),
    ("refactor this function to be shorter", False, "task"),
    ("write a test for the login endpoint", False, "task"),
    ("update the documentation for the API", False, "task"),
    ("delete the unused imports", False, "task"),
    ("clean up the TODO comments", False, "task"),
    ("add logging to the payment service", False, "task"),
    ("create a migration for the new column", False, "task"),
    ("move this file to the utils directory", False, "task"),
    ("rename this variable to something clearer", False, "task"),
    ("add a retry mechanism to the API client", False, "task"),
    ("split this file into smaller modules", False, "task"),
    ("increase the test coverage for auth", False, "task"),
    ("update the error messages to be more helpful", False, "task"),
    ("what does this function do?", False, "question"),
    ("should we use Redis or Memcached?", False, "question"),
    ("how does the auth middleware work?", False, "question"),
    ("where is the config file located?", False, "question"),
    ("why is this test failing?", False, "question"),
    ("can you explain the caching strategy?", False, "question"),
    ("what's the best way to handle this error?", False, "question"),
    ("is there a rate limiter in place?", False, "question"),
    ("how do I set up the dev environment?", False, "question"),
    ("what are the dependencies for this project?", False, "question"),
    ("which database does this service use?", False, "question"),
    ("do we have monitoring set up?", False, "question"),
    ("is this the right approach?", False, "question"),
    ("would GraphQL be better here?", False, "question"),
    ("are there any known issues with this library?", False, "question"),
    ("how about we try a different approach?", False, "exploratory"),
    ("maybe we could use GraphQL instead?", False, "exploratory"),
    ("what if we switched to MongoDB?", False, "exploratory"),
    ("I wonder if Redis would be faster here", False, "exploratory"),
    ("it might be worth trying a different approach", False, "exploratory"),
    ("we could potentially use WebSockets for this", False, "exploratory"),
    ("I'm thinking about whether to use Rust for this", False, "exploratory"),
    ("perhaps a queue would help with the load", False, "exploratory"),
    ("what do you think about using gRPC?", False, "exploratory"),
    ("we should think about whether to split this", False, "exploratory"),
    ("it's worth considering a cache layer", False, "exploratory"),
    ("have you considered using Kafka?", False, "exploratory"),
    ("not sure if we need a service mesh yet", False, "exploratory"),
    ("I'm torn between Redis and Memcached", False, "exploratory"),
    ("there are a few options we could explore", False, "exploratory"),
    ("looks good, ship it", False, "praise_status"),
    ("nice work on the refactor", False, "praise_status"),
    ("the CI pipeline is green", False, "praise_status"),
    ("all tests pass now", False, "praise_status"),
    ("the deploy went smoothly", False, "praise_status"),
    ("great catch on that bug", False, "praise_status"),
    ("LGTM, merge when ready", False, "praise_status"),
    ("the performance numbers look good", False, "praise_status"),
    ("that fixed the memory leak", False, "praise_status"),
    ("everything is working in staging", False, "praise_status"),
    ("approved, go ahead with steps 1 to 3 and hold step 4", False, "approval_task"),
    ("looks good, ship the first two and leave the third for tomorrow", False, "approval_task"),
    ("ok, do the migration first and then the tests", False, "approval_task"),
    ("yes, but just the backend half in this pass", False, "approval_task"),
    ("fine, take the second option and skip the cleanup", False, "approval_task"),
    ("sounds good, start with the parser and report back", False, "approval_task"),
    ("all recommendations accepted, don't touch the deploy script yet", False, "approval_task"),
    ("go ahead with everything except the rename", False, "approval_task"),
    ("ok run it, but not on the shared box", False, "approval_task"),
    ("approved. leave item five for later", False, "approval_task"),
    ("the caching plan is approved", False, "approval_verdict"),
    ("approve the three fixes and the rename", False, "approval_verdict"),
    ("both proposals approved", False, "approval_verdict"),
    ("approved as written", False, "approval_verdict"),
    ("plan accepted, no changes", False, "approval_verdict"),
    ("the second draft is approved", False, "approval_verdict"),
    ("green light on the parser rewrite", False, "approval_verdict"),
    ("sign-off on the schema change", False, "approval_verdict"),
    ("approve all four items", False, "approval_verdict"),
    ("the fix set is approved, proceed", False, "approval_verdict"),
    ("I think we should probably use the queue here", False, "hedge"),
    ("leaning toward postgres but open to it", False, "hedge"),
    ("either works for me, up to you", False, "hedge"),
    ("I'd guess the lockfile is the issue", False, "hedge"),
    ("no strong opinion, redis is fine I suppose", False, "hedge"),
    ("we could go with sqlite for now, or not", False, "hedge"),
    ("maybe keep the old parser, not sure yet", False, "hedge"),
    ("I guess the smaller model would do", False, "hedge"),
    ("possibly the retry is what we want here", False, "hedge"),
    ("if you think so, use the cache layer", False, "hedge"),
    ("would it be better to split the module", False, "unmarked_question"),
    ("what is the right default for the timeout", False, "unmarked_question"),
    ("is the cache still needed after the rewrite", False, "unmarked_question"),
    ("do we still want the retry on the client", False, "unmarked_question"),
    ("should the parser keep the old flag", False, "unmarked_question"),
    ("any reason not to use the queue here", False, "unmarked_question"),
    ("which one do you prefer for the store", False, "unmarked_question"),
    ("how would you handle the retry instead", False, "unmarked_question"),
    ("does the daemon always need the lock", False, "unmarked_question"),
    ("are we keeping the legacy endpoint", False, "unmarked_question"),
    ("/commit", False, "command"),
    ("run the tests please", False, "command"),
    ("git push origin main", False, "command"),
    ("deploy to staging", False, "command"),
    ("show me the git log", False, "command"),
    ("build the docker image", False, "command"),
    ("restart the server", False, "command"),
    ("check the error logs", False, "command"),
    ("revert the last commit", False, "command"),
    ("open a PR for this", False, "command"),
    ("I'm getting a TypeError on line 42", False, "bug_report"),
    ("the API returns 500 when the body is empty", False, "bug_report"),
    ("there's a memory leak in the connection pool", False, "bug_report"),
    ("the import fails with ModuleNotFoundError", False, "bug_report"),
    ("users are seeing stale data after updates", False, "bug_report"),
    ("the response time jumped to 3 seconds", False, "bug_report"),
    ("the migration is timing out on large tables", False, "bug_report"),
    ("the login page crashes on mobile", False, "bug_report"),
    ("there's a race condition in the queue processor", False, "bug_report"),
    ("the websocket connection drops after 30 seconds", False, "bug_report"),
    ("the test is flaky, passes sometimes fails sometimes", False, "bug_report"),
    ("CSS is broken on the checkout page", False, "bug_report"),
    ("the cron job didn't run last night", False, "bug_report"),
    ("the search endpoint returns wrong results", False, "bug_report"),
    ("I noticed the disk usage is at 90%", False, "bug_report"),
    ("I used PostgreSQL for the last project", False, "ambiguous"),
    ("we should think about whether to use Redis", False, "ambiguous"),
    ("can you switch to the other branch?", False, "ambiguous"),
    ("I prefer Python for scripting personally", False, "ambiguous"),
    ("some teams use microservices for this", False, "ambiguous"),
    ("the old code used to use Flask", False, "ambiguous"),
    ("Redis is faster than Memcached for this use case", False, "ambiguous"),
    ("TypeScript has better tooling than JavaScript", False, "ambiguous"),
    ("we talked about using GraphQL last week", False, "ambiguous"),
    ("the docs recommend using async/await", False, "ambiguous"),
    ("some people prefer tabs over spaces", False, "ambiguous"),
    ("I heard good things about Bun", False, "ambiguous"),
    ("the benchmarks show Rust is faster", False, "ambiguous"),
    ("most projects use Docker these days", False, "ambiguous"),
    ("the industry is moving toward serverless", False, "ambiguous"),
    ("we were going to use Kafka but ran out of time", False, "ambiguous"),
    ("I tried using Redis but it was overkill", False, "ambiguous"),
    ("React is the most popular framework", False, "ambiguous"),
    ("our competitors use GraphQL", False, "ambiguous"),
    ("the previous team chose MongoDB for this", False, "ambiguous"),
]


def _prf(fn, pos, neg):
    tp = sum(1 for p in pos if fn(p))
    fp = sum(1 for p in neg if fn(p))
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / len(pos) if pos else 0.0
    return prec, rec


def test_the_corpus_is_what_the_floors_were_measured_on():
    assert len(CORPUS) == 260
    assert sum(1 for _p, d, _c in CORPUS if d) == 120
    assert sum(1 for _p, d, c in CORPUS if d and c in ("negation", "convention")) == 40


NEG = [p for p, d, _c in CORPUS if not d]
CORRECTION_POS = [p for p, d, c in CORPUS if d and c in ("negation", "convention")]
DECISION_POS = [p for p, d, _c in CORPUS if d]


def test_the_correction_gate_on_the_corpus():
    """Measured 2026-10-04: precision 0.97, recall 0.80."""
    prec, rec = _prf(looks_like_correction, CORRECTION_POS, NEG)
    assert prec >= 0.85 and rec >= 0.75, (prec, rec)


def test_the_decision_gate_on_the_corpus():
    """Measured 2026-10-04: precision 1.00, recall 0.93."""
    prec, rec = _prf(looks_like_decision, DECISION_POS, NEG)
    assert prec >= 0.90 and rec >= 0.90, (prec, rec)


def test_the_shared_capture_on_the_corrections_regex_only(regex_only):
    """What the prompt hook and the miner's preference path both store
    through. Measured 2026-10-04, regex only: precision 1.00, recall 0.95."""
    prec, rec = _prf(lambda t: bool(capture.capture_decision(t, server_only=True)), CORRECTION_POS, NEG)
    assert prec >= 0.90 and rec >= 0.85, (prec, rec)

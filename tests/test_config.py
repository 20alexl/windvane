"""The configuration layers (windvane.config), then the location and read-side
helpers every hook leans on: paths, storage, hot_reader, precheck, proc_lock."""

import json
import os
import time
from pathlib import Path

import pytest

from windvane import config


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Never the real ~/.windvane or ~/.claude: both point into tmp_path, no
    daemon starts, and no WINDVANE_<KNOB> from the caller's shell leaks in."""
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("WINDVANE_NO_DAEMON", "1")
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    for key in config.KNOBS:
        monkeypatch.delenv(config.ENV_PREFIX + key.upper(), raising=False)
    from windvane import paths

    paths._project_dir_cache.clear()
    paths._non_project_cache = None
    yield
    paths._project_dir_cache.clear()
    paths._non_project_cache = None


def _write_json(path: Path, data) -> None:
    """Write JSON and move the mtime forward, so the mtime-keyed cache always
    sees a rewrite (two writes inside one clock tick share a stamp)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    old = path.stat().st_mtime_ns if existed else 0
    path.write_bytes(json.dumps(data).encode("utf-8"))
    if existed:
        bump = max(old, path.stat().st_mtime_ns) + 10_000_000
        os.utime(path, ns=(bump, bump))


def _store(tmp_path) -> Path:
    return tmp_path / "store"


def _settings(tmp_path) -> Path:
    return tmp_path / "claude" / "settings.json"


# ── windvane.config: the layers ─────────────────────────────────────────────


def test_every_layer_in_order_env_project_plugin_user_default(tmp_path, monkeypatch):
    proj = tmp_path / "app"
    proj.mkdir()
    assert config.knob("strike_cap", str(proj)) == 3  # the KNOBS default

    _write_json(_store(tmp_path) / "config.json", {"strike_cap": 4})
    assert config.knob("strike_cap", str(proj)) == 4  # user file

    _write_json(_settings(tmp_path), {"pluginConfigs": {"windvane@market": {"options": {"strike_cap": 5}}}})
    assert config.knob("strike_cap", str(proj)) == 5  # plugin userConfig over the user file

    _write_json(proj / ".windvane" / "config.json", {"strike_cap": 6})
    assert config.knob("strike_cap", str(proj)) == 6  # project file over the plugin

    monkeypatch.setenv("WINDVANE_STRIKE_CAP", "7")
    assert config.knob("strike_cap", str(proj)) == 7  # the environment over everything

    # Without a project dir the project file is not a layer.
    monkeypatch.delenv("WINDVANE_STRIKE_CAP")
    assert config.knob("strike_cap") == 5


def test_a_blank_value_in_a_layer_falls_through(tmp_path, monkeypatch):
    proj = tmp_path / "app"
    _write_json(_store(tmp_path) / "config.json", {"alert_command": "notify {message}"})
    _write_json(proj / ".windvane" / "config.json", {"alert_command": "", "strike_cap": None})
    assert config.knob("alert_command", str(proj)) == "notify {message}"
    assert config.knob("strike_cap", str(proj)) == 3
    monkeypatch.setenv("WINDVANE_ALERT_COMMAND", "   ")  # whitespace is not a value
    assert config.knob("alert_command", str(proj)) == "notify {message}"


def test_plugin_config_reads_flat_or_under_options_and_only_a_windvane_key(tmp_path):
    _write_json(_settings(tmp_path), {"pluginConfigs": {"windvane": {"goal_turn_cap": 40}}})
    assert config.plugin_config() == {"goal_turn_cap": 40}
    assert config.knob("goal_turn_cap") == 40

    _write_json(_settings(tmp_path), {"pluginConfigs": {"windvane@local": {"options": {"goal_turn_cap": 41}}}})
    assert config.plugin_config() == {"goal_turn_cap": 41}
    assert config.knob("goal_turn_cap") == 41

    _write_json(_settings(tmp_path), {"pluginConfigs": {"other-plugin": {"goal_turn_cap": 9}}})
    assert config.plugin_config() == {}
    assert config.knob("goal_turn_cap") == 150

    _write_json(_settings(tmp_path), {"pluginConfigs": "not a dict"})
    assert config.plugin_config() == {}


def test_a_missing_or_broken_layer_is_skipped(tmp_path):
    (tmp_path / "claude").mkdir()
    _settings(tmp_path).write_bytes(b"{not json")
    (_store(tmp_path)).mkdir()
    (_store(tmp_path) / "config.json").write_bytes(b"[1, 2, 3]")  # JSON, but not an object
    assert config.plugin_config() == {}
    assert config.user_config() == {}
    assert config.knob("strike_cap") == 3
    assert config.load()["stall_turns"] == 3


def test_an_edited_file_is_seen_without_a_restart(tmp_path):
    cfg = _store(tmp_path) / "config.json"
    _write_json(cfg, {"stall_turns": 4})
    assert config.knob("stall_turns") == 4
    _write_json(cfg, {"stall_turns": 8})
    assert config.knob("stall_turns") == 8


def test_project_config_merges_the_project_file_over_the_user_file(tmp_path):
    proj = tmp_path / "app"
    _write_json(_store(tmp_path) / "config.json", {"a": 1, "b": 1})
    _write_json(proj / ".windvane" / "config.json", {"b": 2})
    assert config.project_config(str(proj)) == {"a": 1, "b": 2}
    assert config.project_config() == {"a": 1, "b": 1}
    assert config.config_path(str(proj)) == proj / ".windvane" / "config.json"


# ── coercion ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, want",
    [("1", True), ("yes", True), ("ON", True), ("true", True), ("0", False), ("no", False), ("off", False), ("maybe", None)],
)
def test_bool_coercion(monkeypatch, raw, want):
    monkeypatch.setenv("WINDVANE_COMPLIANCE", raw)
    got = config.knob("compliance")
    assert got is (config.DEFAULTS["compliance"] if want is None else want)


def test_bool_from_a_file_keeps_a_real_bool(tmp_path):
    _write_json(_store(tmp_path) / "config.json", {"structure": True, "compliance": "off"})
    assert config.knob("structure") is True
    assert config.knob("compliance") is False


def test_int_float_and_str_coercion(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDVANE_GOAL_TURN_CAP", "12")
    assert config.knob("goal_turn_cap") == 12
    monkeypatch.setenv("WINDVANE_GOAL_TURN_CAP", "12.9")
    assert config.knob("goal_turn_cap") == 12  # int(float(raw))
    monkeypatch.setenv("WINDVANE_GOAL_TURN_CAP", "lots")
    assert config.knob("goal_turn_cap") == 150  # will not coerce: the default

    monkeypatch.setenv("WINDVANE_HEADSUP_FRACTION", "0.25")
    assert config.knob("headsup_fraction") == 0.25
    monkeypatch.setenv("WINDVANE_HEADSUP_FRACTION", "x")
    assert config.knob("headsup_fraction") == 0.10

    monkeypatch.setenv("WINDVANE_CHECKPOINT_MARGIN", "15000")  # computed default (None), read as an int
    assert config.knob("checkpoint_margin") == 15000
    monkeypatch.delenv("WINDVANE_CHECKPOINT_MARGIN")
    assert config.knob("checkpoint_margin") is None

    _write_json(_store(tmp_path) / "config.json", {"non_project_dirs": [".scratch", "tmp"], "alert_command": 42})
    assert config.knob("non_project_dirs") == ".scratch,tmp"  # a list joins into the comma string
    assert config.knob("alert_command") == "42"


def test_an_unknown_key_reads_the_layers_raw(tmp_path, monkeypatch):
    assert config.knob("no_such_key") is None
    _write_json(_store(tmp_path) / "config.json", {"no_such_key": [1, 2]})
    assert config.knob("no_such_key") == [1, 2]
    monkeypatch.setenv("WINDVANE_NO_SUCH_KEY", "raw")
    assert config.knob("no_such_key") == "raw"


def test_knob_int_and_knob_float_fall_back(monkeypatch):
    assert config.knob_int("strike_cap") == 3
    assert config.knob_int("no_such_key", default=9) == 9
    assert config.knob_int("no_such_key") == 0
    assert config.knob_float("headsup_fraction") == 0.10
    assert config.knob_float("no_such_key", default=0.5) == 0.5


def test_structure_defaults_to_false():
    assert config.DEFAULTS["structure"] is False
    assert config.knob("structure") is False
    assert config.load()["structure"] is False


# ── locations ───────────────────────────────────────────────────────────────


def test_store_dir_honours_windvane_dir(tmp_path, monkeypatch):
    assert config.store_dir() == tmp_path / "store"
    monkeypatch.setenv("WINDVANE_DIR", "  ")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert config.store_dir() == tmp_path / "home" / ".windvane"
    monkeypatch.delenv("WINDVANE_DIR")
    assert config.store_dir() == tmp_path / "home" / ".windvane"


def test_settings_path_follows_claude_config_dir(tmp_path, monkeypatch):
    assert config._settings_path() == tmp_path / "claude" / "settings.json"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert config._settings_path() == tmp_path / "home" / ".claude" / "settings.json"


# ── load, enabled, autonomy_on ──────────────────────────────────────────────


def test_load_resolves_every_knob_and_clamps_the_goal_cap(tmp_path, monkeypatch):
    cfg = config.load()
    assert set(cfg) == set(config.KNOBS)
    assert cfg == {**config.DEFAULTS, "goal_turn_cap": 150}
    monkeypatch.setenv("WINDVANE_GOAL_TURN_CAP", "0")
    assert config.load()["goal_turn_cap"] == 1
    monkeypatch.setenv("WINDVANE_GOAL_TURN_CAP", "-5")
    assert config.load()["goal_turn_cap"] == 1
    proj = tmp_path / "app"
    _write_json(proj / ".windvane" / "config.json", {"default_rules": False})
    monkeypatch.delenv("WINDVANE_GOAL_TURN_CAP")
    assert config.load(str(proj))["default_rules"] is False
    assert config.load()["default_rules"] is True


@pytest.mark.parametrize(
    "value, want",
    [(True, True), (False, False), (None, False), (0, False), (3, True),
     ("yes", True), ("off", False), ("FALSE", False), ("", False), ("  ", False), ("anything", True)],
)
def test_enabled(value, want):
    assert config.enabled({"k": value}, "k") is want


def test_enabled_on_a_missing_key_is_false():
    assert config.enabled({}, "k") is False


def test_autonomy_from_the_environment(monkeypatch):
    assert config.autonomy_on() is False
    monkeypatch.setenv("WINDVANE_AUTONOMY", "1")
    assert config.autonomy_on() is True
    monkeypatch.setenv("WINDVANE_AUTONOMY", "0")
    assert config.autonomy_on() is False


def test_autonomy_from_the_plugin_userconfig_or_a_file(tmp_path):
    _write_json(_settings(tmp_path), {"pluginConfigs": {"windvane@market": {"options": {"autonomy": True}}}})
    assert config.autonomy_on() is True
    _write_json(_settings(tmp_path), {})
    assert config.autonomy_on() is False
    proj = tmp_path / "app"
    _write_json(proj / ".windvane" / "config.json", {"autonomy": "yes"})
    assert config.autonomy_on(project_dir=str(proj)) is True
    assert config.autonomy_on() is False


def test_autonomy_with_no_state_or_a_broken_state_is_off():
    assert config.autonomy_on(None) is False
    assert config.autonomy_on({}) is False
    assert config.autonomy_on({"run": "not a dict"}) is False  # never raises


def test_autonomy_while_a_goal_runs():
    goal = pytest.importorskip("windvane.goal", reason="windvane.goal is not in the tree yet")
    running_states = [
        s for s in ({"run": {"auto": {"status": "running"}}}, {"goal": {"status": "running"}})
        if goal.running(s)
    ]
    if not running_states:
        pytest.skip("no known state shape reads as running for this windvane.goal")
    assert config.autonomy_on(running_states[0]) is True


def test_knobs_table_lists_every_key():
    table = config.knobs_table()
    for key in config.KNOBS:
        assert f"`{key}`" in table
    assert "| `checkpoint_margin` | computed |" in table


# ── paths ───────────────────────────────────────────────────────────────────


def test_storage_dir_and_project_memory_dir_follow_the_store(tmp_path):
    from windvane import paths

    store = _store(tmp_path)
    assert paths.get_windvane_storage_dir() == store
    assert paths.get_memory_file() == store / "memory.json"
    assert paths._global_handoff_dir() == store / "checkpoints"
    proj = tmp_path / "app"
    proj.mkdir()
    # Unregistered: the hash a registration would give it.
    import hashlib

    h = hashlib.md5(paths._normalize_path(str(proj)).encode()).hexdigest()[:8]
    assert paths.get_project_memory_dir(str(proj)) == store / "projects" / h
    assert paths._project_hash_dir(str(proj)) is None
    _write_json(store / "manifest.json", {"projects": {paths._normalize_path(str(proj)): {"hash": "abcd1234"}}})
    assert paths._get_manifest()["projects"]
    assert paths.get_project_memory_dir(str(proj)) == store / "projects" / "abcd1234"
    assert paths._project_hash_dir(str(proj)) == store / "projects" / "abcd1234"
    assert paths._project_hash_dir("") is None


def test_normalize_path_uses_forward_slashes_and_a_lowercase_drive(tmp_path):
    from windvane import paths

    n = paths._normalize_path(str(tmp_path / "a" / "b"))
    assert "\\" not in n
    if len(n) >= 2 and n[1] == ":":
        assert n[0].islower()


def test_handoff_dirs_are_own_then_descendants_then_ancestors(tmp_path):
    from windvane import paths

    store = _store(tmp_path)
    ws = tmp_path / "ws"
    a = ws / "app"
    deep = a / "sub"
    sib = ws / "other"
    for d in (deep, sib):
        d.mkdir(parents=True)
    n = paths._normalize_path
    _write_json(store / "manifest.json", {"projects": {
        n(str(ws)): {"hash": "h_ws"}, n(str(a)): {"hash": "h_a"},
        n(str(deep)): {"hash": "h_deep"}, n(str(sib)): {"hash": "h_sib"},
    }})
    got = [p.name for p in paths._handoff_candidate_dirs(str(a))]
    assert got == ["h_a", "h_deep", "h_ws"]  # never the sibling, never the global ring
    assert [p.name for p in paths._handoff_candidate_dirs(str(ws))] == ["h_ws", "h_a", "h_sib", "h_deep"]
    # Unregistered and nothing above it registered: the global ring only.
    assert paths._handoff_candidate_dirs(str(tmp_path / "elsewhere")) == [store / "checkpoints"]
    assert paths._handoff_candidate_dirs("") == [store / "checkpoints"]


def test_a_worktree_session_belongs_to_the_main_repo(tmp_path, monkeypatch):
    from windvane import paths

    # .scratch is one workspace's convention: configured, not shipped.
    monkeypatch.setenv("WINDVANE_NON_PROJECT_DIRS", ".scratch")
    ws = tmp_path / "ws"
    main = ws / "service-a"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    wt = main / ".scratch" / "wt"
    wt.mkdir(parents=True)
    (wt / ".git").write_bytes(f"gitdir: {main / '.git' / 'worktrees' / 'wt'}\n".encode("utf-8"))
    (wt / "plan.md").write_bytes(b"x")
    norm = paths._normalize_path
    assert paths.worktree_main(str(wt)) == norm(str(main))
    assert paths.worktree_main(str(main)) == ""  # a real .git dir, not a worktree
    assert paths.canonical_project_root(str(wt)) == norm(str(main))
    # A plain scratch dir (no .git) still maps to the project above it.
    assert paths.canonical_project_root(str(main / ".scratch" / "notes")) == norm(str(main))
    assert paths.canonical_project_root(str(main)) == norm(str(main))
    # A file inside the worktree resolves to the main repo, not the worktree.
    assert paths.resolve_project_for_file(str(wt / "plan.md"), str(ws)) == norm(str(main))
    assert paths.under_non_project_dir(str(wt / "plan.md")) is True
    assert paths.under_non_project_dir(str(main / "src" / "a.py")) is False


def test_a_relative_gitdir_worktree_resolves_too(tmp_path):
    from windvane import paths

    main = tmp_path / "app"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    wt = main / "node_modules" / "wt"  # a default non-project dir, no config needed
    wt.mkdir(parents=True)
    (wt / ".git").write_bytes(b"gitdir: ../../.git/worktrees/wt\n")
    assert paths.worktree_main(str(wt)) == paths._normalize_path(str(main))
    # A .git file that points nowhere git can see is left alone.
    stray = tmp_path / "stray"
    stray.mkdir()
    (stray / ".git").write_bytes(b"gitdir: ../nowhere/.git/worktrees/x\n")
    assert paths.worktree_main(str(stray)) == ""


def test_non_project_dirs_default_and_configured(tmp_path, monkeypatch):
    from windvane import paths

    assert paths.under_non_project_dir("/r/app/node_modules/pkg/index.js") is True
    assert paths.under_non_project_dir("/r/app/.venv/lib/x.py") is True
    assert paths.under_non_project_dir("/r/app/__pycache__/x.pyc") is True
    assert paths.under_non_project_dir("/r/app/.scratch/plan.md") is False  # not shipped
    assert paths.under_non_project_dir("/r/app/node_modules") is False  # the last segment is the file
    assert paths.under_non_project_dir("") is False

    # The store's config.json list, as the user file has always written it.
    _write_json(_store(tmp_path) / "config.json", {"non_project_dirs": [".scratch", " "]})
    assert paths.under_non_project_dir("/r/app/.scratch/plan.md") is True
    _write_json(_store(tmp_path) / "config.json", {})
    assert paths.under_non_project_dir("/r/app/.scratch/plan.md") is False

    # The plugin's userConfig row (a comma-separated string).
    _write_json(_settings(tmp_path), {"pluginConfigs": {"windvane": {"non_project_dirs": "tmp, .cache"}}})
    assert paths.under_non_project_dir("/r/app/.cache/x.py") is True
    assert paths.under_non_project_dir("/r/app/tmp/x.py") is True

    # The environment.
    _write_json(_settings(tmp_path), {})
    monkeypatch.setenv("WINDVANE_NON_PROJECT_DIRS", "build")
    assert paths.under_non_project_dir("/r/app/build/x.py") is True
    assert paths.under_non_project_dir("/r/app/tmp/x.py") is False


def test_resolve_project_for_file_takes_the_nearest_marker(tmp_path):
    from windvane import paths

    ws = tmp_path / "ws"
    app = ws / "app"
    (app / "src" / "deep").mkdir(parents=True)
    (app / "pyproject.toml").write_bytes(b"")
    (ws / "loose").mkdir()
    f = app / "src" / "deep" / "x.py"
    f.write_bytes(b"")
    n = paths._normalize_path
    assert paths.resolve_project_for_file(str(f), str(ws)) == n(str(app))
    assert paths._project_dir_cache[str(f)] == n(str(app))  # cached
    assert paths.resolve_project_for_file(str(ws / "loose" / "y.py"), str(ws)) == n(str(ws))
    assert paths.resolve_project_for_file(str(tmp_path / "outside.py"), str(ws)) == n(str(ws))
    assert paths.resolve_project_for_file("", str(ws)) == str(ws)


def test_get_project_dir_prefers_claude_project_dir(tmp_path, monkeypatch):
    from windvane import paths

    app = tmp_path / "app"
    (app / "src").mkdir(parents=True)
    (app / "package.json").write_bytes(b"{}")
    monkeypatch.chdir(tmp_path)
    n = paths._normalize_path
    assert paths.get_project_dir() == n(str(tmp_path))
    assert paths.get_project_dir(str(app / "src" / "a.ts")) == n(str(app))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(app))
    assert paths.get_project_dir(str(tmp_path / "x.py")) == n(str(app))


def _workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    ws = tmp_path / "ws"
    a, b = ws / "proj-a", ws / "proj-b"
    for p in (ws, a, b):
        (p / ".git").mkdir(parents=True, exist_ok=True)
    (a / "src").mkdir(exist_ok=True)
    (b / "src").mkdir(exist_ok=True)
    return ws, a, b


def test_mined_entries_file_under_the_project_their_files_name(tmp_path, monkeypatch):
    from windvane.paths import _normalize_path, target_project_for_files

    monkeypatch.setenv("WINDVANE_NON_PROJECT_DIRS", ".scratch")  # one workspace's convention, configured
    ws, a, b = _workspace(tmp_path)
    assert _normalize_path(target_project_for_files(str(ws), [str(a / "src" / "x.py")])) == _normalize_path(str(a))
    # majority wins; a temp path outside the root does not vote
    files = [str(b / "src" / "y.py"), str(b / "src" / "z.py"), str(a / "src" / "x.py"), r"C:\Temp\other\t.py"]
    assert _normalize_path(target_project_for_files(str(ws), files)) == _normalize_path(str(b))
    assert target_project_for_files(str(ws), []) == str(ws)
    assert target_project_for_files(str(ws), [r"C:\Temp\only.py"]) == str(ws)
    assert target_project_for_files(str(a), [str(a / "src" / "x.py")]) == str(a)  # already the project
    # The text names a project: it wins over the file vote, even a project
    # whose files were not named at all (a session that edited both).
    assert _normalize_path(target_project_for_files(str(ws), files, "MISTAKE: proj_a.core failed")) == _normalize_path(str(a))
    (ws / "service-a" / ".git").mkdir(parents=True, exist_ok=True)
    got = target_project_for_files(str(ws), [str(a / "src" / "x.py")], "AttributeError: module 'service_a.plant' has no attribute VERSION")
    assert _normalize_path(got) == _normalize_path(str(ws / "service-a"))
    # short or embedded names do not match ("tools" inside "toolset")
    (ws / "tools" / ".git").mkdir(parents=True, exist_ok=True)
    got = target_project_for_files(str(ws), [str(a / "src" / "x.py")], "the toolset broke; tools were fine")
    assert _normalize_path(got) == _normalize_path(str(a))

    # A git WORKTREE carries a `.git` FILE, which is a project marker; the
    # walk must not make it a destination.
    wt = a / ".scratch" / "wt"
    wt.mkdir(parents=True, exist_ok=True)
    (wt / ".git").write_bytes(b"gitdir: ../../../.git/worktrees/wt\n")
    known = [str(ws), str(a), str(b)]
    report = str(wt / "m-cut-report.md")
    assert _normalize_path(target_project_for_files(str(ws), [report], known_projects=known)) == _normalize_path(str(a))
    # ...and even without the known list, the walk is pulled back out of .scratch
    assert _normalize_path(target_project_for_files(str(ws), [report])) == _normalize_path(str(a))
    # a registered project that IS a worktree is never a destination
    assert _normalize_path(
        target_project_for_files(str(ws), [report], known_projects=known + [str(wt)])
    ) == _normalize_path(str(a))
    # a file under no known project does not vote at all
    (ws / "loose").mkdir(exist_ok=True)
    assert target_project_for_files(str(ws), [str(ws / "loose" / "x.py")], known_projects=known) == str(ws)
    # A RELATIVE path is no evidence: it resolves against the CALLING process's cwd.
    for rel in ("v2/tests/test_orders.py", "SUBMISSION.ts", r"src\README.md"):
        assert target_project_for_files(str(ws), [rel], known_projects=known) == str(ws)
        assert target_project_for_files(str(ws), [rel]) == str(ws)
    # one absolute file among relative ones still decides
    mixed = ["record/journal/score/x.jsonl", str(b / "src" / "y.py")]
    assert _normalize_path(target_project_for_files(str(ws), mixed, known_projects=known)) == _normalize_path(str(b))


def test_is_absolute_path_reads_windows_forms_on_any_platform():
    from windvane.paths import _is_absolute_path

    assert _is_absolute_path(r"C:\a\b.py")
    assert _is_absolute_path("d:/a/b.py")
    assert _is_absolute_path(r"\\server\share\x.py")
    assert _is_absolute_path("//server/share/x.py")
    assert not _is_absolute_path("src/a.py")
    assert not _is_absolute_path("   ")


def test_paths_keeps_the_names_other_modules_import():
    from windvane import paths

    for name in ("get_windvane_storage_dir", "_normalize_path", "resolve_project_for_file",
                 "target_project_for_files", "_get_manifest", "get_project_memory_dir",
                 "canonical_project_root", "_handoff_candidate_dirs", "_project_hash_dir",
                 "_global_handoff_dir", "get_memory_file", "get_project_dir",
                 "under_non_project_dir", "_PROJECT_MARKERS", "_project_dir_cache",
                 "_GENERIC_BASENAMES"):
        assert hasattr(paths, name), name
    from windvane import hot_reader

    assert paths._GENERIC_BASENAMES is hot_reader._GENERIC_BASENAMES


# ── storage ─────────────────────────────────────────────────────────────────


def test_past_mistakes_rank_this_project_first():
    from windvane import storage

    mem = {"entries": [
        {"id": "a", "category": "mistake", "content": "MISTAKE: newest, pooled, another project", "created_at": 400,
         "related_files": ["C:/ws/service-b/x.py"], "_inherited": True},
        {"id": "b", "category": "mistake", "content": "MISTAKE: pooled, no file", "created_at": 300, "_inherited": True},
        {"id": "c", "category": "mistake", "content": "MISTAKE: pooled but names a file here", "created_at": 200,
         "related_files": ["C:\\ws\\app\\pkg\\y.py"], "_inherited": True},
        {"id": "d", "category": "mistake", "content": "MISTAKE: oldest, the project's own", "created_at": 100},
        {"id": "e", "category": "mistake", "content": "acknowledged", "created_at": 500, "archived_at": 1},
    ]}
    ms = storage.get_past_mistakes(mem, "C:/ws/app")
    assert [m["id"] for m in ms] == ["c", "d", "b", "a"]
    assert [m["scope"] for m in ms] == [0, 0, 1, 2]
    assert ms[0]["content"] == "pooled but names a file here"  # the prefix is stripped
    assert [m["id"] for m in storage.get_past_mistakes(mem)] == ["a", "b", "c", "d"]  # no project: newest first


def test_rules_counts_and_the_claude_md_filter(tmp_path):
    from windvane import storage

    mem = {"entries": [
        {"id": "r1", "category": "rule", "content": "Never force push the main branch", "relevance": 9},
        {"id": "r2", "category": "rule", "content": "Prefer small commits with clear messages", "relevance": 6},
        {"id": "m1", "category": "mistake", "content": "x"},
        {"id": "d1", "category": "decision", "content": "y"},
    ]}
    assert storage.get_project_rules(mem) == [
        {"id": "r1", "content": "Never force push the main branch", "detector": None},
        {"id": "r2", "content": "Prefer small commits with clear messages", "detector": None},
    ]
    assert storage.get_memory_counts(mem) == {"rule": 2, "mistake": 1, "decision": 1, "total": 4}
    assert storage.get_memory_counts({}) == {"total": 0}

    hub = tmp_path / "hub"
    proj = hub / "app"
    proj.mkdir(parents=True)
    rules = storage.get_project_rules(mem)
    assert storage.filter_rules_in_claude_md(rules, str(proj)) == rules  # no CLAUDE.md
    (proj / "CLAUDE.md").write_bytes(b"# Rules\n- Never force push the main branch.\n")
    assert [r["id"] for r in storage.filter_rules_in_claude_md(rules, str(proj))] == ["r2"]
    # An ancestor's CLAUDE.md is loaded too (a hub's rules bind a spoke).
    (hub / "CLAUDE.md").write_bytes(b"- Prefer small commits with clear messages.\n")
    assert storage.filter_rules_in_claude_md(rules, str(proj)) == []
    # The "(Reason: ...)" tail is not compared: CLAUDE.md states rules without one.
    reasoned = [{"id": "r3", "content": "Never force push the main branch (Reason: a rewrite cost a day of history and some trust)"}]
    assert storage.filter_rules_in_claude_md(reasoned, str(proj)) == []


def _register(store: Path, projects: dict) -> None:
    """Write a manifest and each project's memory.json: {path: (hash, entries)}."""
    from windvane.paths import _normalize_path

    manifest = {"projects": {}}
    for path, (h, entries) in projects.items():
        manifest["projects"][_normalize_path(str(path))] = {"hash": h}
        _write_json(store / "projects" / h / "memory.json", {"project_name": Path(path).name, "entries": entries})
    _write_json(store / "manifest.json", manifest)


def test_load_project_memory_inherits_ancestors_and_marks_them(tmp_path):
    from windvane import storage

    ws = tmp_path / "ws"
    app = ws / "app"
    app.mkdir(parents=True)
    _register(_store(tmp_path), {
        ws: ("h_ws", [{"id": "w1", "category": "rule", "content": "workspace rule"},
                      {"id": "a1", "category": "rule", "content": "same id as the project's own"}]),
        app: ("h_app", [{"id": "a1", "category": "mistake", "content": "own"}]),
    })
    mem = storage.load_project_memory(str(app))
    by_id = {e["id"]: e for e in mem["entries"]}
    assert set(by_id) == {"a1", "w1"}
    assert by_id["a1"]["content"] == "own" and not by_id["a1"].get("_inherited")
    assert by_id["w1"]["_inherited"] is True
    # A child of a registered workspace with no store of its own: the ancestors only.
    sub = ws / "fresh"
    sub.mkdir()
    mem = storage.load_project_memory(str(sub))
    assert mem["project_name"] == "fresh"
    assert {e["id"] for e in mem["entries"]} == {"w1", "a1"}
    # Nothing registered on the path at all.
    assert storage.load_project_memory(str(tmp_path / "elsewhere")) == {}
    d = _store(tmp_path) / "projects" / "h_app"
    assert storage._load_project_entries_from_dir(d)[0]["id"] == "a1"
    assert storage._load_project_data_from_dir(d)["project_name"] == "app"
    assert storage._load_project_data_from_dir(tmp_path / "missing") == {}
    assert storage._load_project_entries_from_dir(tmp_path / "missing") == []


def test_load_project_memory_reads_the_legacy_single_file(tmp_path):
    from windvane import storage
    from windvane.paths import _normalize_path

    ws = tmp_path / "ws"
    app = ws / "app"
    app.mkdir(parents=True)
    _write_json(_store(tmp_path) / "memory.json", {"projects": {
        _normalize_path(str(ws)): {"entries": [{"id": "w"}]},
        _normalize_path(str(app)): {"entries": [{"id": "a"}]},
    }})
    assert [e["id"] for e in storage.load_project_memory(str(app))["entries"]] == ["a", "w"]


# ── hot_reader ──────────────────────────────────────────────────────────────


def test_generic_basenames_need_a_full_path():
    from windvane import hot_reader

    gate = 0.35 * 0.5  # a bare-name match would score 0.5 under the 0.35 file weight
    assert hot_reader._file_match_score("C:/ws/app/README.md", [], "FileNotFoundError: src/README.md") == 0.0
    assert hot_reader._file_match_score("C:/ws/app/CLAUDE.md", ["CLAUDE.md"], "") == 0.0
    assert hot_reader._file_match_score("C:/ws/app/README.md", ["C:/ws/app/README.md"], "") >= gate
    # A specific name still matches on its own; diverging paths never do.
    assert hot_reader._file_match_score("C:/ws/app/loader.py", ["loader.py"], "") == 0.5
    assert hot_reader._file_match_score(
        "C:/ws/service-a/pkg/__init__.py", ["C:/ws/service-b/pkg/__init__.py"], ""
    ) == 0.0
    assert hot_reader._file_match_score("C:/ws/app/pkg/a.py", ["pkg/a.py"], "") == 1.0
    assert hot_reader._file_match_score("C:/ws/app/pkg/a.py", [], "see c:/ws/app/pkg/a.py") == 0.7


def test_edit_reminders_need_a_file_match_for_rules_and_a_full_path_when_old():
    from windvane.hot_reader import score_loaded_entries

    now = time.time()
    ctx = {"file_path": "C:/ws/app/src/loader.py"}
    fresh_hit = {"id": "1", "category": "decision", "content": "loader.py reads the manifest first", "related_files": ["C:/ws/app/src/loader.py"], "created_at": now - 86400, "relevance": 6}
    old_name_drop = {"id": "2", "category": "decision", "content": "we discussed loader.py once", "related_files": ["loader.py"], "created_at": now - 91 * 86400, "relevance": 6}
    old_full_path = {"id": "3", "category": "decision", "content": "C:/ws/app/src/loader.py must stay lazy", "related_files": ["C:/ws/app/src/loader.py"], "created_at": now - 91 * 86400, "relevance": 6}
    far_rule = {"id": "4", "category": "rule", "content": "Delegate session maintenance to a background agent", "related_files": [], "created_at": now - 128 * 86400, "relevance": 9}
    near_rule = {"id": "5", "category": "rule", "content": "src/loader.py: never read the whole file", "related_files": ["C:/ws/app/src/loader.py"], "created_at": now - 128 * 86400, "relevance": 9}
    out = score_loaded_entries([fresh_hit, old_name_drop, old_full_path, far_rule, near_rule], ctx, limit=3)
    ids = [e["id"] for e in out]
    assert "1" in ids and "3" in ids and "5" in ids
    assert "2" not in ids and "4" not in ids
    # No file-relevant memory: silence, even with a rule on hand.
    assert score_loaded_entries([far_rule], ctx) == []
    # No file in the context: plain ranking.
    assert [e["id"] for e in score_loaded_entries([far_rule, fresh_hit], {}, limit=1)] == ["4"]


def test_a_rule_outranks_a_mistake_on_the_same_file():
    from windvane.hot_reader import score_entry

    base = {"related_files": ["C:/ws/app/a.py"], "created_at": time.time(), "relevance": 5}
    ctx = {"file_path": "C:/ws/app/a.py"}
    assert score_entry({**base, "category": "rule"}, ctx) > score_entry({**base, "category": "mistake"}, ctx)


def test_extract_file_refs_keeps_directories():
    from windvane.hot_reader import extract_file_refs

    refs = extract_file_refs('failed in "service-a/pkg/x.py" and C:\\ws\\b\\y.ts, not .hidden.py')
    assert "service-a/pkg/x.py" in refs
    assert "C:\\ws\\b\\y.ts" in refs
    assert all(not r.startswith(".") for r in refs)


def test_the_injection_weight_reads_the_store_and_is_bounded(tmp_path):
    from windvane import hot_reader

    assert hot_reader._memory_injection_weight() == 1.0
    _write_json(_store(tmp_path) / "injection_weights.json", {"weights": {"memory": 5.0}})
    assert hot_reader._memory_injection_weight() == 1.2
    _write_json(_store(tmp_path) / "injection_weights.json", {"weights": {"memory": 0.1}})
    assert hot_reader._memory_injection_weight() == 0.8


def test_hot_reader_reads_the_configured_store_with_inheritance(tmp_path):
    from windvane.hot_reader import HotMemoryReader

    ws = tmp_path / "ws"
    app = ws / "app"
    app.mkdir(parents=True)
    _register(_store(tmp_path), {
        ws: ("h_ws", [{"id": "w1", "content": "workspace"}]),
        app: ("h_app", [{"id": "a1", "content": "own"}, {"id": "a2", "content": "gone", "archived_at": 1}]),
    })
    r = HotMemoryReader()
    assert r._storage == _store(tmp_path)
    assert [e["id"] for e in r.load_entries(str(app))] == ["a1", "w1"]  # archived invisible
    ctx = {"file_path": str(app / "a.py")}
    assert r.get_scored_memories(str(app), ctx) == []  # nothing names the file
    assert HotMemoryReader._score_entry({"category": "rule"}, {}) > 0.3


def test_a_project_sharing_only_its_directory_name_is_another_project(tmp_path):
    """Both readers: a path with no store of its own takes its ancestors and
    never a registered project elsewhere that happens to have the same name."""
    from windvane import storage
    from windvane.hot_reader import HotMemoryReader

    ws = tmp_path / "ws"
    other_api = tmp_path / "elsewhere" / "api"
    (ws / "api").mkdir(parents=True)
    other_api.mkdir(parents=True)
    _register(_store(tmp_path), {
        ws: ("h_ws", [{"id": "w1", "category": "rule", "content": "workspace rule"}]),
        other_api: ("h_other", [{"id": "o1", "category": "mistake", "content": "the other api's mistake"}]),
    })
    # Under a registered workspace: the ancestors only.
    assert {e["id"] for e in storage.load_project_memory(str(ws / "api"))["entries"]} == {"w1"}
    assert [e["id"] for e in HotMemoryReader().load_entries(str(ws / "api"))] == ["w1"]
    # Nowhere registered, same name as a registered project: nothing.
    lone = tmp_path / "lone" / "api"
    lone.mkdir(parents=True)
    assert storage.load_project_memory(str(lone)) == {}
    assert HotMemoryReader().load_entries(str(lone)) == []


def test_store_reexports_every_name_it_imports_from_hot_reader():
    from windvane import hot_reader, store

    for name in ("CATEGORY_BONUSES", "RECENCY_HALF_LIFE_DAYS", "SCORE_WEIGHTS", "HotMemoryReader",
                 "_GENERIC_BASENAMES", "_HOOK_TAG_PATTERNS", "_file_match_score", "extract_file_refs"):
        assert getattr(store, name) is getattr(hot_reader, name)


# ── precheck ────────────────────────────────────────────────────────────────


def _indexed_project(tmp_path: Path) -> Path:
    from windvane.code_index import build_code_index
    from windvane.store import project_store_dir

    proj = tmp_path / "app"
    pkg = proj / "pkg"
    pkg.mkdir(parents=True)
    (proj / "pyproject.toml").write_bytes(b"")
    (pkg / "__init__.py").write_bytes(b"")
    (pkg / "core.py").write_bytes(b"def run():\n    pass\n\n\ndef stop():\n    pass\n")
    (pkg / "a.py").write_bytes(b"from pkg.core import run\n")
    (pkg / "b.py").write_bytes(b"import pkg.core\n")
    assert build_code_index(str(proj), project_store_dir(str(proj))) is not None
    return proj


def test_precheck_names_an_import_that_will_not_resolve(tmp_path):
    from windvane.precheck import precheck_edit

    proj = _indexed_project(tmp_path)
    f = str(proj / "pkg" / "c.py")
    out = precheck_edit(f, "from pkg.core import runn\n", str(proj))
    assert out.startswith("<windvane-precheck>") and out.endswith("</windvane-precheck>")
    assert "`runn` is not exported by `pkg.core` (exports: run, stop). Closest: run." in out
    out = precheck_edit(f, "import pkg.nothere\n", str(proj))
    assert "module `pkg.nothere` not found" in out
    # Silent on what it cannot judge: valid imports, externals, relatives, non-Python.
    assert precheck_edit(f, "from pkg.core import run, stop\nimport os\nfrom . import x\n", str(proj)) == ""
    assert precheck_edit(str(proj / "x.md"), "from pkg.core import rnu\n", str(proj)) == ""
    assert precheck_edit(f, "no imports here", str(proj)) == ""
    assert precheck_edit(f, "from pkg.core import rnu\n", str(tmp_path / "unindexed")) == ""


def test_blast_radius_and_read_context(tmp_path):
    from windvane.precheck import blast_radius, read_context

    proj = _indexed_project(tmp_path)
    core = str(proj / "pkg" / "core.py")
    out = blast_radius(core, str(proj))
    assert out.startswith("<windvane-blast-radius>") and out.endswith("</windvane-blast-radius>")
    assert "`pkg.core` is imported by 2 module(s)" in out
    assert blast_radius(str(proj / "pkg" / "a.py"), str(proj)) == ""  # a leaf
    assert blast_radius(str(proj / "README.md"), str(proj)) == ""
    ctx = read_context(core, str(proj))
    assert ctx.startswith("- `pkg.core`") and "defines run, stop" in ctx and "imported by 2" in ctx
    assert read_context(str(proj / "notes.txt"), str(proj)) == ""


def test_precheck_parses_conservatively():
    from windvane.precheck import _parse_imported_names

    assert _parse_imported_names("a, b as c  # note") == ["a", "b"]
    assert _parse_imported_names("(a,") == []
    assert _parse_imported_names("*") == []


# ── proc_lock ───────────────────────────────────────────────────────────────


def test_a_second_holder_of_a_process_lock_is_refused(tmp_path):
    from windvane import proc_lock

    first = proc_lock.acquire(tmp_path / "x.lock")
    assert first is not None
    assert proc_lock.acquire(tmp_path / "x.lock") is None
    assert proc_lock.held(tmp_path / "x.lock")
    first.release()
    first.release()  # a second release is a no-op
    assert not proc_lock.held(tmp_path / "x.lock")
    again = proc_lock.acquire(tmp_path / "x.lock")
    assert again is not None
    again.release()


def test_a_lock_that_never_existed_is_not_held(tmp_path):
    from windvane import proc_lock

    assert not proc_lock.held(tmp_path / "sub" / "never.lock")
    lock = proc_lock.acquire(tmp_path / "sub" / "deeper" / "y.lock")  # parents are created
    assert lock is not None and lock.path == tmp_path / "sub" / "deeper" / "y.lock"
    lock.release()

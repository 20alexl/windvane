"""windvane configuration: one place for every setting and tuning knob.

Four layers, the first that names a key wins:

1. the environment, ``WINDVANE_<KEY>`` (``WINDVANE_STRIKE_CAP=5``);
2. the project file, ``<project>/.windvane/config.json``;
3. the plugin's userConfig, as Claude Code stores it in
   ``~/.claude/settings.json`` under ``pluginConfigs`` (the entry whose key
   starts with ``windvane``);
4. the user file, ``~/.windvane/config.json`` (or ``$WINDVANE_DIR/config.json``);

then the default in ``KNOBS``. A layer that is missing or unreadable is
skipped; nothing here raises.

    {"structure": true, "goal_turn_cap": 200, "compliance": false}

``KNOBS`` lists every key with its default and the module that reads it.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

CONFIG_DIR = ".windvane"
CONFIG_FILE = "config.json"
ENV_PREFIX = "WINDVANE_"

# key: (default, reader, what it does). The default's type is the key's type;
# a value of another type is coerced, and one that will not coerce falls back
# to the default. ``None`` means "computed" (see the reader).
KNOBS: dict[str, tuple[Any, str, str]] = {
    # Project shape and the rule pack
    "structure": (False, "events.session_start", "seed CLAUDE.md, .learnings/ and session-logs/ where missing"),
    "default_rules": (True, "rules", "seed the default rule pack once per project"),
    "strict_pack": (False, "events.session_start", "seed the strict pack beside the default one"),
    "compliance": (True, "compliance", "match rules that carry a detector against tool calls"),
    # Unattended runs
    "autonomy": (False, "config.autonomy_on", "stall nudges and the halt brake"),
    "alert_command": ("", "alerts", "shell command that receives one alert line ({message} or stdin)"),
    "goal_turn_cap": (150, "goal", "turns under one /goal before the halt is armed"),
    "stall_turns": (3, "stall", "consecutive no-effect turns per strike"),
    "stall_decay": (5, "stall", "consecutive good turns that remove one strike"),
    "strike_cap": (3, "stall", "strikes before the halt (autonomy mode only)"),
    # Context pressure and the usage budget
    "output_reserve": (32_000, "pressure", "tokens between the configured compaction point and where it fires"),
    "checkpoint_margin": (None, "pressure", "tokens above the trigger for CHECKPOINT NOW (20000, or 10000 on a 200K window)"),
    "headsup_fraction": (0.10, "pressure", "fraction of the window before the point for the heads-up"),
    "checkpoint_cadence": (60, "pressure", "turns with no checkpoint or completed step before the fallback reminder"),
    "budget_five_hour_pct": (90, "pressure", "usage percent of the 5-hour window that nudges"),
    "budget_seven_day_pct": (95, "pressure", "usage percent of the 7-day window that nudges"),
    "budget_pct": (90, "pressure", "usage percent of any other rate-limit window that nudges"),
    # Mining and paths
    "live_mine": (300, "events.stop", "seconds between live mining ticks at turn end; 0 disables"),
    "non_project_dirs": ("", "paths", "comma-separated directory names that are never a project"),
    "git_trace": ("", "repo_state", "file that logs every git call (debugging)"),
}
# The plugin userConfig rows python, status_segment, result_budget and
# semantic are not knobs: the mod reads them from the plugin options (and
# WINDVANE_PYTHON / WINDVANE_RESULT_BUDGET), windvane.semantic from the
# plugin row and WINDVANE_SEMANTIC. No config file sets them.

DEFAULTS: dict[str, Any] = {k: v[0] for k, v in KNOBS.items()}

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


def store_dir() -> Path:
    """The store: ``WINDVANE_DIR`` (a leading ``~`` expanded), else
    ``~/.windvane``. ``paths.get_windvane_storage_dir()`` is the same
    location, and ``daemon_client.py`` resolves it the same way."""
    override = os.environ.get("WINDVANE_DIR", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    return Path.home() / ".windvane"


def config_path(project_dir: str) -> Path:
    return Path(project_dir) / CONFIG_DIR / CONFIG_FILE


def _settings_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return (Path(base) if base else Path.home() / ".claude") / "settings.json"


# ---------------------------------------------------------------------------
# Layers
# ---------------------------------------------------------------------------

_file_cache: dict[str, tuple[int, dict]] = {}


def _read_json(path: Path) -> dict:
    """A JSON object from ``path``, cached on its mtime (a long-lived daemon
    sees an edit without a restart). A file changed in the last two seconds
    is read again: two writes inside the filesystem's timestamp granularity
    share an mtime. {} when missing or unreadable."""
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {}
    key = str(path)
    hit = _file_cache.get(key)
    if hit and hit[0] == stamp and time.time() - stamp / 1e9 > 2.0:
        return hit[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    _file_cache[key] = (stamp, data)
    return data


def user_config() -> dict:
    """``<store>/config.json``: the person's defaults for every project."""
    return _read_json(store_dir() / CONFIG_FILE)


def plugin_config() -> dict:
    """The plugin's userConfig values from Claude Code's settings:
    ``pluginConfigs`` -> the first key starting with ``windvane``. The values
    sit under ``options`` in some versions and flat in others; both read."""
    data = _read_json(_settings_path())
    configs = data.get("pluginConfigs")
    if not isinstance(configs, dict):
        return {}
    for key in sorted(configs):
        if str(key).startswith("windvane"):
            entry = configs[key]
            if not isinstance(entry, dict):
                return {}
            opts = entry.get("options")
            return opts if isinstance(opts, dict) else entry
    return {}


def project_config(project_dir: str = "") -> dict:
    """The project file over the user file (keys merged, project wins)."""
    merged = dict(user_config())
    if project_dir:
        merged.update(_read_json(config_path(project_dir)))
    return merged


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def _coerce(key: str, raw: Any) -> Any:
    default = DEFAULTS.get(key)
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        return default
    if isinstance(default, int) or (default is None and key == "checkpoint_margin"):
        if str(raw).strip().lower() in _FALSE:
            return 0  # "off" disables a counted knob (live_mine)
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default
    if isinstance(default, str):
        if isinstance(raw, (list, tuple)):
            return ",".join(str(x) for x in raw)
        return "" if raw is None else str(raw)
    return raw


def knob(key: str, project_dir: str = "") -> Any:
    """One setting, resolved through the four layers. An unknown key reads
    the layers the same way and returns None when no layer names it."""
    env = os.environ.get(ENV_PREFIX + key.upper(), "")
    if env.strip():
        return _coerce(key, env) if key in DEFAULTS else env
    for layer in (_read_json(config_path(project_dir)) if project_dir else {}, plugin_config(), user_config()):
        if key in layer and layer[key] is not None and layer[key] != "":
            return _coerce(key, layer[key]) if key in DEFAULTS else layer[key]
    return DEFAULTS.get(key)


def knob_int(key: str, project_dir: str = "", default: Optional[int] = None) -> int:
    v = knob(key, project_dir)
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(default if default is not None else (DEFAULTS.get(key) or 0))


def knob_float(key: str, project_dir: str = "", default: Optional[float] = None) -> float:
    v = knob(key, project_dir)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default if default is not None else (DEFAULTS.get(key) or 0.0))


def load(project_dir: str = "") -> dict:
    """Every knob resolved for ``project_dir``. Never raises."""
    cfg = {k: knob(k, project_dir) for k in KNOBS}
    try:
        cfg["goal_turn_cap"] = max(1, int(cfg["goal_turn_cap"]))
    except Exception:
        cfg["goal_turn_cap"] = DEFAULTS["goal_turn_cap"]
    return cfg


def enabled(cfg: dict, key: str) -> bool:
    v = cfg.get(key)
    return bool(v) if not isinstance(v, str) else v.strip().lower() not in _FALSE + ("",)


def autonomy_on(state: Optional[dict] = None, project_dir: str = "") -> bool:
    """Autonomy mode: ``WINDVANE_AUTONOMY=1``, the ``autonomy`` setting
    (the plugin's userConfig row, or a config file), or a /goal running in
    this session (``goal.running``). Stall nudges and the halt brake are
    on only here."""
    if knob("autonomy", project_dir):
        return True
    if not state:
        return False
    try:
        from windvane import goal

        return goal.running(state)
    except Exception:
        return False


def knobs_table() -> str:
    """The KNOBS table as Markdown rows (the README and ``--knobs`` print it)."""
    rows = ["| key | default | read by | what |", "|---|---|---|---|"]
    for k, (default, reader, what) in KNOBS.items():
        d = "computed" if default is None else json.dumps(default)
        rows.append(f"| `{k}` | {d} | {reader} | {what} |")
    return "\n".join(rows)


if __name__ == "__main__":  # pragma: no cover
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--knobs":
        print(knobs_table())
    else:
        print(json.dumps(load(sys.argv[1] if len(sys.argv) > 1 else ""), indent=2))

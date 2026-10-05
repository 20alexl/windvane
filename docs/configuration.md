# Configuration

## Where a setting comes from

Every engine setting is resolved through four layers. The first layer that names the key wins, and a layer that is missing or unreadable is skipped:

1. **The environment**, `WINDVANE_` plus the key in capitals (`WINDVANE_STRIKE_CAP=5`).
2. **The project file**, `<project>/.windvane/config.json`.
3. **The plugin config**, the rows below, which Claude Code stores in `~/.claude/settings.json` under `pluginConfigs` (the entry whose key starts with `windvane`; `CLAUDE_CONFIG_DIR` moves that folder).
4. **The user file**, `config.json` in the store (`~/.windvane/config.json`, or under `WINDVANE_DIR`).

Then the default applies. A value of the wrong type is coerced, and one that will not coerce falls back to the default. The files are plain JSON objects:

```json
{"structure": true, "goal_turn_cap": 200, "compliance": false}
```

Edits are seen without a restart. A boolean accepts `1`, `true`, `yes`, `on` and their opposites. A counted setting accepts `off` for 0.

To print every setting as resolved for a project: `python -m windvane.config <project>`. To print the reference table: `python -m windvane.config --knobs`. Run both from the cloned folder.

## The plugin config rows

| Row | Type | Default | Effect |
|---|---|---|---|
| `python` | string | empty | The interpreter that runs the engine (3.10 or later). Empty means `python` on PATH. `WINDVANE_PYTHON` wins over this row. |
| `status_segment` | boolean | true | Show the context fill and the checkpoint age in the status line. The band and the pane stay when it is off. |
| `result_budget` | string | `60000` | Characters a tool result keeps before its middle is cut. Head and tail stay. `WINDVANE_RESULT_BUDGET` wins over this row. |
| `semantic` | boolean | false | Use the sentence-transformers encoder for decision capture and memory and session search when the semantic extra is installed. Off, the regex tier scores. The first interactive session offers to install the extra and turn this on. |
| `alert_command` | string | empty | A shell command that receives one line when an unattended run halts, hits a usage limit or needs input. Empty means no alerts. |
| `strict_pack` | boolean | false | Seed the strict pack (style and workflow rules) beside the default pack in every new project. |
| `autonomy` | boolean | false | Stall nudges and the halt brake for unattended runs. Off for an attended session. |

`python`, `status_segment`, `result_budget` and `semantic` are plugin rows only, with `WINDVANE_PYTHON`, `WINDVANE_RESULT_BUDGET` and `WINDVANE_SEMANTIC` as their environment switches. They are not config-file keys. `alert_command`, `strict_pack` and `autonomy` are also engine settings and follow the layers above.

## Every setting

| Key | Default | Read by | Effect |
|---|---|---|---|
| `structure` | false | session start | Seed CLAUDE.md, `.learnings/` and `session-logs/` where missing, in a directory that is already a project |
| `default_rules` | true | rules | Seed the default rule pack once per project |
| `strict_pack` | false | session start | Seed the strict pack beside the default one |
| `compliance` | true | compliance | Match rules that carry a detector against tool calls |
| `autonomy` | false | autonomy check | Stall nudges and the halt brake |
| `alert_command` | empty | alerts | Shell command that receives one alert line, either as `{message}` in the command or on stdin |
| `goal_turn_cap` | 150 | goal | Turns under one `/goal` before the halt is armed |
| `stall_turns` | 3 | stall | Consecutive no-effect turns per strike |
| `stall_decay` | 5 | stall | Consecutive good turns that remove one strike |
| `strike_cap` | 3 | stall | Strikes before the halt (autonomy mode only) |
| `output_reserve` | 32000 | pressure | Tokens between the configured compaction point and where it fires |
| `checkpoint_margin` | computed | pressure | Tokens above the trigger for the checkpoint band: 20000, or 10000 on a 200K window |
| `headsup_fraction` | 0.10 | pressure | Fraction of the window before the point for the heads-up. Must be between 0 and 1 |
| `checkpoint_cadence` | 60 | pressure | Turns with no checkpoint or finished step before the fallback reminder |
| `budget_five_hour_pct` | 90 | pressure | Usage percent of the 5-hour window that nudges |
| `budget_seven_day_pct` | 95 | pressure | Usage percent of the 7-day window that nudges |
| `budget_pct` | 90 | pressure | Usage percent of any other rate-limit window that nudges |
| `live_mine` | 300 | turn end | Seconds between live mining ticks at turn end, 0 disables. Values under 60 are raised to 60 |
| `non_project_dirs` | empty | paths | Comma-separated directory names that are never a project |
| `git_trace` | empty | repo state | File that logs every git call, for debugging |

`default_rules`, `compliance`, `structure` and `strict_pack` can be set per project in `.windvane/config.json`. A project that wrote its own rules keeps them: seeding skips any rule the project or an ancestor already has in substance.

## The compaction point

windvane takes the point from Claude Code. In order: the `CLAUDE_CODE_AUTO_COMPACT_WINDOW` environment variable (100,000 or more), the `--autocompact` flag of a background job, then the `autoCompactWindow` setting from managed, project-local, project and user settings. With none of those, the point is the window on a 200K model and 967,000 tokens on a larger one. A configured window larger than the model's window is capped at the window. `python -m windvane.pressure assess <session_id> [project_dir]` prints the assessment for a session as JSON.

## Environment switches

| Variable | Effect |
|---|---|
| `WINDVANE_DIR` | The store folder, instead of `~/.windvane`. A leading `~` is expanded. |
| `WINDVANE_PYTHON` | The interpreter for the engine. Wins over the `python` row. |
| `WINDVANE_RESULT_BUDGET` | The tool-result budget in characters. Wins over the `result_budget` row. |
| `WINDVANE_AUTONOMY` | Set to `1` for autonomy mode. |
| `WINDVANE_SEMANTIC` | `1` turns the semantic tier on, `0` forces it off whatever the row says; unset, the row decides. |
| `WINDVANE_ALERT_COMMAND`, `WINDVANE_STRIKE_CAP`, `WINDVANE_GOAL_TURN_CAP`, `WINDVANE_LIVE_MINE` | The matching settings above. |
| `WINDVANE_COMPLIANCE` | Turns the compliance check on or off. |
| `WINDVANE_NON_PROJECT_DIRS` | The `non_project_dirs` setting. |
| `WINDVANE_DAEMON_TIMEOUT` | Seconds of idle before the daemon exits. Default 1800. |
| `WINDVANE_NO_DAEMON` | When set, no daemon is spawned. For benchmarks and one-off runs against a temporary store. |
| `WINDVANE_HOOK_DEBUG` | When set, the hook client prints diagnostics to stderr. |
| `WINDVANE_LAST_FILE_PATH` | When set, the pre-read hook writes the last file read to this path, for a status line script. |
| `WINDVANE_ARCHIVE_DAYS` | Days before an unused entry is archived. Default 14. |
| `WINDVANE_SESSION_RETENTION_DAYS` | When above 0, drops search shards of whole months older than this many days. |
| `WINDVANE_EMBED_MODEL`, `WINDVANE_EMBED_DIM`, `WINDVANE_DEVICE` | The embedding model, its truncated dimension and the device, for the semantic tier. |

## Rule pack commands

The default pack seeds itself once per project, on the first fresh session in a directory that has a repository or a manifest file. The strict pack is opt-in.

```bash
python -m windvane.rules seed --project DIR            # default tier, if not seeded yet
python -m windvane.rules seed --project DIR --strict   # also the strict tier
python -m windvane.rules seed --project DIR --force    # seed again even if the marker says seeded
```

Run these from the cloned folder. Each prints one JSON line with `added`, `skipped`, `detectors_attached` and the tiers seeded. A marker, `default_pack.json`, in the project's store folder records what was seeded. `/windvane-strict` runs the strict seed for the session's project.

Manage rules with the memory tool: `list_rules` to see them with their ids, `add_rule` with `content`, `reason` and an optional `detector`, `set_detector` to attach or clear one (`{}` clears), `modify` to change the text, `delete` to remove one, `archive` and `restore` to set one aside and bring it back. A detector has these keys:

| Key | Meaning |
|---|---|
| `tools` | Tool names the rule watches. Empty means any tool. |
| `command` | A regex matched against a shell command. |
| `paths` | Globs matched against an edited path. |
| `input` | A regex matched against the tool input as JSON. |
| `note` | What the detector catches, for the report. |
| `unattended` | Set to `deny` to refuse the call in autonomy mode. |

A rule without a detector is advisory. The default pack's detectors cover destructive shell commands, outbound actions (a push, a pull request, a publish, an outbound request) and kill by process name.

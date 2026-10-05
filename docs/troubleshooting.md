# Troubleshooting

Start with the setup check. Run it from the folder you cloned (or set `PYTHONPATH=<folder>`):

```bash
python -m windvane.doctor
```

It prints one JSON line with the interpreter, the store, the semantic tier and the daemon. It only reads. It never starts, stops or replaces the daemon.

## The daemon

The daemon is optional for correctness. Without it every hook still runs, in its own process, at the cost of the package imports on each call.

**Files.** The daemon keeps these in the store (`~/.windvane`, or `WINDVANE_DIR`):

| File | Meaning |
|---|---|
| `daemon_port` | The loopback port it listens on. Written last, after it is ready. |
| `daemon_token` | The secret every request must carry. Minted at each start, readable by the owner alone. Without it the clients run in their own process, and the daemon refuses what arrives. |
| `daemon_pid` | Its process id. |
| `daemon.lock` | Held for its whole life. A held lock is what means a daemon is alive. |
| `daemon_model` | The encoder signature it loaded, or `none`. |
| `daemon_starting` | A marker that a spawn is under way. Only one spawn is made per 30 seconds. |

**Does it answer?** `python -m windvane.doctor` reports `daemon.answers`. It is false when no port file exists, which is normal before the first session and after the daemon's idle exit. The port file with `answers` false means the port is stale or the daemon is stalled.

**Why it is not running.** The first hook that finds none starts one. Several things stop that: `WINDVANE_NO_DAEMON` is set; a start was already attempted in the last 30 seconds; or the interpreter cannot start (see below). The daemon also exits on its own after 30 idle minutes (`WINDVANE_DAEMON_TIMEOUT` seconds) and whenever the package's source files change on disk, so editing the engine restarts it on the next hook.

**Restart it.** End the one process whose id is in `daemon_pid`, by that id only. Do not end Python processes by name, since others may be yours. The next hook starts a fresh daemon. Stale files with no lock behind them are removed on the next check.

**See what it says.** The spawned daemon discards its output. To read it, start one by hand in the foreground from the cloned folder with `python -m windvane.daemon`, after ending the running one. It prints the port and whether the semantic tier loaded. Set `WINDVANE_HOOK_DEBUG=1` to make the hook client print why it did or did not use the daemon.

**Several versions of the plugin or several stores.** Each store has its own daemon, because the files live in the store. A client whose configured encoder differs from the running daemon's replaces it.

## The interpreter

Every part of the engine is run with the interpreter named by `WINDVANE_PYTHON`, else the `python` row, else `python` on PATH. The classic hooks in `hooks/hooks.json` run `python` from PATH directly, so on a machine where `python` is not on PATH they fail even when the row is set. Put a working `python` on PATH, or accept that only the mod's own engine calls (tools, commands, the compaction brief, agent briefs) use the row.

When the mod cannot start the interpreter it says so: `python did not run`, followed by the cause, and advice to set the row or `WINDVANE_PYTHON` to a 3.10 or later interpreter. The setup check exits with status 1 and names the minimum when the interpreter is older than 3.10.

## The semantic extra

The semantic tier needs `numpy` and `sentence-transformers` installed in the interpreter that runs the daemon:

```bash
pip install "sentence-transformers>=2.7.0" "numpy>=1.24.0"
```

Then turn on the `semantic` row, or set `WINDVANE_SEMANTIC=1`. Both conditions are needed: requested and installed. `python -m windvane.doctor` shows `installed` and `requested` separately. The first request loads the model in the background, so scoring waits on that load and falls back to the regex tier meanwhile. Changing the model or the tier replaces a running daemon. If the tier is on but the extra is missing, nothing breaks and the regex tier scores.

## Hooks that time out

The hook timeouts in `hooks/hooks.json` are in seconds: 3 on the per-tool hooks, 5 on the prompt, turn-end, post-compaction, session-end and stop-failure hooks, and 10 on session start and before compaction. A hook that goes past its timeout is dropped by Claude Code and its output is lost for that call. The usual cause is no daemon: a cold handler pays the imports. Check the daemon first.

In an interactive session the bridge handles a slow daemon itself. A request that timed out may have run, so it is not repeated. For the next 30 seconds the events go to the command hooks while the daemon recovers. Tool calls to the daemon wait 60 seconds. A tool call that times out is not retried, and the model is told to check its effect before calling again.

## The dim line a failed hook leaves

In a session that hot-reloads the plugin folder, a failed hook or a module that did not load leaves one dim line in the transcript. In every other session the line goes to the debug log, as `windvane: <line>`. windvane's client catches its own failures and prints nothing, so such a line points at something outside the handler: the interpreter did not start, or the hook timed out. Check the interpreter and the daemon as above.

## Markers, and why the rules appear twice or not at all

Two small files under `sessions/` in the store coordinate the mod and the hooks:

- `<session id>.mod` is written every 10 seconds by the mod. While it is under two minutes old the engine knows a mod is present, the session-start banner does not complain about a missing status line, and status line scripts stay out of the context mirror.
- `<session id>.briefed` is written when the mod placed the rules and the checkpoint inside a compacted conversation. While it is under two minutes old, the session-start banner for the compaction leaves them out.

If the rules and the checkpoint show twice after a compaction, the marker was not written or was older than two minutes. If they show neither, the marker is fresh but the compacted conversation lost the message. Run `/windvane` to see what the store holds and `python -m windvane.brief --project <dir> --checkpoint` to see what would be injected.

If the status segment is missing in a session, check `status_segment`, and check that `sessions/` exists in the store. The mod logs `no sessions folder` and keeps the mirror off when it does not. The engine creates that folder the first time a hook saves session state, and the segment appears after the next tick, within ten seconds.

The band above the prompt says what windvane last put in front of the model. It counts only rows that carry windvane's markers. A band that shows tag names such as `edit-reminder` instead of counts means the last row held no rules or mistakes.

## Where the logs are

- **Mod.** Its diagnostics go to Claude Code's debug log (`claude --debug`) on lines that begin `windvane:`. In a session that hot-reloads the plugin folder (a `--plugin-dir` session), a failed hook or a module that did not load also leaves one dim line in the transcript.
- **Engine.** There is no log file. The daemon and the background miner discard their output.
- **Mining.** `mining_status.json` in the store holds the miner's status and, when Claude Code's transcript format is no longer recognised, a warning that the session-start banner shows.
- **Runs.** A substantial session writes a run report to `<project>/.windvane/runs/`. `mine(run_report)` writes and returns the current session's report.
- **Alerts.** Each alert, sent or not, is recorded in the session state with the reason a send failed.

## Other symptoms

**A run halted and every tool is denied.** This is the autonomy brake. The denial names the cause. Release it with `python -m windvane.stall release <session_id>` from the cloned folder, and `/goal clear` if a goal is active. `python -m windvane.stall status <session_id>` prints the strikes and events.

**A command was refused by a rule.** A rule with a detector marked `unattended: deny` refuses a matching command in autonomy mode. Either approve the work another way, change the rule with `memory(set_detector)`, or turn autonomy off.

**Rules did not seed.** The default pack seeds on the first fresh session in a directory that holds a repository, a manifest file or a CLAUDE.md, and never in a home directory or a drive root. `default_rules` false turns it off. `python -m windvane.rules seed --project DIR` seeds by hand.

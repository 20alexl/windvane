# Install

## Requirements

- Claude Code with plugin support.
- Python 3.10 or later. The engine uses only the standard library. The interpreter is `python` on PATH, the `python` config row, or the `WINDVANE_PYTHON` environment variable, in that order of increasing priority.
- The semantic tier is optional and needs the `semantic` extra (see [troubleshooting](troubleshooting.md#the-semantic-extra)).

## Install

```bash
git clone https://github.com/20alexl/windvane.git windvane
claude plugin marketplace add ./windvane
claude plugin install windvane@windvane
```

The marketplace is the cloned folder itself, and the plugin in it is named `windvane`.

## Configure

Open the config rows with:

```
/plugin configure windvane@windvane
```

Or set them when installing, with `--config KEY=VALUE` once per row:

```bash
claude plugin install windvane@windvane --config python=/usr/bin/python3 --config strict_pack=true
```

The rows are `python`, `status_segment`, `result_budget`, `semantic`, `alert_command`, `strict_pack`, `autonomy`, `continue_after_compact` and `early_compaction`. Each is described in [configuration](configuration.md). All of them have working defaults, so nothing needs setting for a first run.

The first interactive session asks one question: whether to install the semantic extra (sentence-transformers and numpy, several hundred megabytes) and turn the `semantic` row on. With it, memory and session search use a small embedding model and find paraphrases a keyword match misses. "Not now" asks again a week later, "Never ask" closes the question, and a headless run is never asked. A session with `WINDVANE_SEMANTIC` set in its environment, to `1` or to `0`, is not asked either: the variable is the decision. The same two steps by hand are the pip line in [troubleshooting](troubleshooting.md#the-semantic-extra) and the row in `/plugin configure windvane@windvane`.

## Verify

The setup check reads the machine and prints one JSON line. It does not start, stop or replace the daemon. Run it from the folder you cloned, or set `PYTHONPATH=<folder>`, so that Python can find the package. The installed plugin is read from that folder:

```bash
cd <folder> && python -m windvane.doctor
```

The line has four parts:

| Part | What to look for |
|---|---|
| `python` | `ok` is true when the interpreter is 3.10 or later. Below that the check prints an error and exits with status 1. |
| `store` | The store path, whether it holds a manifest, and how many projects it registers. A new install shows `exists` false until the first session writes one. |
| `semantic` | `installed` is true when the extra imports. `requested` is true when the row or `WINDVANE_SEMANTIC=1` asks for it. |
| `daemon` | `answers` is true when the daemon named by the port file replied. It is false until the first session has started one. |

Start a Claude Code session in a project. The session-start banner begins with `windvane session started`. In an interactive session the status line shows `windvane ctx N% ...` once the first mirror tick has run (every 10 seconds).

## Import an existing store

To bring over a claude-engram store, either run `/windvane-import` in a session (it shows the files copied, the files skipped and the destination), or run the check with the import flag:

```bash
cd <folder> && python -m windvane.doctor --import
```

By default the source is `~/.claude_engram`. Name another with `--from DIR`. The import copies the manifest, the project folders, the checkpoint rings and the other store files into the windvane store. It leaves out `sessions/` and the runtime files of a live engram process, and respells the engram name in the manifest's top-level keys. It never changes, moves or deletes the source.

If the windvane store already holds a manifest, the import is refused. Pass `--merge` to copy only the projects the store does not register. `python -m windvane.migrate --import --dry-run` counts what would be copied and writes nothing.

## Uninstall

```bash
claude plugin uninstall windvane@windvane
claude plugin marketplace remove windvane
```

The store is not touched. To remove the daemon, end the process whose id is in `~/.windvane/daemon_pid`, or let it exit on its own after 30 idle minutes. To remove the data, delete the store folder (`~/.windvane`, or the folder `WINDVANE_DIR` names) and each project's `.windvane/` folder.

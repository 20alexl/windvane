# windvane

A Claude Code plugin that keeps the state of a session and steers it: the recorder drafts the checkpoint, compaction happens at the right point with the state banked, the rules and the checkpoint ride inside the compacted conversation and every subagent's prompt, tool results are trimmed and secrets redacted, and project memory and rules are a tool call away. The principle: never ask the model to write what the machine can record.

This file is for people working on windvane itself. Users read README.md and docs/.

## Layout

- `.claude-plugin/plugin.json` is the manifest and holds the config rows. `marketplace.json` makes the repository its own marketplace.
- `hooks/` is the mod, in TypeScript. `register.ts` loads the pieces: the band, the pane, `/remember`, the strict, export and import commands, the door, the ledger, the agent briefs, the compaction brief, the bridge and the tools. `hooks.json` declares the classic command hooks used headless and as the bridge's fallback. `*.test.ts` files sit beside their modules.
- `engine/windvane/` is the Python engine. No dependencies. `hooks/` there holds the hook handlers, `mining/` the session miner, `semantic/` the optional encoder. `daemon.py` and `daemon_client.py` are the resident process and its thin client. `config.py` holds every setting in `KNOBS`.
- `skills/reference/SKILL.md` is the quick reference the model reads (`/windvane:reference`; the plain `/windvane` is the pane, so the skill cannot share that name).
- `docs/` is the user documentation. `demo/` holds the demo tape and gif.
- `tests/` holds the Python tests.
- `types/` and `.claude-plugin/types/` are the type declarations for the mod. The second folder is written by Claude Code when the plugin loads and is not committed.

## Tests

Python tests, from the repository root (needs `pytest`, available as the `test` extra):

```bash
python -m pytest tests/test_config.py
```

Run the tests for the module you changed, not the whole suite by default. `pyproject.toml` puts `engine` on the path.

Plugin tests and checks, from the repository root:

```bash
claude plugin test .
claude plugin validate .
```

Python needs 3.10 or later.

## Rules for the code

- The engine stays standard-library only. The semantic tier is an optional extra and is imported only inside the functions that need it.
- The mod follows `$` only within one file, and the engine takes one unmatched hook per event per plugin. That is why `register.ts` holds the session-start and turn-complete work for the other modules, and why each module declares its own state references. Keep that shape.
- `register.ts` mirrors the engine's pressure constants (output reserve, checkpoint margin, heads-up fraction, default compaction point). Change both together.
- Never write a file with a call that turns line endings into CRLF. The repository is LF, and `.gitattributes` normalises on read, so a CRLF file looks clean in `git status` while its bytes differ. Write bytes with the terminator the file already has.
- A hook handler must never raise into Claude Code. Failures degrade to an empty answer.
- The mod writes nothing to the store. The engine writes it.

## Rules for text

- Docs and comments are plain prose for a stranger: no personal names, machine names, workspace paths, email addresses or network addresses, and no emoji.
- Every claim in a doc is backed by code in this tree. When a doc and the code disagree, the code wins and the doc is fixed.
- Do not describe internal function names in user docs.
- Commits and published text carry no personal names.
- Keep user-visible numbers (defaults, thresholds, counts) in step with `config.py`, `rules.py` and `hooks.json`.

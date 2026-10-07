---
name: reference
description: Quick reference for the windvane tools (checkpoint, compact_now, memory, log, mine, deps) and what the hooks already do for you; use when banking state, storing a discovery, managing rules or searching past sessions.
---

# windvane

windvane records the session state itself. Most of what you would otherwise write down is already captured by hooks, so call a tool only for the cases below. The tools are `mcp__windvane__<name>`.

## Checkpoint

The recorder drafts the whole checkpoint: the task, the current step, completed and pending steps, files, warnings and the handoff note. It reads your task list, your edits, your git commits and the closing paragraph of your last reply.

- `checkpoint(operation="save")` with no other argument accepts the draft. This is the normal call.
- A field you pass amends that field and keeps the rest of the draft: `task_description`, `current_step`, `completed_steps`, `pending_steps`, `files_involved`, `handoff_summary`, `handoff_warnings`, `handoff_context_needed`.
- `checkpoint(operation="restore")` reads one back. `index` picks an older record, 0 is the newest.
- `checkpoint(operation="list")` shows the ring, newest first.

Save when a unit of work has closed. A save never compacts, so save at every step end. The save's reply gives the fill as a percent of the compaction point and, past the person's early mark, the one question the save raises: call `compact_now` now if the next step does not need what is in the context, or carry on. Decide there. The context notes say where the fill stands, as a percent of the compaction point, three times per cycle: `COMPACT AT A STEP END` (the person's early mark: no hurry; finish the step, save, and call `compact_now` if the phase has closed, else carry on), `Context pressure` (the heads-up: finish the step, start nothing long, compact at its end) and `CHECKPOINT NOW` (the last call: finish the step in hand, save, call `compact_now` and end the turn, since auto-compaction is next). A compaction keeps only what was banked.

## Compacting

`compact_now` banks the draft and compacts as soon as the turn ends. It is the only way a compaction starts before Claude Code's own trigger: nothing compacts on a save, and windvane never compacts on its own. Call it when a phase is done and the context is filling, then end your turn at once. After the compaction windvane sends one prompt that resumes the work; the rules and the checkpoint arrive with it in the session-start brief. Left alone, the session runs to the trigger, where Claude Code compacts and the draft is banked as it stands.

## Memory

- `memory(operation="remember", content=...)` when something genuinely surprised you about this codebase, a tool or a failure. The recorder captures what it can detect, not surprise. Do not use `remember` in place of a checkpoint. A fact and the resume state are two records.
- `memory(operation="recall")` lists the project memory. `search` with `query` finds entries.
- `memory(operation="add_rule", content=..., reason=...)` stores a permanent rule. `list_rules` shows rules with ids.
- `memory(operation="set_detector", memory_id=..., detector={...})` makes a rule watch tool calls. A detector holds `tools`, `command` (a regex), `paths` (globs), `input` (a regex), `note`, and `unattended: "deny"` to refuse the call in autonomy mode. `{}` clears it.
- `modify`, `delete`, `promote` (to a rule), `archive`, `restore` and `acknowledge_mistake` act on one entry by `memory_id`, the id shown in brackets.
- `list_mistakes` shows the tracked mistakes.

## Log

`log(operation="mistake", description=..., how_to_avoid=...)` records an error the hooks missed. `log(operation="decision", decision=..., reason=..., alternatives=[...])` records a choice and why. Failed tools and typed decisions are already captured, so log only what they did not catch.

## Mine and deps

- `mine(operation="search", query=...)` finds past conversation on this project. `kind` narrows the hits to decision, next-step, error or narration.
- `mine` also takes `decisions` (when and why something was decided), `errors`, `struggles`, `replay` (with `file_path`), `timeline`, `run_report`, `run_status` and `status`.
- `deps(operation="map", symbol=...)` says where a symbol is defined and what imports it. `deps(operation="impact", file_path=...)` lists what depends on a file before you change it.

## Keep the draft accurate

The draft reads your task list and your last reply. Keep the task list current with TaskCreate and TaskUpdate: the open tasks become the pending steps, finished ones the completed steps, and the task in progress the current step. End each reply with a short paragraph that says what is done and what is next. That paragraph becomes the handoff note. Say a step is done only when it is.

## Do not call what the hooks already do

windvane does these on its own, so a tool call for them wastes context: the session-start banner with the rules and the restored checkpoint, the pre-edit check of past mistakes, the import and impact checks before an edit, logging failed commands and tests, capturing decisions from the person's prompts, tracking test runs, banking a draft before a compaction, and the background mining of past sessions.

## When the run is halted

In autonomy mode a halt denies every tool except `checkpoint`, a push notification and a few messaging tools. Save a checkpoint that says what was blocking and what a person must decide, send the one-line notification, and stop.

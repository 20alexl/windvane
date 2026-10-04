// windvane: /windvane opens a pane with what windvane holds for this
// session, read from the store's own files:
//
// - the newest checkpoint record whole (the ring register.ts reads for the
//   status line): task, current step, completed, pending, files, warnings,
//   context needed, handoff note, goal;
// - the project's rules (the cwd's registered project and the ancestors it
//   inherits from, as the engine's project memory loader walks them);
// - the mistakes for the file the model last touched (Edit, Write, Read),
//   matched as the pre-edit check matches them.
//
// The pane reads the store when it opens and when Refresh is pressed.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, On, RenderElement } from 'claude-code'

import type { PaneView } from '../types'
import {
  ageText,
  mistakesFor,
  normalizePath,
  projectChain,
  readEntries,
  readLatest,
  readManifest,
  ringsFor,
  rulesOf,
  storePath,
} from './ring'
import type { Io } from './ring'
import { BAND_HIDDEN_KEY } from './band'
import { forgetCall, noteCallLoop } from './bridge'

const lastFile = atom({ plugin: 'windvane', key: 'lastFile' } as const, null)
const paneView = atom({ plugin: 'windvane', key: 'pane' } as const, null)
const pressure = atom({ plugin: 'windvane', key: 'pressure' } as const, null)
const bandHidden = atom({ plugin: 'windvane', key: 'bandHidden' } as const, false)

const PANE = 'windvane'
const TITLE = 'windvane'
const TOUCH_TOOLS = new Set(['Edit', 'Write', 'Read', 'MultiEdit', 'NotebookEdit'])

// Registered at session.start by register.ts.
export const PANE_COMMAND = {
  name: 'windvane',
  description: "Show windvane's checkpoint, rules and file mistakes in a pane",
}

function ioOf($: EngineInterface): Io {
  return { read: p => $.fs.read(p) as Promise<string>, exists: p => $.fs.exists(p) }
}

function list(...values: (string[] | undefined)[]): string[] {
  for (const v of values) if (Array.isArray(v) && v.length) return v.filter(s => typeof s === 'string' && s !== '')
  return []
}

// The pane's body, from the store as it stands.
async function loadView($: EngineInterface): Promise<PaneView> {
  const io = ioOf($)
  const store = storePath(await $.env.get('WINDVANE_DIR'), await $.env.get('USERPROFILE'), await $.env.get('HOME'))
  const manifest = await readManifest(io, store)
  const cwd = normalizePath(await $.session.cwd())
  const root = normalizePath(await $.session.root())

  const rec = await readLatest(io, ringsFor(manifest, store, cwd, root), store)
  const chain = projectChain(manifest, cwd)
  const rules = rulesOf(await readEntries(io, store, chain))

  const file = (await read($, lastFile)) ?? undefined
  const fileChain = file ? projectChain(manifest, file) : []
  const mistakes = file ? mistakesFor(await readEntries(io, store, fileChain.length ? fileChain : chain), file) : []

  const view: PaneView = { loadedAt: Date.now(), store, project: chain[0]?.path, rules, file, mistakes }
  if (rec) {
    const task = rec.task_description ?? ''
    const handoff = rec.handoff_summary || rec.summary || ''
    view.checkpoint = {
      created: rec.created ?? rec.timestamp,
      kind: rec.kind,
      task_id: rec.task_id,
      project_path: rec.project_path,
      task_description: task || undefined,
      current_step: rec.current_step || undefined,
      completed: list(rec.completed_steps),
      pending: list(rec.next_steps, rec.pending_steps),
      files: list(rec.files_in_progress, rec.files_involved),
      warnings: list(rec.warnings, rec.handoff_warnings),
      context_needed: list(rec.context_needed, rec.handoff_context_needed),
      handoff: handoff && handoff !== task ? handoff : undefined,
      goal: rec.goal || undefined,
    }
  }
  return view
}

async function refresh($: EngineInterface): Promise<void> {
  const view = await loadView($)
  await update($, paneView, () => view)
}

async function showBand($: EngineInterface): Promise<void> {
  await update($, bandHidden, () => false)
  await $.store.set(BAND_HIDDEN_KEY, false)
}

export function registerPane(on: On): void {
  on('command.run', { command: 'windvane' }, async $ => {
    await refresh($)
    await $.ui.open({ id: PANE, title: TITLE })
    return { text: 'windvane pane opened.' }
  })

  // The file the model last touched; a subagent's touches are its own.
  // Also the loop each call runs in, for the bridge: classic.PreToolUse,
  // which fires beneath this hook, names none (bridge.ts).
  on('tool.call', async ($, e, next) => {
    noteCallLoop(e.tool_use_id, e.agentId)
    const ran = await next(e).finally(() => forgetCall(e.tool_use_id))
    const path = (e as unknown as { file_path?: unknown; notebook_path?: unknown }).file_path
      ?? (e as unknown as { notebook_path?: unknown }).notebook_path
    if (e.agentId === undefined && TOUCH_TOOLS.has(String(e.tool)) && typeof path === 'string' && path && ran.deny === undefined) {
      const file = normalizePath(path)
      await update($, lastFile, () => file)
    }
    return ran
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Button, Text } = $.ui.resolve(e)
    const view = await read($, paneView)
    const figures = await read($, pressure)
    const hidden = await read($, bandHidden)
    const now = Date.now()

    const rows: RenderElement[] = []
    let n = 0
    const line = (text: string, style: { bold?: boolean; dimColor?: boolean; color?: string } = {}) =>
      rows.push(
        <Text key={`l${n++}`} wrap="wrap" {...style}>
          {text}
        </Text>,
      )

    if (figures) line(`ctx ${figures.percent ?? '?'}% · ${ageText(figures.checkpointCreated, now)}${figures.inBand ? ' · checkpoint now' : ''}`, { dimColor: true })

    if (!view) {
      line('Nothing read yet: press Refresh.', { dimColor: true })
    } else {
      const c = view.checkpoint
      line('Checkpoint', { bold: true })
      if (!c) {
        line('  none in the ring', { dimColor: true })
      } else {
        line(`  ${c.kind ?? 'auto'} · ${ageText(c.created, now).replace('ckpt ', '')} ago${c.task_id ? ` · ${c.task_id}` : ''}${c.project_path ? ` · ${c.project_path}` : ''}`, { dimColor: true })
        if (c.task_description) line(`  Task: ${c.task_description}`)
        if (c.current_step) line(`  Current step: ${c.current_step}`)
        if (c.completed.length) {
          line(`  Completed (${c.completed.length}):`)
          for (const s of c.completed) line(`    - ${s}`)
        }
        if (c.pending.length) {
          line(`  Pending (${c.pending.length}):`)
          for (const s of c.pending) line(`    - ${s}`)
        }
        if (c.files.length) line(`  Files: ${c.files.join(', ')}`)
        if (c.warnings.length) {
          line('  Warnings:')
          for (const s of c.warnings) line(`    ! ${s}`)
        }
        if (c.context_needed.length) {
          line('  Context needed:')
          for (const s of c.context_needed) line(`    ! ${s}`)
        }
        if (c.handoff) line(`  Handoff note: ${c.handoff}`)
        if (c.goal) line(`  Goal: ${c.goal}`)
      }

      line(`Rules (${view.rules.length}${view.project ? `, ${view.project}` : ''})`, { bold: true })
      if (!view.rules.length) line('  none', { dimColor: true })
      for (const r of view.rules) line(`  [${r.id}] ${r.content}`)

      line(view.file ? `Mistakes for ${view.file} (${view.mistakes.length})` : 'Mistakes', { bold: true })
      if (!view.file) line('  no file touched yet this session', { dimColor: true })
      else if (!view.mistakes.length) line('  none', { dimColor: true })
      for (const m of view.mistakes) line(`  [${m.id}] ${m.content}`)
    }

    return (
      <Box key="windvane-pane" flexDirection="column">
        <Box key="windvane-actions" flexDirection="row">
          <Button key="windvane-refresh" label="Refresh" onPress={() => refresh($)} />
          {hidden && <Button key="windvane-show-band" label="Show band" onPress={() => showBand($)} />}
        </Box>
        {rows}
      </Box>
    )
  })
}

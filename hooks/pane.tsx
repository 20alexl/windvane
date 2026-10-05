// windvane: /windvane opens a pane with what windvane holds for this
// session, read from the store's own files:
//
// - the newest checkpoint record whole (the ring register.ts reads for the
//   status line): task, current step, completed, pending, files, warnings,
//   context needed, handoff note, goal;
// - the project's rules (the cwd's registered project and the ancestors it
//   inherits from, as the engine's project memory loader walks them);
// - the mistakes for the file the model last touched (Edit, Write, Read,
//   noted as the call is made), matched as the pre-edit check matches them.
//
// The pane reads the store when it opens and when Refresh is pressed.
// register.ts hooks the command, the tool calls that name the file and the
// pane's render, keeps the view in $.state and hands it here to draw; this
// module never holds `$`.
import type { Elements, RenderElement } from 'claude-code'

import type { PaneView, Pressure } from '../types'
import type { Host } from './engine'
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

// The elements a drawing takes: the surface's table, as $.ui.resolve(e)
// hands it out.
export type Draw = Pick<Elements['terminal'], 'Box' | 'Text' | 'Button'>

// The tools whose file_path names the file the model last touched.
export const TOUCH_TOOLS = new Set(['Edit', 'Write', 'Read', 'MultiEdit', 'NotebookEdit'])
// The body rows asked for while the pane sits inline above the prompt: the
// summary, the checkpoint and the first rules without scrolling.
export const PANE_ROWS = 24
// The label column of the checkpoint's rows, and how many items of a list
// are shown before "and N more".
const LABEL = 11
const LIST_ROWS = 6

function oneLine(text: string): string {
  return text.replace(/\s+/g, ' ').trim()
}

function clip(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, Math.max(0, max - 1))}…` : text
}

function basename(path: string): string {
  const parts = path.replace(/\\/g, '/').replace(/\/+$/, '').split('/')
  return parts[parts.length - 1] || path
}

function count(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? '' : 's'}`
}

// Registered at session.start by register.ts.
export const PANE_COMMAND = {
  name: 'windvane',
  description: "Show windvane's checkpoint, rules and file mistakes in a pane",
}

function ioOf(host: Host): Io {
  return { read: p => host.read(p), exists: p => host.exists(p) }
}

function list(...values: (string[] | undefined)[]): string[] {
  for (const v of values) if (Array.isArray(v) && v.length) return v.filter(s => typeof s === 'string' && s !== '')
  return []
}

// The pane's body, from the store as it stands; `file` is the one the model
// last touched, when any.
export async function loadView(host: Host, file: string | undefined): Promise<PaneView> {
  const io = ioOf(host)
  const store = await host.store()
  const manifest = await readManifest(io, store)
  const cwd = normalizePath(await host.cwd())
  const root = normalizePath(await host.root())

  const rec = await readLatest(io, ringsFor(manifest, store, cwd, root), store)
  const chain = projectChain(manifest, cwd)
  const rules = rulesOf(await readEntries(io, store, chain))

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

// What the pane draws from: the view as loaded (null before the first
// read), the status line's figures, whether the band is hidden (its Show
// button then appears), the body's width in columns and the time.
export type PaneData = {
  view: PaneView | null
  figures: Pressure | null
  hidden: boolean
  bodyColumns: number | undefined
  nowMs: number
}

// The buttons' work, done by register.ts: a fresh read of the store, and
// the band shown again.
export type PaneActions = { refresh: () => void; showBand: () => void }

// The pane's tree.
export function drawPane(ui: Draw, data: PaneData, act: PaneActions): RenderElement {
  const { Box, Button, Text } = ui
  const { view, figures, hidden, nowMs: now } = data

  // The body's width, so prose that may take two rows is cut after them;
  // every other row is cut at the edge by the surface, with an ellipsis.
  const width = Math.max(24, data.bodyColumns || 80)
  const rows: RenderElement[] = []
  let n = 0
  type Style = { bold?: boolean; dimColor?: boolean }
  const row = (text: string, style: Style = {}) =>
    rows.push(
      <Text key={`l${n++}`} wrap="truncate-end" {...style}>
        {text}
      </Text>,
    )
  const gap = () => row(' ')
  const pad = (label: string) => label.padEnd(LABEL)
  // A labelled row of prose: up to two rows, then an ellipsis.
  const prose = (label: string, text: string) =>
    rows.push(
      <Text key={`l${n++}`} wrap="wrap">
        {`  ${pad(label)}${clip(oneLine(text), 2 * width - LABEL - 4)}`}
      </Text>,
    )
  // A labelled list: the count in the label, one item per row, the first
  // LIST_ROWS of them.
  const items = (label: string, list: string[]) => {
    row(`  ${pad(`${label} ${list.length}`)}${oneLine(list[0] ?? '')}`)
    for (const s of list.slice(1, LIST_ROWS)) row(`  ${pad('')}${oneLine(s)}`)
    if (list.length > LIST_ROWS) row(`  ${pad('')}and ${list.length - LIST_ROWS} more`, { dimColor: true })
  }

  // One row of figures: the fill, the checkpoint age, the band, and what
  // the pane holds below.
  const summary = [
    figures ? `ctx ${figures.percent ?? '?'}%` : undefined,
    figures ? ageText(figures.checkpointCreated, now) : undefined,
    figures?.inBand ? 'checkpoint now' : undefined,
    view ? count(view.rules.length, 'rule') : undefined,
    view?.file ? `${count(view.mistakes.length, 'mistake')} for ${basename(view.file)}` : undefined,
  ].filter((s): s is string => s !== undefined)
  if (summary.length) row(summary.join(' · '), { dimColor: true })

  if (!view) {
    row('Nothing read yet: press Refresh.', { dimColor: true })
  } else {
    const c = view.checkpoint
    gap()
    if (!c) {
      row('Checkpoint · none in the ring', { bold: true })
    } else {
      const meta = [c.kind ?? 'auto', `${ageText(c.created, now).replace('ckpt ', '')} ago`, c.task_id, c.project_path ? basename(c.project_path) : undefined]
      row(`Checkpoint · ${meta.filter(Boolean).join(' · ')}`, { bold: true })
      if (c.task_description) prose('Task', c.task_description)
      if (c.current_step) prose('Step', c.current_step)
      if (c.completed.length) items('Done', c.completed)
      if (c.pending.length) items('Pending', c.pending)
      if (c.files.length) row(`  ${pad(`Files ${c.files.length}`)}${c.files.map(basename).join(', ')}`)
      if (c.warnings.length) items('Warnings', c.warnings)
      if (c.context_needed.length) items('Needed', c.context_needed)
      if (c.handoff) prose('Handoff', c.handoff)
      if (c.goal) prose('Goal', c.goal)
    }

    gap()
    row(`Rules · ${view.rules.length}${view.project ? ` · ${basename(view.project)}` : ''}`, { bold: true })
    if (!view.rules.length) row('  none', { dimColor: true })
    for (const r of view.rules) row(`  [${r.id}] ${oneLine(r.content)}`)

    gap()
    row(view.file ? `Mistakes · ${view.mistakes.length} · ${basename(view.file)}` : 'Mistakes', { bold: true })
    if (!view.file) row('  no file touched yet this session', { dimColor: true })
    else if (!view.mistakes.length) row('  none', { dimColor: true })
    for (const m of view.mistakes) row(`  [${m.id}] ${oneLine(m.content)}`)
  }

  return (
    <Box key="windvane-pane" flexDirection="column">
      <Box key="windvane-actions" flexDirection="row">
        <Button key="windvane-refresh" label="Refresh" onPress={act.refresh} />
        {hidden && <Button key="windvane-show-band" label="Show band" onPress={act.showBand} />}
      </Box>
      {rows}
    </Box>
  )
}

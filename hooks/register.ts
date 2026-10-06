// windvane's hooks module: the session keeps its own state.
//
// The mirror. windvane's hooks read the context fill from
//   sessions/<sid>.ctx.json. This module writes it every 10 s from
//   $.session.usage(), plus a sessions/<sid>.mod marker that keeps
//   statusline writers out.
// The status line: "windvane ctx 51% · ckpt 12m", the checkpoint age read
//   from the project's ring (latest_handoff.json). The status_segment
//   option set to false hides it; the band and the pane stay.
// Compaction at windvane's point, after the save. The engine's pressure
//   bands are mirrored here: once the fill is inside the checkpoint band
//   AND a deliberate checkpoint save has landed since the band was entered,
//   the next turn boundary compacts. Compaction then happens with the state
//   banked, at windvane's number, instead of at Claude Code's trigger with
//   whatever happened to be saved. After a compaction windvane started, one
//   prompt of windvane's resumes the work from the checkpoint
//   (continue_after_compact); the SessionStart(compact) banner carries the
//   rules and the checkpoint for that one, since a plugin's own
//   session.compact hooks do not run for a compaction it starts.
//
// Every hook of the plugin is registered in this file, and this is the only
// file that holds `$`. Each other piece keeps its logic in its own module
// and takes a Host (engine.ts), a handful of closures over `$` built by
// hostOf below: the band above the prompt (band.tsx), the /windvane pane
// (pane.tsx), /remember (remember.ts), /windvane-strict (strict.ts),
// /windvane-export (export.ts), /windvane-import (import.ts), tool results
// trimmed and secrets redacted at the door (door.ts), the per-project token
// and cost ledger with /windvane-cost (ledger.ts), the rules and file
// mistakes at the head of every subagent's prompt (agents.ts), the
// compacted conversation carrying the rules and the checkpoint (compact.ts),
// windvane's command hooks answered by the daemon over loopback HTTP where
// nothing else hooks the event (bridge.ts), and the tools the model calls
// (tools.ts). The store reads they share live in ring.ts, the options in
// engine.ts. The $.state values the band and the pane draw from are
// declared here (../types/index.d.ts), read and written here, and handed to
// the drawings as plain values.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, SessionMessage, TurnCompleteInput } from 'claude-code'

import { briefFor } from './agents'
import { BAND_HIDDEN_KEY, drawBand, parseWindvane, textOf } from './band'
import { bridgeDecision } from './bridge'
import { compactBrief } from './compact'
import { readBudget, rewrite } from './door'
import { PLUGIN, VERSION, engineEnv, pythonOf, settingsOf, type EarlyCompaction, type Host, type Settings } from './engine'
import { EXPORT_COMMAND, exportProject } from './export'
import { IMPORT_COMMAND, importStore } from './import'
import { LEDGER_COMMAND, add, asEntry, costBaseline, ledgerKey, localDay, report, turnEntry } from './ledger'
import { PANE_COMMAND, PANE_ROWS, TOUCH_TOOLS, drawPane, loadView } from './pane'
import { REMEMBER_COMMAND, remember } from './remember'
import {
  CHECK_TIMEOUT_MS,
  INSTALL_TIMEOUT_MS,
  SEMANTIC_OFFER_KEY,
  checkArgv,
  extraInstalledFrom,
  installArgv,
  offerDue,
  offerFor,
  recordOf,
  rowOnFrom,
} from './setup'
import { ageText, normalizePath, readLatest, readManifest, ringsFor, storePath } from './ring'
import { STRICT_COMMAND, seedStrict } from './strict'
import { TOOL_SPECS, serve } from './tools'
import type { Io } from './ring'

// $.state values the band and the pane draw from (../types/index.d.ts).
const lastRead = atom({ plugin: 'windvane', key: 'read' } as const, null)
const pressure = atom({ plugin: 'windvane', key: 'pressure' } as const, null)
const bandHidden = atom({ plugin: 'windvane', key: 'bandHidden' } as const, false)
const lastFile = atom({ plugin: 'windvane', key: 'lastFile' } as const, null)
const paneView = atom({ plugin: 'windvane', key: 'pane' } as const, null)

const MIRROR_EVERY_MS = 10_000

// The prompt that resumes the work after a compaction windvane started
// (continue_after_compact). The engine runs it as a turn of its own once the
// session is idle, framed under the plugin's name. The rules and the
// checkpoint reach the model beside it, in the SessionStart(compact) banner:
// a compaction a plugin starts runs beneath that plugin's own hooks, so
// compact.ts cannot place them in the conversation for this one.
const CONTINUE_TEXT =
  'The conversation was compacted with the checkpoint banked. The rules and the checkpoint are in the session-start brief beside this message. Continue from the checkpoint: its current step first, then the pending steps. End your reply with what is done and what is next. If the person has already sent a prompt since the compaction, say so in one line and stop.'

// The engine's pressure constants (its config knobs' defaults), mirrored.
// Keep in step with the engine.
const OUTPUT_RESERVE = 32_000
const CHECKPOINT_MARGIN = 20_000
const CHECKPOINT_MARGIN_SMALL = 10_000
const SMALL_WINDOW = 200_000
const HEADSUP_FRACTION = 0.1
const DEFAULT_COMPACT_1M = 967_000

type Context = { percent?: number; tokens?: number; window: number }

type Mirror = {
  session_id: string
  ts: number
  source: 'mod'
  plugin: string
  total_input_tokens?: number
  context_window_size: number
  used_percentage?: number
  model_id: string
  model_name: string
  total_cost_usd?: number
  five_hour_pct?: number
  five_hour_resets_at?: number
  seven_day_pct?: number
  seven_day_resets_at?: number
  // Every rate-limit window the session reports, by kind (five_hour,
  // seven_day, a model-specific weekly window, ...); the flat keys above
  // stay for older readers.
  rate_limits: Record<string, { pct?: number; resets_at?: number }>
  // The early_compaction row opened the checkpoint band below the engine's
  // own margin: the engine's nudge asks for the checkpoint at the end of the
  // step from this (not NOW, the trigger being far), with the row's value as
  // the reason.
  early_band?: string
  // The compaction window the mod measured against (the session's
  // rawMaxTokens, or the default point), so a store can be read when the band
  // did not open where it was expected. The engine resolves its own.
  compaction_point?: number
  // The fill is left out: the reading still shows the pre-compaction size
  // (see staleTokens in register).
  stale_after_compaction?: true
}

// usage.rateLimits as the mirror's rate_limits dict, one entry per kind.
function rateLimitsOf(limits: readonly { kind: string; percentUsed?: number; resetsAt?: string }[]): Mirror['rate_limits'] {
  const out: Mirror['rate_limits'] = {}
  for (const r of limits) out[r.kind] = { pct: r.percentUsed, resets_at: epochSeconds(r.resetsAt) }
  return out
}

function thresholds(window: number, point: number) {
  const margin = window <= SMALL_WINDOW ? CHECKPOINT_MARGIN_SMALL : CHECKPOINT_MARGIN
  const triggerAt = Math.floor(point - OUTPUT_RESERVE)
  const checkpointAt = Math.floor(triggerAt - margin)
  let headsupAt = Math.floor(point - HEADSUP_FRACTION * window)
  if (headsupAt >= checkpointAt) headsupAt = Math.floor(checkpointAt - (HEADSUP_FRACTION * window) / 2)
  return { headsupAt, checkpointAt, triggerAt }
}

function defaultPoint(window: number): number {
  return window > SMALL_WINDOW ? Math.min(DEFAULT_COMPACT_1M, window) : window
}

// The checkpoint band's state: when it was entered ($.clock's ms; undefined
// outside it), whether it is open below the engine's margin for the
// early_compaction row alone, what the last counted main turn cost, and the
// compaction point the last judgement measured against.
type BandState = { enteredAt?: number; early: boolean; lastTurnCostUsd?: number; point?: number }

// The band bookkeeping, against Claude Code's own compaction window: the
// band opens at the engine's margin above the trigger, or earlier when the
// early_compaction row's fill (a share of that window, as /context counts
// it) or turn cost is reached. A top-level function because $ is followed
// only into one.
async function updateBand($: EngineInterface, usage: { context: Context }, early: EarlyCompaction | undefined, band: BandState): Promise<void> {
  const tokens = tokensOf(usage.context)
  const point = (await $.session.usage({ breakdown: 'summary' })).context.breakdown?.rawMaxTokens
    ?? defaultPoint(usage.context.window)
  band.point = point
  const th = thresholds(usage.context.window, point)
  const earlyHit = early !== undefined && tokens !== undefined && (
    (early.percent !== undefined && tokens * 100 >= point * early.percent)
    || (early.usd !== undefined && band.lastTurnCostUsd !== undefined && band.lastTurnCostUsd >= early.usd))
  if (tokens !== undefined && (tokens >= th.checkpointAt || earlyHit)) {
    if (band.enteredAt === undefined) {
      band.enteredAt = await $.clock.now()
      band.early = tokens < th.checkpointAt
    }
  } else {
    band.enteredAt = undefined
    band.early = false
  }
}

function epochSeconds(iso?: string): number | undefined {
  if (!iso) return undefined
  const ms = Date.parse(iso)
  return Number.isFinite(ms) ? ms / 1000 : undefined
}

// The fill as a share of the compaction window (the figure /context shows),
// when the point is known; of the model's window otherwise.
function percentOf(context: Context, point?: number): number | undefined {
  const tokens = tokensOf(context)
  if (tokens !== undefined && point !== undefined && point > 0) return Math.round((100 * tokens) / point)
  if (context.percent !== undefined) return Math.round(context.percent)
  if (tokens !== undefined && context.window > 0) return Math.round((100 * tokens) / context.window)
  return undefined
}

function tokensOf(context: Context): number | undefined {
  if (context.tokens !== undefined) return context.tokens
  if (context.percent !== undefined && context.window > 0) return Math.round((context.percent / 100) * context.window)
  return undefined
}

// The fill as the session reports it now; undefined when it cannot be read.
async function fillOf($: EngineInterface): Promise<number | undefined> {
  try {
    return tokensOf((await $.session.usage()).context)
  } catch {
    return undefined
  }
}

// The engine interface as the other modules take it (Host, engine.ts):
// closures over `$`, each call on `$` spelled here. The environment is read
// here by name; a module asks for the store or the interpreter.
function hostOf($: EngineInterface): Host {
  return {
    pluginRoot: $.plugin.root,
    store: async () => storePath(await $.env.get('WINDVANE_DIR'), await $.env.get('USERPROFILE'), await $.env.get('HOME')),
    python: async configured => pythonOf(await $.env.get('WINDVANE_PYTHON'), configured),
    sessionId: () => $.session.id(),
    cwd: () => $.session.cwd(),
    root: () => $.session.root(),
    now: () => $.clock.now(),
    after: (ms, fn) => $.clock.after(ms, fn),
    exists: path => $.fs.exists(path),
    read: async path => String(await $.fs.read(path)),
    write: async (path, text) => {
      await $.fs.write(path, text)
    },
    run: (argv, init) => $.process.run(argv, init),
    post: (port, token, path, body) =>
      $.http.fetch(`http://127.0.0.1:${port}/${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Windvane-Hook': '1', 'X-Windvane-Token': token },
        body,
      }),
    storeGet: key => $.store.get(key),
    storeSet: (key, value) => $.store.set(key, value),
    storeKeys: () => $.store.keys(),
    settings: async source => (source === undefined ? await $.settings.read() : await $.settings.read({ source })) as Record<string, unknown>,
    // The env a command hook process inherits that windvane's handlers read
    // per session (the daemon's per-request session env); CLAUDE_PROJECT_DIR
    // is the session root.
    sessionEnv: async () => ({
      CLAUDE_PROJECT_DIR: await $.session.root(),
      CLAUDE_CODE_AUTO_COMPACT_WINDOW: (await $.env.get('CLAUDE_CODE_AUTO_COMPACT_WINDOW')) ?? '',
      CLAUDE_CONFIG_DIR: (await $.env.get('CLAUDE_CONFIG_DIR')) ?? '',
      WINDVANE_AUTONOMY: (await $.env.get('WINDVANE_AUTONOMY')) ?? '',
      WINDVANE_ALERT_COMMAND: (await $.env.get('WINDVANE_ALERT_COMMAND')) ?? '',
      WINDVANE_STRIKE_CAP: (await $.env.get('WINDVANE_STRIKE_CAP')) ?? '',
      WINDVANE_GOAL_TURN_CAP: (await $.env.get('WINDVANE_GOAL_TURN_CAP')) ?? '',
      WINDVANE_LIVE_MINE: (await $.env.get('WINDVANE_LIVE_MINE')) ?? '',
    }),
    configDir: async () => {
      const home = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME')) ?? ''
      return ((await $.env.get('CLAUDE_CONFIG_DIR')) || `${home}/.claude`).replace(/\\/g, '/')
    },
    resultBudgetEnv: () => $.env.get('WINDVANE_RESULT_BUDGET'),
    log: text => $.ui.log(text),
    debug: text => $.ui.log(text, { to: 'debug' }),
  }
}

function ioOf($: EngineInterface): Io {
  return { read: p => $.fs.read(p) as Promise<string>, exists: p => $.fs.exists(p) }
}

// The first-run offer of the semantic tier. setup.ts holds the decisions;
// the $ work is here, as for the other pieces. Asked at session.start and
// never awaited there, so the dialog never delays the start. A dismissed
// dialog records nothing and the next session asks again.
async function offerSemantic($: EngineInterface, e: { isInteractive: boolean }, settings: Settings): Promise<void> {
  if (!e.isInteractive || !offerDue(await $.store.get(SEMANTIC_OFFER_KEY), Date.now())) return
  // WINDVANE_SEMANTIC in the environment, on or off, is the person's own
  // decision about the tier: nothing to ask (and a scripted session, the
  // demo take among them, is never interrupted by the dialog).
  if (((await $.env.get('WINDVANE_SEMANTIC')) ?? '').trim() !== '') return
  const python = pythonOf(await $.env.get('WINDVANE_PYTHON'), settings.python)
  const env = engineEnv($.plugin.root, storePath(await $.env.get('WINDVANE_DIR'), await $.env.get('USERPROFILE'), await $.env.get('HOME')))
  const rowOn = rowOnFrom(await $.config.list())
  let installed = false
  try {
    installed = extraInstalledFrom(await $.process.run(checkArgv(python), { env, timeoutMs: CHECK_TIMEOUT_MS }))
  } catch {
    installed = false
  }
  const offer = offerFor(rowOn, installed)
  if (!offer) {
    await $.store.set(SEMANTIC_OFFER_KEY, recordOf('done'))
    return
  }
  let answer: string
  try {
    answer = await $.ui.ask(offer.question, { options: [offer.act, 'Not now', 'Never ask'], header: 'windvane' })
  } catch {
    return
  }
  $.ui.log(`${PLUGIN}: semantic offer answered: ${answer}`, { to: 'debug' })
  if (answer === 'Never ask') {
    await $.store.set(SEMANTIC_OFFER_KEY, recordOf('never'))
    return
  }
  if (answer !== offer.act) {
    await $.store.set(SEMANTIC_OFFER_KEY, recordOf('later'))
    return
  }
  if (!installed) {
    $.ui.toast(`${PLUGIN}: installing the semantic extra; this takes a few minutes`)
    let ok = false
    try {
      const run = await $.process.run(installArgv(python), { env, timeoutMs: INSTALL_TIMEOUT_MS })
      ok = run.exitCode === 0
      if (!ok) $.ui.log(`${PLUGIN}: pip install failed (exit ${run.exitCode}): ${run.stderr.trim().slice(-400)}`)
    } catch (err) {
      $.ui.log(`${PLUGIN}: pip install did not run: ${String(err)}`)
    }
    if (!ok) {
      $.ui.toast(`${PLUGIN}: the semantic extra did not install; see the log, or pip install it yourself`)
      await $.store.set(SEMANTIC_OFFER_KEY, recordOf('later'))
      return
    }
  }
  if (!rowOn) {
    // The row's key as fixed text: the directory reads the call as written.
    const set = await $.config.set({ key: 'windvane.semantic', value: true })
    if (set.deny) {
      $.ui.toast(`${PLUGIN}: the semantic row stayed off: ${set.deny}`)
      await $.store.set(SEMANTIC_OFFER_KEY, recordOf('later'))
      return
    }
  }
  $.ui.toast(`${PLUGIN}: the semantic tier is on; the daemon loads the model on its next start`)
  await $.store.set(SEMANTIC_OFFER_KEY, recordOf('done'))
}

// The UI at session.start: the band's Hide from the last session, and the
// commands. A failure costs that piece alone, never the mirror.
async function startUi($: EngineInterface): Promise<void> {
  try {
    if ((await $.store.get(BAND_HIDDEN_KEY)) === true) await update($, bandHidden, () => true)
  } catch (err) {
    $.ui.log(`${PLUGIN}: band: ${String(err)}`)
  }
  for (const spec of [PANE_COMMAND, REMEMBER_COMMAND, LEDGER_COMMAND, STRICT_COMMAND, EXPORT_COMMAND, IMPORT_COMMAND]) {
    try {
      await $.command.register(spec)
    } catch (err) {
      $.ui.log(`${PLUGIN}: /${spec.name}: ${String(err)}`)
    }
  }
  // The tools the model calls, served by tools.ts.
  for (const spec of TOOL_SPECS) {
    try {
      await $.tool.register(spec)
    } catch (err) {
      $.ui.log(`${PLUGIN}: tool ${spec.name}: ${String(err)}`)
    }
  }
}

// The band's Hide and the pane's Show band: the flag in $.state, and in
// $.store across sessions.
async function hideBand($: EngineInterface): Promise<void> {
  await update($, bandHidden, () => true)
  await $.store.set(BAND_HIDDEN_KEY, true)
}

async function showBand($: EngineInterface): Promise<void> {
  await update($, bandHidden, () => false)
  await $.store.set(BAND_HIDDEN_KEY, false)
}

// The pane's body read from the store afresh (pane.tsx), for the file the
// model last touched.
async function refreshPane($: EngineInterface): Promise<void> {
  const view = await loadView(hostOf($), (await read($, lastFile)) ?? undefined)
  await update($, paneView, () => view)
}

// The ledger's project: the folder the session was opened in. A shell cd
// moves $.session.cwd() for the rest of the session; the ledger stays put.
let ledgerProject = ''

// The ledger at session.start: its project and the cost it counts from.
async function startLedger($: EngineInterface, cwd: string): Promise<void> {
  ledgerProject = normalizePath(cwd)
  costBaseline((await $.session.usage()).cost?.usd)
}

// The ledger at turn.complete: a main-loop turn that counted something,
// added to the project's entry for the local day. Answers what the turn
// cost (the session's priced cost across it), or undefined for a turn that
// counted nothing.
async function recordTurn($: EngineInterface, e: TurnCompleteInput): Promise<number | undefined> {
  if (e.agentId !== undefined || e.usage === undefined) return undefined
  const entry = turnEntry(e.usage, (await $.session.usage()).cost?.usd)
  const key = ledgerKey(ledgerProject || (await $.session.cwd()), localDay(await $.clock.now()))
  await $.store.set(key, add(asEntry(await $.store.get(key)), entry))
  return entry.cost_usd
}

export const register: Register = (on, options) => {
  // The options are fixed for this load; a change reloads the module.
  const settings = settingsOf(options)

  // Per-load state; a reload starts it over, the files on disk do not.
  let sid = ''
  let store = ''
  let sessions = ''
  let rings: string[] = []
  // Times are $.clock's (ms since the epoch).
  const bandState: BandState = { early: false } // the checkpoint band (updateBand)
  let lastSaveAt: number | undefined // a deliberate checkpoint save that succeeded
  let compactAsked = false // compact_now succeeded this turn
  let compactRequested = false // a compaction is under way
  // The fill as read right after a compaction. Claude Code's usage figures
  // keep the pre-compaction size until the next request records the
  // rewritten conversation's, so a reading equal to this one says nothing:
  // the band stays closed and the mirror carries no fill until the reading
  // changes or a turn completes.
  let staleTokens: number | undefined
  // When the turn that asked for the compaction began, and when the person
  // last submitted a prompt of their own while no turn ran. A prompt the
  // person types while a compaction runs is queued and runs before anything
  // a plugin submits; the continue prompt would then arrive a turn late and
  // stale, so it is skipped when such a prompt has landed since that turn
  // began (its own prompt was submitted before it). A prompt typed over the
  // running turn is another matter: the engine delivers it into that turn
  // (the person saw it answered before the compaction), and one it did not
  // deliver starts the next turn on its own, where the continue prompt's
  // text tells the model to stop in one line.
  let turnBeganAt: number | undefined
  let personPromptAt: number | undefined
  const continueStale = () => turnBeganAt !== undefined && personPromptAt !== undefined && personPromptAt > turnBeganAt + 100
  const early = settings.earlyCompaction
  let doorBudget: number | undefined // the door's budget, read once per load

  // A deliberate save that succeeded: checkpoint(save), or compact_now,
  // which banks the draft and asks for the compaction at the turn boundary.
  // The serving hooks below call this once the engine has answered without
  // an error. The engine carries the arguments at the top level of the event.
  const noteSave = (call: unknown, compactNow: boolean, at: number): void => {
    const { operation, agentId } = call as { operation?: string; agentId?: string }
    if (compactNow || operation === 'save') lastSaveAt = at
    if (compactNow && agentId === undefined) compactAsked = true
  }

  // Inside the band with a save made since the band was entered.
  const savedInBand = () => bandState.enteredAt !== undefined && lastSaveAt !== undefined && lastSaveAt >= bandState.enteredAt

  // The person's own prompts are noted for the continue prompt's sake: the
  // ones typed at the terminal (composer) or sent from the Remote Control
  // bridge while no turn ran (`turnId` names the turn a prompt was typed
  // over; the compaction runs between turns, so a prompt typed during it has
  // none). A prompt typed over a running turn was delivered into it, or
  // starts the next turn by itself, and leaves the note alone; so does a
  // delivery into the running turn (a peer session's message, a task
  // notification, a subagent's prompt), which is not the person continuing
  // the session. A continue of windvane's that is already stale is dropped
  // (a second net under the check made before it is submitted).
  on('prompt.submit', async ($, e, next) => {
    const origin = e.origin as { kind?: string; name?: string } | undefined
    const ours = origin?.kind === 'plugin' && origin.name === PLUGIN
    if (!ours) {
      if ((origin?.kind === 'composer' || origin?.kind === 'bridge') && e.turnId === undefined) personPromptAt = await $.clock.now()
      return next(e)
    }
    if (e.text === CONTINUE_TEXT && continueStale()) return { drop: `${PLUGIN}: the session already continued, so the resume prompt was dropped` }
    return next(e)
  })

  // The band (band.tsx). Every row windvane's hooks hand the model passes
  // session.append with door hook-context; a subagent's rows are its own.
  on('session.append', { door: 'hook-context' }, async ($, e, next) => {
    if (e.agentId === undefined) {
      const reading = parseWindvane(textOf(e.message.content), Date.now())
      if (reading) await update($, lastRead, () => reading)
    }
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey || e.props.view.agentId !== undefined) return next(e)
    const reading = await read($, lastRead)
    if (reading === null || (await read($, bandHidden))) return next(e)
    const figures = await read($, pressure)
    return drawBand($.ui.resolve(e), reading, figures, Date.now(), () => hideBand($))
  })

  // The pane (pane.tsx). /windvane reads the store and opens it: tall enough
  // inline for the summary, the checkpoint and the first rules; the keys go
  // to the pane so the arrows scroll it, and Escape closes it.
  on('command.run', { command: 'windvane' }, async $ => {
    await refreshPane($)
    await $.ui.open({ id: 'windvane', title: 'windvane', rows: PANE_ROWS, focus: true, closeOnEscape: true })
    return { text: 'windvane pane opened.' }
  })

  // The file the model last touched, noted as the call is made (a denied
  // edit still says which file the model is on); a subagent's touches are
  // its own.
  on('tool.call', async ($, e, next) => {
    const path = (e as unknown as { file_path?: unknown; notebook_path?: unknown }).file_path
      ?? (e as unknown as { notebook_path?: unknown }).notebook_path
    if (e.agentId === undefined && TOUCH_TOOLS.has(String(e.tool)) && typeof path === 'string' && path) {
      const file = normalizePath(path)
      await update($, lastFile, () => file)
    }
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: 'windvane' }, async ($, e) => {
    const view = await read($, paneView)
    const figures = await read($, pressure)
    const hidden = await read($, bandHidden)
    const data = { view, figures, hidden, bodyColumns: e.props.bodyColumns, nowMs: Date.now() }
    return drawPane($.ui.resolve(e), data, { refresh: () => refreshPane($), showBand: () => showBand($) })
  })

  // /remember (remember.ts): the selected transcript text, stored as a
  // decision for the session's project.
  on('command.run', { command: 'remember' }, async $ => {
    const selected = await $.ui.selection()
    const text = selected?.text.trim() ?? ''
    if (!text) return { text: 'Nothing is selected. Select text in the transcript, then run /remember.' }
    return { text: await remember(hostOf($), settings.python, text, await read($, lastFile)) }
  })

  // /windvane-strict, /windvane-export, /windvane-import: the engine does
  // the work (strict.ts, export.ts, import.ts).
  on('command.run', { command: 'windvane-strict' }, async $ => ({ text: await seedStrict(hostOf($), settings.python) }))
  on('command.run', { command: 'windvane-export' }, async $ => ({ text: await exportProject(hostOf($), settings.python) }))
  on('command.run', { command: 'windvane-import' }, async $ => ({ text: await importStore(hostOf($), settings.python) }))

  // The door (door.ts): a tool result's text redacted and trimmed before
  // the row is stored and read. A row that needs no change goes on untouched.
  on('session.append', async ($, e, next) => {
    if (e.door !== 'tool-result') return next(e)
    if (doorBudget === undefined) doorBudget = await readBudget(hostOf($), settings.resultBudget)
    const content = rewrite(e.message.content, doorBudget)
    if (content === undefined) return next(e)
    return next({ ...e, message: { ...e.message, content } })
  })

  // /windvane-cost (ledger.ts): the ledger's project is the folder the
  // session was opened in; before session.start the session's cwd stands in.
  on('command.run', { command: 'windvane-cost' }, async $ => ({
    text: await report(hostOf($), ledgerProject || normalizePath(await $.session.cwd())),
  }))

  // The subagents' brief (agents.ts): the Agent call's prompt with the
  // project's rules and the named files' mistakes at its head.
  on('tool.call', { tool: 'Agent' }, async ($, e, next) => {
    const prompt = await briefFor(hostOf($), settings.python, e)
    if (prompt === undefined) return next(e)
    return next({ ...e, prompt })
  })

  // The compacted conversation (compact.ts): every trigger but precompute
  // (the matcher also keeps this hook apart from the matcher-less one
  // below). One user-role message carrying the rules and the checkpoint is
  // placed after the summary (the first message core hands up), ahead of
  // what it kept. A subagent's compaction and a skip pass through.
  on('session.compact', { trigger: ['manual', 'auto', 'plugin'] }, async ($, e, next) => {
    const out = await next(e)
    if (out.messages === undefined || e.agentId !== undefined) return out
    const text = await compactBrief(hostOf($), settings.python)
    if (text === undefined) return out
    const restore: SessionMessage = { role: 'user', text, toolUses: [] }
    const messages = [...out.messages]
    messages.splice(messages.length > 0 ? 1 : 0, 0, restore)
    return { ...out, messages }
  })

  // The bridge (bridge.ts): windvane's command hooks answered by the daemon
  // over loopback where every command hook that would fire is windvane's and
  // served; otherwise the command hooks run exactly as before. One hook per
  // classic event, by name, the event handed whole. PreToolUse has none: it
  // is a permission check, whose hook may only deny, ask or pass the event
  // on, and a bridged check with context and no decision would have to pass
  // it on and run its handlers twice; its command hooks run as before.
  on('classic.UserPromptSubmit', async ($, e, next) => {
    const got = await bridgeDecision(hostOf($), e)
    if ('answer' in got) return { ...got.answer }
    return next(e)
  })
  // SessionStart's answer is the banner alone, named here so the directory
  // reads that the session's first message is left as it is.
  on('classic.SessionStart', async ($, e, next) => {
    const got = await bridgeDecision(hostOf($), e)
    if ('answer' in got) return { additionalContext: got.answer.additionalContext ?? [] }
    return next(e)
  })
  on('classic.Notification', async ($, e, next) => {
    const got = await bridgeDecision(hostOf($), e)
    if ('answer' in got) return { ...got.answer }
    return next(e)
  })
  on('classic.PostToolUse', async ($, e, next) => {
    const got = await bridgeDecision(hostOf($), e)
    if ('answer' in got) return { ...got.answer }
    return next(e)
  })
  on('classic.PostToolUseFailure', async ($, e, next) => {
    const got = await bridgeDecision(hostOf($), e)
    if ('answer' in got) return { ...got.answer }
    return next(e)
  })
  on('classic.PostToolBatch', async ($, e, next) => {
    const got = await bridgeDecision(hostOf($), e)
    if ('answer' in got) return { ...got.answer }
    return next(e)
  })
  on('classic.StopFailure', async ($, e, next) => {
    const got = await bridgeDecision(hostOf($), e)
    if ('answer' in got) return { ...got.answer }
    return next(e)
  })
  on('classic.PreCompact', async ($, e, next) => {
    const got = await bridgeDecision(hostOf($), e)
    if ('answer' in got) return { ...got.answer }
    return next(e)
  })
  on('classic.PostCompact', async ($, e, next) => {
    const got = await bridgeDecision(hostOf($), e)
    if ('answer' in got) return { ...got.answer }
    return next(e)
  })
  on('classic.Stop', async ($, e, next) => {
    const got = await bridgeDecision(hostOf($), e)
    if ('answer' in got) return { ...got.answer }
    return next(e)
  })
  on('classic.SessionEnd', async ($, e, next) => {
    const got = await bridgeDecision(hostOf($), e)
    if ('answer' in got) return { ...got.answer }
    return next(e)
  })

  // The tools the model calls (tools.ts): one matched hook per tool, each
  // matcher naming its tool literally. Each answers for itself: a failure is
  // a deny, else the reply is the result. A checkpoint save or a compact_now
  // that the engine answered is a deliberate save (noteSave).
  on('tool.call', { tool: 'mcp__windvane__checkpoint' }, async ($, e) => {
    const got = await serve(hostOf($), 'checkpoint', e, settings)
    if ('deny' in got) return { deny: got.deny }
    noteSave(e, false, await $.clock.now())
    return { result: got.result }
  })
  on('tool.call', { tool: 'mcp__windvane__compact_now' }, async ($, e) => {
    const got = await serve(hostOf($), 'compact_now', e, settings)
    if ('deny' in got) return { deny: got.deny }
    noteSave(e, true, await $.clock.now())
    return { result: got.result }
  })
  on('tool.call', { tool: 'mcp__windvane__memory' }, async ($, e) => {
    const got = await serve(hostOf($), 'memory', e, settings)
    if ('deny' in got) return { deny: got.deny }
    return { result: got.result }
  })
  on('tool.call', { tool: 'mcp__windvane__log' }, async ($, e) => {
    const got = await serve(hostOf($), 'log', e, settings)
    if ('deny' in got) return { deny: got.deny }
    return { result: got.result }
  })
  on('tool.call', { tool: 'mcp__windvane__mine' }, async ($, e) => {
    const got = await serve(hostOf($), 'mine', e, settings)
    if ('deny' in got) return { deny: got.deny }
    return { result: got.result }
  })
  on('tool.call', { tool: 'mcp__windvane__deps' }, async ($, e) => {
    const got = await serve(hostOf($), 'deps', e, settings)
    if ('deny' in got) return { deny: got.deny }
    return { result: got.result }
  })

  on('session.start', async ($, e, next) => {
    // The other pieces start first, each on its own: a failure there leaves
    // the mirror running, and the mirror's early return leaves them running.
    await startUi($)
    try {
      await startLedger($, e.cwd)
    } catch (err) {
      $.ui.log(`${PLUGIN}: ledger off: ${String(err)}`)
    }
    // The first-run offer of the semantic tier (setup.ts): asked, never
    // awaited, so the dialog never delays the start.
    void offerSemantic($, e, settings).catch(err => $.ui.log(`${PLUGIN}: the semantic offer failed: ${String(err)}`, { to: 'debug' }))

    sid = await $.session.id()
    const model = await $.session.model()

    store = storePath(await $.env.get('WINDVANE_DIR'), await $.env.get('USERPROFILE'), await $.env.get('HOME'))
    sessions = `${store}/sessions`
    const mirrorPath = `${sessions}/${sid}.ctx.json`
    const markerPath = `${sessions}/${sid}.mod`

    // The rings this session can save into, through the store's manifest.
    try {
      const root = normalizePath(await $.session.root())
      const cwd = normalizePath(await $.session.cwd())
      rings = ringsFor(await readManifest(ioOf($), store), store, cwd, root)
    } catch {
      rings = []
    }

    // The engine creates the sessions folder (its session-start hook, within
    // seconds of the first session after an install). Until it exists each
    // tick is skipped and the next one looks again; the mirror is never
    // switched off for the session.
    let sessionsReady = false

    const tick = async () => {
      if (!sessionsReady) {
        if (!(await $.fs.exists(sessions))) return
        sessionsReady = true
      }
      const usage = await $.session.usage()
      const stale = staleTokens !== undefined && tokensOf(usage.context) === staleTokens
      if (stale) {
        bandState.enteredAt = undefined
        bandState.early = false
      } else {
        staleTokens = undefined
        await updateBand($, usage, early, bandState)
      }
      const five = usage.rateLimits.find(r => r.kind === 'five_hour')
      const seven = usage.rateLimits.find(r => r.kind === 'seven_day')
      const rec: Mirror = {
        session_id: sid,
        ts: Date.now() / 1000,
        source: 'mod',
        plugin: PLUGIN,
        total_input_tokens: stale ? undefined : usage.context.tokens,
        context_window_size: usage.context.window,
        used_percentage: stale ? undefined : usage.context.percent,
        model_id: model,
        model_name: model,
        total_cost_usd: usage.cost?.usd,
        five_hour_pct: five?.percentUsed,
        five_hour_resets_at: epochSeconds(five?.resetsAt),
        seven_day_pct: seven?.percentUsed,
        seven_day_resets_at: epochSeconds(seven?.resetsAt),
        rate_limits: rateLimitsOf(usage.rateLimits),
      }
      if (bandState.early && early !== undefined) rec.early_band = early.label
      if (bandState.point !== undefined) rec.compaction_point = bandState.point
      if (stale) rec.stale_after_compaction = true
      await $.fs.write(mirrorPath, JSON.stringify(rec))
      await $.fs.write(markerPath, JSON.stringify({ plugin: PLUGIN, version: VERSION, ts: rec.ts }))

      const latest = await readLatest(ioOf($), rings, store)
      const pct = stale ? undefined : percentOf(usage.context, bandState.point)
      const band = bandState.enteredAt !== undefined && !savedInBand() ? ' · checkpoint now' : ''
      if (settings.statusSegment) $.ui.status(`${PLUGIN} ctx ${pct === undefined ? '?' : pct + '%'} · ${ageText(latest?.created)}${band}`)
      // The same figures for the band above the prompt.
      await update($, pressure, () => ({ percent: pct, checkpointCreated: latest?.created, inBand: band !== '' }))
    }

    await tick()
    $.clock.every(MIRROR_EVERY_MS, () => {
      void tick()
    })
    return next(e)
  })

  // A compaction is over: the band opens again from a fresh reading, and the
  // fill reads as it did before the compaction until the next request
  // (staleTokens, read by the caller through fillOf).
  const noteCompacted = (fill: number | undefined): void => {
    staleTokens = fill
    bandState.enteredAt = undefined
    bandState.early = false
    compactAsked = false
    compactRequested = false
  }

  // The turn boundary: compact_now asked for it, or the fill is inside the
  // band with a checkpoint banked since the band was entered; compact now,
  // at windvane's number. The compaction runs inside this hook, where the
  // engine says it belongs: the conversation compacts between turns, and a
  // compaction left to a timer lost the race against a prompt queued for
  // the next turn ("a turn is running"). The hook's budget stops while a $
  // call is in flight, so the compaction costs it nothing.
  on('turn.complete', async ($, e, next) => {
    if ((e as unknown as { agentId?: string }).agentId !== undefined) return next(e)
    // A turn ended: the next reading is the rewritten conversation's.
    staleTokens = undefined
    // Decided before the ledger judges the band again below: the band a
    // costly turn opened at its end is what the save in this turn answered.
    const compacting = (compactAsked || savedInBand()) && !compactRequested
    const why = bandState.early && early !== undefined ? ` (early_compaction ${early.label})` : ''
    if (compacting) compactRequested = true
    const done = await next(e)
    // The ledger counts the turn once it is done. What the turn cost is the
    // early_compaction row's dollar signal, so the band is judged again here
    // and a save in the next turn counts.
    try {
      const cost = await recordTurn($, e)
      if (cost !== undefined) bandState.lastTurnCostUsd = cost
      if (early?.usd !== undefined && cost !== undefined) await updateBand($, await $.session.usage(), early, bandState)
    } catch (err) {
      $.ui.log(`${PLUGIN}: ledger: ${String(err)}`)
    }
    if (compacting) {
      turnBeganAt = (await $.clock.now()) - Math.max(0, e.durationMs ?? 0)
      const tokens = await fillOf($)
      $.ui.toast(`${PLUGIN}: checkpoint banked, compacting at ${Math.round((tokens ?? 0) / 1000)}K${why}`)
      let compacted = false
      try {
        const out = await $.session.compact()
        compacted = !('skip' in out && out.skip)
        // Done, or vetoed by a hook: either way this band is answered.
        noteCompacted(compacted ? await fillOf($) : undefined)
      } catch (err) {
        // Refused (a turn had begun after all) or failed: the save still
        // stands in the band, so the next turn end asks again. Nothing is
        // lost, and nothing is said in the transcript.
        compactRequested = false
        $.ui.log(`${PLUGIN}: compaction not done, retried at the next turn end: ${String(err)}`, { to: 'debug' })
      }
      if (compacted && settings.continueAfterCompact) {
        // The resume prompt once the hook has returned: a prompt the person
        // queued during the compaction runs first, and that case is read
        // where the prompt is submitted.
        $.clock.after(0, () => {
          if (continueStale()) {
            $.ui.log(`${PLUGIN}: the person continued the session during the compaction; no resume prompt`)
            return
          }
          void $.prompt.submit({ text: CONTINUE_TEXT }).catch(err => {
            $.ui.log(`${PLUGIN}: the continue after the compaction failed: ${String(err)}`)
          })
        })
      }
    }
    return done
  })

  // Any compaction that stood, the engine's or the person's, opens a new
  // cycle once it is done (a vetoed or refused one leaves the band as it
  // was, so the save still counts at the next turn end).
  on('session.compact', async ($, e, next) => {
    const out = await next(e)
    if (e.trigger !== 'precompute' && !('skip' in out && out.skip)) noteCompacted(await fillOf($))
    return out
  })
}

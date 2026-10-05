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
// Each other piece lives in its own module, registered from here: the band
// above the prompt (band.tsx), the /windvane pane (pane.tsx), /remember
// (remember.ts), /windvane-strict (strict.ts), /windvane-export (export.ts),
// /windvane-import (import.ts), tool results trimmed and secrets redacted at
// the door (door.ts), the per-project token and cost ledger with
// /windvane-cost (ledger.ts), the rules and file mistakes at the head of
// every subagent's prompt (agents.ts), the compacted conversation carrying
// the rules and the checkpoint (compact.ts), windvane's command hooks
// answered by the daemon over loopback HTTP where nothing else hooks the
// event (bridge.ts), and the tools the model calls (tools.ts). The store
// reads they share live in ring.ts, the engine's interpreter and the options
// in engine.ts. The engine takes one unmatched hook per event per plugin and
// follows $ only within one file, so their session.start and turn.complete
// work is done here (startUi, startLedger, recordTurn), and each module gets
// the options it needs as plain values.
import { atom, update } from 'claude-code'
import type { EngineInterface, Register, ToolCallInput, ToolCallResult, TurnCompleteInput } from 'claude-code'

import { registerAgents } from './agents'
import { BAND_HIDDEN_KEY, registerBand } from './band'
import { registerBridge } from './bridge'
import { registerCompact } from './compact'
import { registerDoor } from './door'
import { PLUGIN, VERSION, engineEnv, pythonOf, settingsOf, type EarlyCompaction, type Settings } from './engine'
import { EXPORT_COMMAND, registerExport } from './export'
import { IMPORT_COMMAND, registerImport } from './import'
import { LEDGER_COMMAND, add, asEntry, costBaseline, ledgerKey, localDay, registerLedger, turnEntry } from './ledger'
import { PANE_COMMAND, registerPane } from './pane'
import { REMEMBER_COMMAND, registerRemember } from './remember'
import {
  CHECK_TIMEOUT_MS,
  INSTALL_TIMEOUT_MS,
  SEMANTIC_OFFER_KEY,
  SEMANTIC_ROW,
  checkArgv,
  extraInstalledFrom,
  installArgv,
  offerDue,
  offerFor,
  recordOf,
  rowOnFrom,
} from './setup'
import { ageText, normalizePath, readLatest, readManifest, ringsFor, storePath } from './ring'
import { STRICT_COMMAND, registerStrict } from './strict'
import { TOOL_NAMES, TOOL_SPECS, registerTools } from './tools'
import type { Io } from './ring'

// $.state values the band and the pane draw from (../types/index.d.ts).
const pressure = atom({ plugin: 'windvane', key: 'pressure' } as const, null)
const bandHidden = atom({ plugin: 'windvane', key: 'bandHidden' } as const, false)

const MIRROR_EVERY_MS = 10_000
const COMPACT_DELAY_MS = 250

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
  // own margin: the engine's nudge says CHECKPOINT NOW from this, with the
  // row's value as the reason.
  early_band?: string
  // The compaction window the mod measured against (the session's
  // rawMaxTokens, or the default point), so a store can be read when the band
  // did not open where it was expected. The engine resolves its own.
  compaction_point?: number
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
  const on = rowOnFrom(await $.config.list())
  let installed = false
  try {
    installed = extraInstalledFrom(await $.process.run(checkArgv(python), { env, timeoutMs: CHECK_TIMEOUT_MS }))
  } catch {
    installed = false
  }
  const offer = offerFor(on, installed)
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
  if (!on) {
    const set = await $.config.set({ key: SEMANTIC_ROW, value: true })
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
  let compactRequested = false // a compaction is scheduled
  // When the turn that asked for the compaction began, and when the last
  // prompt that was not windvane's own was submitted. A prompt the person
  // types while a compaction runs is queued and runs before anything a
  // plugin submits; the continue prompt would then arrive a turn late and
  // stale, so it is skipped when such a prompt has landed since that turn
  // began (its own prompt was submitted before it).
  let turnBeganAt: number | undefined
  let personPromptAt: number | undefined
  const continueStale = () => turnBeganAt !== undefined && personPromptAt !== undefined && personPromptAt > turnBeganAt + 100
  const early = settings.earlyCompaction

  // A deliberate save that succeeded: checkpoint(save), or
  // compact_now, which banks the draft and asks for the compaction at the
  // turn boundary. tools.ts serves both and answers a failure with a deny;
  // these hooks are registered ahead of it so they sit above it and see its
  // answer. The engine carries the arguments at the top level of the event.
  const noteSave = (e: ToolCallInput, ran: ToolCallResult, compactNow: boolean, at: number): ToolCallResult => {
    const op = (e as unknown as { operation?: string }).operation
    const ok = ran.deny === undefined && ran.isError !== true
    if (ok && (compactNow || op === 'save')) lastSaveAt = at
    if (ok && compactNow && (e as unknown as { agentId?: string }).agentId === undefined) compactAsked = true
    return ran
  }
  on('tool.call', { tool: TOOL_NAMES.checkpoint }, async ($, e, next) => {
    const ran = await next(e)
    return noteSave(e, ran, false, await $.clock.now())
  })
  on('tool.call', { tool: TOOL_NAMES.compact_now }, async ($, e, next) => {
    const ran = await next(e)
    return noteSave(e, ran, true, await $.clock.now())
  })

  // Inside the band with a save made since the band was entered.
  const savedInBand = () => bandState.enteredAt !== undefined && lastSaveAt !== undefined && lastSaveAt >= bandState.enteredAt

  // Every prompt but windvane's own is noted for the continue prompt's sake;
  // a continue of windvane's that is already stale is dropped (a second net
  // under the check made before it is submitted).
  on('prompt.submit', async ($, e, next) => {
    const origin = e.origin as { kind?: string; name?: string } | undefined
    const ours = origin?.kind === 'plugin' && origin.name === PLUGIN
    if (!ours) {
      personPromptAt = await $.clock.now()
      return next(e)
    }
    if (e.text === CONTINUE_TEXT && continueStale()) return { drop: `${PLUGIN}: the session already continued, so the resume prompt was dropped` }
    return next(e)
  })

  registerBand(on)
  registerPane(on)
  registerRemember(on, settings)
  registerStrict(on, settings)
  registerExport(on, settings)
  registerImport(on, settings)
  registerDoor(on, settings)
  registerLedger(on, () => ledgerProject)
  registerAgents(on, settings)
  registerCompact(on, settings)
  registerBridge(on)
  registerTools(on, settings)

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
      await updateBand($, usage, early, bandState)
      const five = usage.rateLimits.find(r => r.kind === 'five_hour')
      const seven = usage.rateLimits.find(r => r.kind === 'seven_day')
      const rec: Mirror = {
        session_id: sid,
        ts: Date.now() / 1000,
        source: 'mod',
        plugin: PLUGIN,
        total_input_tokens: usage.context.tokens,
        context_window_size: usage.context.window,
        used_percentage: usage.context.percent,
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
      await $.fs.write(mirrorPath, JSON.stringify(rec))
      await $.fs.write(markerPath, JSON.stringify({ plugin: PLUGIN, version: VERSION, ts: rec.ts }))

      const latest = await readLatest(ioOf($), rings, store)
      const pct = percentOf(usage.context, bandState.point)
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

  // The turn boundary: compact_now asked for it, or the fill is inside the
  // band with a checkpoint banked since the band was entered; compact now,
  // at windvane's number. The compaction runs from a timer so the hook's own
  // budget is not spent on it, and once the turn is over ($.session.compact
  // rejects while a turn runs).
  on('turn.complete', async ($, e, next) => {
    if ((e as unknown as { agentId?: string }).agentId !== undefined) return next(e)
    if ((compactAsked || savedInBand()) && !compactRequested) {
      compactRequested = true
      compactAsked = false
      turnBeganAt = (await $.clock.now()) - Math.max(0, e.durationMs ?? 0)
      const usage = await $.session.usage()
      const tokens = tokensOf(usage.context)
      const why = bandState.early && early !== undefined ? ` (early_compaction ${early.label})` : ''
      $.ui.toast(`${PLUGIN}: checkpoint banked, compacting at ${Math.round((tokens ?? 0) / 1000)}K${why}`)
      $.clock.after(COMPACT_DELAY_MS, () => {
        void (async () => {
          let compacted = false
          try {
            const out = await $.session.compact()
            compacted = !('skip' in out && out.skip)
          } catch (err) {
            $.ui.log(`${PLUGIN}: compaction failed: ${String(err)}`)
          } finally {
            compactRequested = false
            bandState.enteredAt = undefined
            bandState.early = false
          }
          if (!compacted || !settings.continueAfterCompact) return
          if (continueStale()) {
            $.ui.log(`${PLUGIN}: the person continued the session during the compaction; no resume prompt`)
            return
          }
          try {
            await $.prompt.submit({ text: CONTINUE_TEXT })
          } catch (err) {
            $.ui.log(`${PLUGIN}: the continue after the compaction failed: ${String(err)}`)
          }
        })()
      })
    }
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
    return done
  })

  // Any compaction, ours or the engine's, opens a new cycle.
  on('session.compact', ($, e, next) => {
    bandState.enteredAt = undefined
    bandState.early = false
    compactAsked = false
    compactRequested = false
    return next(e)
  })
}

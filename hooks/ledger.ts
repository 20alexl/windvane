// windvane: the per-project token and cost ledger, recorded by the machine
// so nobody has to write it down.
//
// Every main-loop turn.complete adds the turn's usage (TurnUsage: the four
// token counts as the API reports them) to $.store under
// `ledger:<the folder the session was opened in>:<YYYY-MM-DD>` (the local
// day), with a turn count
// and the cost: the session's own priced total ($.session.usage().cost.usd)
// since the previous main turn, so a subagent's spend lands on the turn that
// waited for it. Subagent turns are not counted on their own (their tokens
// are in the session's cost, not in the token columns). A turn that counted
// nothing (an interrupt, an API error: no usage) adds no entry, and a shell
// cd during the session does not move the project.
//
// /windvane-cost prints today's totals for this project, this project's
// totals over every recorded day, and today's over every project.
//
// Two engine rules shape the split. A plugin registers one matcherless hook
// per event, and register.ts holds session.start and turn.complete; and `$`
// is passed only into functions of the same file. So the store writes at
// those two events live in register.ts (startLedger, recordTurn there), built
// on the plain functions here; this file hooks only its command.
import type { CommandSpec, EngineInterface, On, TurnUsage } from 'claude-code'
import { normalizePath } from './ring'

const PREFIX = 'ledger:'

export const LEDGER_COMMAND: CommandSpec = {
  name: 'windvane-cost',
  description: "windvane: this project's tokens and cost, today and over every recorded day",
}

export type LedgerEntry = {
  input: number
  output: number
  cache_read: number
  cache_creation: number
  turns: number
  cost_usd: number
}

function empty(): LedgerEntry {
  return { input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, cost_usd: 0 }
}

function num(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : 0
}

// A stored entry read defensively: a field missing or not a number is 0.
export function asEntry(v: unknown): LedgerEntry {
  const o = (v ?? {}) as Record<string, unknown>
  return {
    input: num(o.input),
    output: num(o.output),
    cache_read: num(o.cache_read),
    cache_creation: num(o.cache_creation),
    turns: num(o.turns),
    cost_usd: num(o.cost_usd),
  }
}

export function add(a: LedgerEntry, b: LedgerEntry): LedgerEntry {
  return {
    input: a.input + b.input,
    output: a.output + b.output,
    cache_read: a.cache_read + b.cache_read,
    cache_creation: a.cache_creation + b.cache_creation,
    turns: a.turns + b.turns,
    cost_usd: a.cost_usd + b.cost_usd,
  }
}

// The session's cost at the previous main turn. Module state: a reload starts
// over from the figure at its session.start, so nothing is counted twice.
let lastCost: number | undefined

// At session.start: the figure the ledger counts from.
export function costBaseline(cost: number | undefined): void {
  lastCost = cost
}

// One main-loop turn as a ledger entry: its four counts, and the session's
// cost since the previous main turn (0 when either figure is missing).
export function turnEntry(usage: TurnUsage | undefined, cost: number | undefined): LedgerEntry {
  const spent = cost !== undefined && lastCost !== undefined && cost >= lastCost ? cost - lastCost : 0
  if (cost !== undefined) lastCost = cost
  return {
    input: num(usage?.input_tokens),
    output: num(usage?.output_tokens),
    cache_read: num(usage?.cache_read_input_tokens),
    cache_creation: num(usage?.cache_creation_input_tokens),
    turns: 1,
    cost_usd: spent,
  }
}

// The local day of a time in milliseconds, YYYY-MM-DD.
export function localDay(ms: number): string {
  const d = new Date(ms)
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${mm}-${dd}`
}

export function ledgerKey(cwd: string, day: string): string {
  return `${PREFIX}${normalizePath(cwd)}:${day}`
}

// `ledger:<project>:<day>`: the project may hold colons (a drive letter), the
// day is the last ten characters.
function parseKey(key: string): { project: string; day: string } | undefined {
  if (!key.startsWith(PREFIX) || key.length < PREFIX.length + 12) return undefined
  const day = key.slice(-10)
  if (key[key.length - 11] !== ':' || !/^\d{4}-\d{2}-\d{2}$/.test(day)) return undefined
  return { project: key.slice(PREFIX.length, -11), day }
}

function grouped(n: number): string {
  return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
}

function line(label: string, t: LedgerEntry): string {
  return (
    `${label}: ${grouped(t.turns)} turns · input ${grouped(t.input)} · output ${grouped(t.output)}` +
    ` · cache read ${grouped(t.cache_read)} · cache write ${grouped(t.cache_creation)} · $${t.cost_usd.toFixed(2)}`
  )
}

async function report($: EngineInterface, project: string): Promise<string> {
  const today = localDay(await $.clock.now())
  let projectToday = empty()
  let projectAll = empty()
  let allToday = empty()
  for (const key of await $.store.keys()) {
    const k = parseKey(key)
    if (!k || (k.project !== project && k.day !== today)) continue
    const entry = asEntry(await $.store.get(key))
    if (k.project === project) projectAll = add(projectAll, entry)
    if (k.day === today) allToday = add(allToday, entry)
    if (k.project === project && k.day === today) projectToday = add(projectToday, entry)
  }
  return [
    `windvane ledger for ${project}, ${today}`,
    line('today, this project', projectToday),
    line('this project, all days', projectAll),
    line('today, all projects', allToday),
  ].join('\n')
}

// `projectOf` answers the ledger's project: the folder the session was opened
// in, kept by register.ts from session.start. Empty before that, when the
// session's cwd stands in.
export function registerLedger(on: On, projectOf: () => string): void {
  on('command.run', { command: 'windvane-cost' }, async $ => ({
    text: await report($, projectOf() || normalizePath(await $.session.cwd())),
  }))
}

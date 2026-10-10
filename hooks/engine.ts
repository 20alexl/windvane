// windvane: how the mod reaches the engine, and the plugin's options.
//
// The engine is the Python package shipped inside the plugin
// (`<plugin root>/windvane`). The mod runs its modules as
// `python -m windvane.<module>` with PYTHONPATH pointing at that folder, so
// nothing has to be installed beside the plugin. A process started by
// `$.process.run` inherits the host's environment, not the session's: what
// the engine needs from the session (the store, the session id) is passed
// in `env` by the caller.
//
// Pure helpers, and the Host type: the engine follows `$` into functions of
// the same file and never across an import, so register.ts alone holds `$`
// and hands the other modules a Host, a handful of closures over it.
import type { HttpResponse, ProcessRunInit, ProcessRunResult, SettingsSource, Timer } from 'claude-code'
import type { PluginOptions } from 'claude-code'

export const PLUGIN = 'windvane'
export const VERSION = '1.0.15'

// What a module gets instead of `$`: the calls it needs, each spelled once in
// register.ts (hostOf there), the one file that holds the engine interface.
// The environment variables are read there by name, so a module asks for the
// store or the interpreter, never for a variable.
export type Host = {
  // The plugin's folder, absolute.
  pluginRoot: string
  // The store: WINDVANE_DIR, else .windvane under the home folder.
  store(): Promise<string>
  // The interpreter: WINDVANE_PYTHON, else the python option, else `python`.
  python(configured: string): Promise<string>
  sessionId(): Promise<string>
  cwd(): Promise<string>
  root(): Promise<string>
  // $.clock's time, ms since the epoch.
  now(): Promise<number>
  after(ms: number, fn: () => void): Timer
  exists(path: string): Promise<boolean>
  read(path: string): Promise<string>
  write(path: string, text: string): Promise<void>
  run(argv: string[], init: ProcessRunInit): Promise<ProcessRunResult>
  // One POST of a JSON body to the daemon on loopback, at /hook or /tool.
  post(port: number, token: string, path: 'hook' | 'tool', body: string): Promise<HttpResponse>
  storeGet(key: string): Promise<unknown>
  storeSet(key: string, value: unknown): Promise<void>
  storeKeys(): Promise<string[]>
  // The settings merged over every source, or one source's as loaded.
  settings(source?: SettingsSource): Promise<Record<string, unknown>>
  // The session's environment the engine's handlers read (the bridge).
  sessionEnv(): Promise<Record<string, string>>
  // Claude Code's config folder: CLAUDE_CONFIG_DIR, else .claude under home.
  configDir(): Promise<string>
  // WINDVANE_RESULT_BUDGET as set, for the door.
  resultBudgetEnv(): Promise<string | undefined>
  // A line in the session's log, and one in the debug log alone.
  log(text: string): void
  debug(text: string): void
}

// The plugin's userConfig rows the mod itself reads. `semantic`,
// `alert_command`, `strict_pack` and `autonomy` are read by the engine from
// the settings' pluginConfigs; the mod passes nothing for them.
export type Settings = {
  // The interpreter the python row names; '' when it names none.
  python: string
  // false hides the status line segment; the band and the pane stay.
  statusSegment: boolean
  // The door's budget in characters; undefined leaves the door's default.
  resultBudget: number | undefined
  // false: a compaction windvane started ends the session's work until the
  // person types; true: windvane's own prompt resumes it.
  continueAfterCompact: boolean
  // The early_compaction row: the checkpoint band also opens at this fill
  // (a percentage of the window) or once one turn has cost this much (a
  // dollar figure, the session's own priced cost across the turn).
  // undefined: the band opens only at the engine's margin above the trigger.
  earlyCompaction: EarlyCompaction | undefined
}

export type EarlyCompaction = { label: string; percent?: number; usd?: number }

// A positive whole number of characters, from a string or a number; anything
// else is undefined.
export function budgetOf(raw: unknown): number | undefined {
  const n = typeof raw === 'number' ? raw : typeof raw === 'string' && raw.trim() !== '' ? Number(raw.trim()) : NaN
  return Number.isInteger(n) && n > 0 ? n : undefined
}

// The early_compaction row: '40%' (a fill, 1..99 percent of the window) or
// '$0.40' (one turn's cost in dollars, above zero). Anything else is off.
export function earlyCompactionOf(raw: unknown): EarlyCompaction | undefined {
  if (typeof raw !== 'string') return undefined
  const s = raw.trim()
  const pct = /^(\d+(?:\.\d+)?)\s*%$/.exec(s)
  if (pct) {
    const n = Number(pct[1])
    return n > 0 && n < 100 ? { label: `${n}%`, percent: n } : undefined
  }
  const usd = /^\$\s*(\d+(?:\.\d+)?)$/.exec(s)
  if (usd) {
    const n = Number(usd[1])
    return n > 0 ? { label: `$${n}`, usd: n } : undefined
  }
  return undefined
}

// register(on, options): the values as the modules use them.
export function settingsOf(options: PluginOptions | undefined): Settings {
  const o = options ?? {}
  return {
    python: typeof o.python === 'string' ? o.python.trim() : '',
    statusSegment: o.status_segment !== false,
    resultBudget: budgetOf(o.result_budget),
    continueAfterCompact: o.continue_after_compact !== false,
    earlyCompaction: earlyCompactionOf(o.early_compaction),
  }
}

// The interpreter: WINDVANE_PYTHON wins, then the python option, then
// whatever `python` resolves to on PATH.
export function pythonOf(env: string | undefined, configured: string): string {
  return (env && env.trim()) || configured.trim() || 'python'
}

// The environment a run of the engine gets over the host's own: the engine
// package on PYTHONPATH, the store, UTF-8 pipes, and the session id where the
// engine keys its state by the session.
export function engineEnv(root: string, store: string, sessionId?: string): Record<string, string> {
  const env: Record<string, string> = {
    PYTHONPATH: `${root.replace(/\\/g, '/')}`,
    WINDVANE_DIR: store,
    PYTHONIOENCODING: 'utf-8',
  }
  if (sessionId) env.CLAUDE_CODE_SESSION_ID = sessionId
  return env
}

// What to tell the person when the interpreter did not start.
export function pythonHint(python: string, err: unknown): string {
  return `${python} did not run (${String(err)}). Set the plugin's python option, or WINDVANE_PYTHON, to a Python 3.10+ interpreter.`
}

// The last line of a run's stdout read as a JSON object; undefined when it
// is not one.
export function lastJsonLine(stdout: string): Record<string, unknown> | undefined {
  const lines = stdout.trim().split('\n')
  const last = (lines[lines.length - 1] ?? '').trim()
  if (!last.startsWith('{')) return undefined
  try {
    const v: unknown = JSON.parse(last)
    return typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : undefined
  } catch {
    return undefined
  }
}

// Text on one line, at most n characters.
export function clip(text: string, n: number): string {
  const one = text.replace(/\s+/g, ' ').trim()
  return one.length > n ? one.slice(0, n - 3) + '...' : one
}

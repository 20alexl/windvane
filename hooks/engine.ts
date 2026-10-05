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
// Pure helpers only: the engine follows `$` into functions of the same file
// and never across an import, so each hook file makes its own `$` calls and
// hands the values to these.
import type { PluginOptions } from 'claude-code'

export const PLUGIN = 'windvane'
export const VERSION = '0.1.0'

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
}

// A positive whole number of characters, from a string or a number; anything
// else is undefined.
export function budgetOf(raw: unknown): number | undefined {
  const n = typeof raw === 'number' ? raw : typeof raw === 'string' && raw.trim() !== '' ? Number(raw.trim()) : NaN
  return Number.isInteger(n) && n > 0 ? n : undefined
}

// register(on, options): the values as the modules use them.
export function settingsOf(options: PluginOptions | undefined): Settings {
  const o = options ?? {}
  return {
    python: typeof o.python === 'string' ? o.python.trim() : '',
    statusSegment: o.status_segment !== false,
    resultBudget: budgetOf(o.result_budget),
    continueAfterCompact: o.continue_after_compact !== false,
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

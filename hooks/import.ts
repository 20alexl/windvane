// windvane: /windvane-import brings an existing store's records into
// windvane's.
//
// The engine does the work, `python -m windvane.migrate --import`, and
// prints one JSON line, `{"copied": N, "skipped": N, "dst": "<store>"}` or
// `{"error": "..."}`; the command shows the counts or the error.
import type { EngineInterface, On } from 'claude-code'

import { clip, engineEnv, lastJsonLine, pythonHint, pythonOf, type Settings } from './engine'
import { storePath } from './ring'

const IMPORT_TIMEOUT_MS = 300_000

// Registered at session.start by register.ts.
export const IMPORT_COMMAND = {
  name: 'windvane-import',
  description: "windvane: import an existing store's memories, rules and checkpoints",
}

// The reply in one line: "copied N, skipped N into <dst>".
export function importLine(reply: Record<string, unknown>): string {
  const n = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : 0)
  const dst = typeof reply.dst === 'string' && reply.dst ? ` into ${reply.dst}` : ''
  return `copied ${n(reply.copied)}, skipped ${n(reply.skipped)}${dst}`
}

async function importStore($: EngineInterface, configured: string): Promise<string> {
  const python = pythonOf(await $.env.get('WINDVANE_PYTHON'), configured)
  const store = storePath(await $.env.get('WINDVANE_DIR'), await $.env.get('USERPROFILE'), await $.env.get('HOME'))
  let run
  try {
    run = await $.process.run([python, '-m', 'windvane.migrate', '--import'], {
      env: engineEnv($.plugin.root, store),
      timeoutMs: IMPORT_TIMEOUT_MS,
    })
  } catch (err) {
    return `Not imported: ${pythonHint(python, err)}`
  }
  const reply = lastJsonLine(run.stdout)
  if (run.exitCode !== 0 || !reply || typeof reply.error === 'string') {
    const why = (typeof reply?.error === 'string' && reply.error) || clip(run.stderr, 200) || `exit ${run.exitCode}`
    return `Not imported: ${why}`
  }
  return `Imported: ${importLine(reply)}`
}

export function registerImport(on: On, settings: Settings): void {
  on('command.run', { command: 'windvane-import' }, async $ => ({ text: await importStore($, settings.python) }))
}

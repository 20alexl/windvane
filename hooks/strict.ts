// windvane: /windvane-strict seeds the strict rule pack into this project.
//
// The default pack (the working rules) is seeded by the engine on a fresh
// session start. The strict pack (style and workflow rules) is opt-in: the
// strict_pack option seeds it into every new project, this command into the
// session's project once. The engine does the work, `python -m
// windvane.rules seed --project <cwd> --strict`, and prints one JSON line;
// the command shows its summary.
import type { EngineInterface, On } from 'claude-code'

import { clip, engineEnv, lastJsonLine, pythonHint, pythonOf, type Settings } from './engine'
import { storePath } from './ring'

const STRICT_TIMEOUT_MS = 30_000

// Registered at session.start by register.ts.
export const STRICT_COMMAND = {
  name: 'windvane-strict',
  description: 'windvane: seed the strict rule pack (style and workflow rules) into this project',
}

// The JSON line in one sentence: its summary, else its plain fields.
export function summaryOf(reply: Record<string, unknown>): string {
  if (typeof reply.summary === 'string' && reply.summary.trim()) return reply.summary.trim()
  const parts = Object.entries(reply)
    .filter(([, v]) => typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean')
    .map(([k, v]) => `${k}: ${String(v)}`)
  return parts.length > 0 ? parts.join(' · ') : 'done'
}

async function seedStrict($: EngineInterface, configured: string): Promise<string> {
  const python = pythonOf(await $.env.get('WINDVANE_PYTHON'), configured)
  const store = storePath(await $.env.get('WINDVANE_DIR'), await $.env.get('USERPROFILE'), await $.env.get('HOME'))
  const project = await $.session.cwd()
  let run
  try {
    run = await $.process.run([python, '-m', 'windvane.rules', 'seed', '--project', project, '--strict'], {
      cwd: project,
      env: engineEnv($.plugin.root, store),
      timeoutMs: STRICT_TIMEOUT_MS,
    })
  } catch (err) {
    return `Strict pack not seeded: ${pythonHint(python, err)}`
  }
  const reply = lastJsonLine(run.stdout)
  if (run.exitCode !== 0 || !reply || typeof reply.error === 'string') {
    const why = (typeof reply?.error === 'string' && reply.error) || clip(run.stderr, 200) || `exit ${run.exitCode}`
    return `Strict pack not seeded: ${why}`
  }
  return `Strict pack: ${summaryOf(reply)}`
}

export function registerStrict(on: On, settings: Settings): void {
  on('command.run', { command: 'windvane-strict' }, async $ => ({ text: await seedStrict($, settings.python) }))
}

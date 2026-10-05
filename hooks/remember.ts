// windvane: /remember stores the text the person selected in the
// transcript as a DECISION in windvane, for the session's project.
//
// The store is written by the engine, never by the mod: the command runs
// `python -m windvane.remember`, which files the entry through the same
// writer the memory tool's remember operation and the miner's decisions use.
// The text goes in on stdin, so no quoting rule of any shell touches it.
// The interpreter is WINDVANE_PYTHON, else the plugin's python option, else
// `python` on PATH; the store is the one the rest of the mod reads
// (WINDVANE_DIR, else ~/.windvane).
import { atom, read } from 'claude-code'
import type { EngineInterface, On } from 'claude-code'

import { clip, engineEnv, lastJsonLine, pythonHint, pythonOf, type Settings } from './engine'
import { storePath } from './ring'

const lastFile = atom({ plugin: 'windvane', key: 'lastFile' } as const, null)

const REMEMBER_TIMEOUT_MS = 30_000

// Registered at session.start by register.ts.
export const REMEMBER_COMMAND = {
  name: 'remember',
  description: 'Store the selected transcript text in windvane as a decision',
}

type Reply = { stored?: boolean; id?: string; project?: string; message?: string; error?: string }

async function remember($: EngineInterface, configured: string, text: string): Promise<string> {
  const python = pythonOf(await $.env.get('WINDVANE_PYTHON'), configured)
  const store = storePath(await $.env.get('WINDVANE_DIR'), await $.env.get('USERPROFILE'), await $.env.get('HOME'))
  const argv = [python, '-m', 'windvane.remember', '--project', await $.session.cwd(), '--kind', 'decision']
  const file = await read($, lastFile)
  if (file) argv.push('--file', file)

  let run
  try {
    run = await $.process.run(argv, {
      stdin: text,
      env: engineEnv($.plugin.root, store),
      timeoutMs: REMEMBER_TIMEOUT_MS,
    })
  } catch (err) {
    return `Not remembered: ${pythonHint(python, err)}`
  }
  const reply = (lastJsonLine(run.stdout) ?? {}) as Reply
  if (run.exitCode !== 0 || reply.error) {
    const why = reply.error || clip(run.stderr, 200) || `exit ${run.exitCode}`
    return `Not remembered: ${why}`
  }
  if (!reply.stored) return `Already in windvane${reply.project ? ` for ${reply.project}` : ''}: ${reply.message ?? ''}`.trim()
  return `Remembered as a decision${reply.project ? ` for ${reply.project}` : ''}${reply.id ? ` [${reply.id}]` : ''}: ${clip(text, 120)}`
}

export function registerRemember(on: On, settings: Settings): void {
  on('command.run', { command: 'remember' }, async $ => {
    const selected = await $.ui.selection()
    const text = selected?.text.trim() ?? ''
    if (!text) return { text: 'Nothing is selected. Select text in the transcript, then run /remember.' }
    return { text: await remember($, settings.python, text) }
  })
}

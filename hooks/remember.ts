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
//
// register.ts registers the command, hooks its run with the selection and
// the file the model last touched; this module never holds `$`.
import { clip, engineEnv, lastJsonLine, pythonHint, type Host } from './engine'

const REMEMBER_TIMEOUT_MS = 30_000

// Registered at session.start by register.ts.
export const REMEMBER_COMMAND = {
  name: 'remember',
  description: 'Store the selected transcript text in windvane as a decision',
}

type Reply = { stored?: boolean; id?: string; project?: string; message?: string; error?: string }

// The command's answer for the selected text; `file` is the one the model
// last touched, when any.
export async function remember(host: Host, configured: string, text: string, file: string | null | undefined): Promise<string> {
  const python = await host.python(configured)
  const store = await host.store()
  const argv = [python, '-m', 'windvane.remember', '--project', await host.cwd(), '--kind', 'decision']
  if (file) argv.push('--file', file)

  let run
  try {
    run = await host.run(argv, {
      stdin: text,
      env: engineEnv(host.pluginRoot, store),
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

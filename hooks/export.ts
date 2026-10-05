// windvane: /windvane-export writes the project's memory out as files.
//
// The engine does the work, `python -m windvane.export --project <cwd>`,
// and prints one JSON line naming the files it wrote (`written`, or `paths`
// / `files`); the command lists them. An engine that prints one path per
// line instead is read the same way.
//
// register.ts registers the command and hooks its run; this module never
// holds `$`.
import { clip, engineEnv, lastJsonLine, pythonHint, type Host } from './engine'

const EXPORT_TIMEOUT_MS = 120_000

// Registered at session.start by register.ts.
export const EXPORT_COMMAND = {
  name: 'windvane-export',
  description: "windvane: export this project's memory, rules and checkpoints to files",
}

// The paths a run wrote: the JSON line's list, else stdout's lines.
export function pathsOf(stdout: string, reply: Record<string, unknown> | undefined): string[] {
  if (reply) {
    for (const key of ['written', 'paths', 'files']) {
      const v = reply[key]
      if (Array.isArray(v)) return v.filter((p): p is string => typeof p === 'string' && p !== '')
    }
    return []
  }
  return stdout
    .split('\n')
    .map(l => l.trim())
    .filter(Boolean)
}

// The command's answer.
export async function exportProject(host: Host, configured: string): Promise<string> {
  const python = await host.python(configured)
  const store = await host.store()
  const project = await host.cwd()
  let run
  try {
    run = await host.run([python, '-m', 'windvane.export', '--project', project], {
      cwd: project,
      env: engineEnv(host.pluginRoot, store),
      timeoutMs: EXPORT_TIMEOUT_MS,
    })
  } catch (err) {
    return `Not exported: ${pythonHint(python, err)}`
  }
  const reply = lastJsonLine(run.stdout)
  if (run.exitCode !== 0 || typeof reply?.error === 'string') {
    const why = (typeof reply?.error === 'string' && reply.error) || clip(run.stderr, 200) || `exit ${run.exitCode}`
    return `Not exported: ${why}`
  }
  const paths = pathsOf(run.stdout, reply)
  if (paths.length === 0) return 'Nothing exported: the project holds nothing to write.'
  return [`Exported ${paths.length} file${paths.length === 1 ? '' : 's'}:`, ...paths.map(p => `  ${p}`)].join('\n')
}

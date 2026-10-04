// windvane: the engine renders, the mod delivers.
//
// `python -m windvane.brief` prints the pieces of windvane's banner that the
// mod hands to places no command hook reaches: the rules block, the pre-edit
// hook's past-mistakes lines per file, and the checkpoint the
// SessionStart(compact) banner restores, each as the hooks render it. The
// mod never re-renders them, so the two paths cannot drift.
//
// Pure helpers only: the engine follows `$` into functions of the same file
// and never across an import, so each hook file makes its own `$` calls
// (a short top-level runBrief) around these.
import type { ProcessRunResult } from 'claude-code'

export const BRIEF_TIMEOUT_MS = 15_000

export type Brief = { rules: string[]; files: Record<string, string[]>; checkpoint: string[] }

// The CLI's argv for the session's project. `extra` goes last: `--files`
// takes the rest of the argv.
export function briefArgv(python: string, project: string, sid: string, extra: string[]): string[] {
  return [python, '-m', 'windvane.brief', '--project', project, '--session', sid, '--json', ...extra]
}

// The run's answer, or a reason it has none (the caller then passes its
// event through untouched and logs the reason).
export function parseBrief(ran: ProcessRunResult): Brief | string {
  if (ran.exitCode !== 0) return `brief exited ${ran.exitCode}: ${ran.stderr.trim().slice(0, 200)}`
  try {
    const got = JSON.parse(ran.stdout) as Partial<Brief>
    return { rules: got.rules ?? [], files: got.files ?? {}, checkpoint: got.checkpoint ?? [] }
  } catch {
    return `brief printed no JSON: ${ran.stdout.trim().slice(0, 200)}`
  }
}

// Blocks of lines joined as the CLI's plain mode joins them: one blank line
// between blocks, empty blocks dropped.
export function joinBlocks(blocks: string[][]): string {
  return blocks
    .filter(b => b.length > 0)
    .map(b => b.join('\n'))
    .join('\n\n')
}

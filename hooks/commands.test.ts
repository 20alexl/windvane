// windvane: /windvane-strict, /windvane-export and /windvane-import each run
// one engine module with the engine on PYTHONPATH and say what it answered.
//
// The test's hooks are the engine's bottom: an op event answers { value },
// a core event its result object.
import type { On } from 'claude-code'
import { expect, test } from 'claude-code/testing'

const STORE = 'C:/tmp/windvane-commands-store'
const RUN = { args: '', origin: { kind: 'composer' as const }, presentation: { isFullscreen: false, columns: 120 } }

type Run = { argv: readonly string[]; init?: { cwd?: string; env?: Record<string, string> } }

function engine(on: On, stdout: string, exitCode = 0, stderr = ''): Run[] {
  const runs: Run[] = []
  on('env.get', ($, e) => ({ value: e.name === 'WINDVANE_DIR' ? STORE : undefined }))
  on('session.cwd', () => ({ value: 'E:/demo/proj' }))
  on('process.run', ($, e) => {
    runs.push(e as Run)
    return { value: { exitCode, stdout, stderr, isStdoutTruncated: false, isStderrTruncated: false } }
  })
  return runs
}

test('/windvane-strict seeds the strict pack and shows the summary', async ($, on) => {
  const runs = engine(on, 'seeding\n{"summary": "4 strict rules seeded, 2 already present", "seeded": 4}\n')
  const out = await $.command.run({ command: 'windvane-strict', ...RUN })
  expect(out.text).toBe('Strict pack: 4 strict rules seeded, 2 already present')
  expect(runs[0]?.argv).toEqual(['python', '-m', 'windvane.rules', 'seed', '--project', 'E:/demo/proj', '--strict'])
  expect(runs[0]?.init?.env?.PYTHONPATH?.endsWith('/windvane')).toBe(true)
  expect(runs[0]?.init?.env?.WINDVANE_DIR).toBe(STORE)
})

test('/windvane-export lists the paths written', async ($, on) => {
  const runs = engine(on, '{"written": ["E:/demo/proj/.windvane/export/memory.md", "E:/demo/proj/.windvane/export/rules.md"]}')
  const out = await $.command.run({ command: 'windvane-export', ...RUN })
  expect(out.text).toBe(
    ['Exported 2 files:', '  E:/demo/proj/.windvane/export/memory.md', '  E:/demo/proj/.windvane/export/rules.md'].join('\n'),
  )
  expect(runs[0]?.argv).toEqual(['python', '-m', 'windvane.export', '--project', 'E:/demo/proj'])
})

test('/windvane-import shows the counts, and a failure says why', async ($, on) => {
  let stdout = '{"copied": 412, "skipped": 7, "dst": "C:/Users/nobody/.windvane"}'
  let exitCode = 0
  const runs: Run[] = []
  on('env.get', () => ({ value: undefined }))
  on('process.run', ($, e) => {
    runs.push(e as Run)
    return { value: { exitCode, stdout, stderr: 'Traceback: no store to import', isStdoutTruncated: false, isStderrTruncated: false } }
  })

  const out = await $.command.run({ command: 'windvane-import', ...RUN })
  expect(out.text).toBe('Imported: copied 412, skipped 7 into C:/Users/nobody/.windvane')
  expect(runs[0]?.argv).toEqual(['python', '-m', 'windvane.migrate', '--import'])

  // The engine's error line is said as written.
  stdout = '{"error": "no store to import at C:/Users/nobody/.old_store"}'
  exitCode = 1
  expect((await $.command.run({ command: 'windvane-import', ...RUN })).text).toBe('Not imported: no store to import at C:/Users/nobody/.old_store')

  // No answer at all: stderr.
  stdout = ''
  expect((await $.command.run({ command: 'windvane-import', ...RUN })).text).toBe('Not imported: Traceback: no store to import')
})

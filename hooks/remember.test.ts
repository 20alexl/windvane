// windvane: /remember hands the selected text to the engine's writer
// (`python -m windvane.remember`, the text on stdin) for the session's
// project and the file it last touched, and says what the engine answered.
//
// The test's hooks are the engine's bottom: an op event answers { value },
// a core event its result object.
import { expect, mock, test } from 'claude-code/testing'

const STORE = 'C:/tmp/windvane-remember-store'
const RUN = { args: '', origin: { kind: 'composer' as const }, presentation: { isFullscreen: true, columns: 160 } }

type Run = { argv: readonly string[]; init?: { stdin?: string; env?: Record<string, string>; timeoutMs?: number } }

function ran(stdout: string, exitCode = 0, stderr = '') {
  return { value: { exitCode, stdout, stderr, isStdoutTruncated: false, isStderrTruncated: false } }
}

test('/remember stores the selection as a decision through the engine', async ($, on) => {
  const runs: Run[] = []
  let selection: { text: string } | undefined = { text: '  Use sqlite for the cache, not redis.\n' }
  let reply = ran('{"stored": true, "id": "abc123", "project": "e:/demo/proj", "message": "Memory added with id=abc123"}')
  mock.env(on, { WINDVANE_DIR: STORE, WINDVANE_PYTHON: 'E:/py/venv/Scripts/python.exe' })
  on('session.cwd', () => ({ value: 'E:\\demo\\proj' }))
  on('ui.selection', () => ({ value: selection }))
  on('process.run', ($, e) => {
    runs.push(e as Run)
    return reply
  })
  on('tool.call', () => ({ result: { content: [{ type: 'text', text: 'ok' }] } }))

  // The model touched a file: it rides along so the engine files the entry there.
  await $.tool.call({ tool: 'Write', file_path: 'E:\\demo\\proj\\src\\cache.py', content: 'x' } as never)

  const answer = await $.command.run({ command: 'remember', ...RUN })
  expect(answer.text).toBe('Remembered as a decision for e:/demo/proj [abc123]: Use sqlite for the cache, not redis.')
  expect(runs).toHaveLength(1)
  expect(runs[0]?.argv).toEqual([
    'E:/py/venv/Scripts/python.exe', '-m', 'windvane.remember',
    '--project', 'E:\\demo\\proj', '--kind', 'decision', '--file', 'e:/demo/proj/src/cache.py',
  ])
  expect(runs[0]?.init?.stdin).toBe('Use sqlite for the cache, not redis.')
  expect(runs[0]?.init?.env?.WINDVANE_DIR).toBe(STORE)
  expect(runs[0]?.init?.env?.PYTHONPATH?.endsWith('/engine')).toBe(true)

  // The engine's duplicate check answers.
  reply = ran('{"stored": false, "id": "abc123", "project": "e:/demo/proj", "message": "Duplicate of existing memory (id=abc123)"}')
  expect((await $.command.run({ command: 'remember', ...RUN })).text).toBe(
    'Already in windvane for e:/demo/proj: Duplicate of existing memory (id=abc123)',
  )

  // The engine failing is said, with its reason.
  reply = ran('{"error": "nothing to remember: the text is empty"}', 1)
  expect((await $.command.run({ command: 'remember', ...RUN })).text).toBe('Not remembered: nothing to remember: the text is empty')
  reply = ran('', 1, 'No module named windvane')
  expect((await $.command.run({ command: 'remember', ...RUN })).text).toBe('Not remembered: No module named windvane')

  // Nothing selected: nothing runs.
  selection = undefined
  expect((await $.command.run({ command: 'remember', ...RUN })).text).toBe('Nothing is selected.')
  selection = { text: '   ' }
  expect((await $.command.run({ command: 'remember', ...RUN })).text).toBe('Nothing is selected.')
  expect(runs).toHaveLength(4)
})

test('without WINDVANE_PYTHON the python on PATH runs', async ($, on) => {
  const runs: Run[] = []
  mock.env(on, { USERPROFILE: 'C:\\Users\\nobody' })
  on('session.cwd', () => ({ value: 'E:/demo' }))
  on('ui.selection', () => ({ value: { text: 'keep the ring at twenty' } }))
  on('process.run', ($, e) => {
    runs.push(e as Run)
    return ran('{"stored": true, "id": "x1", "project": "e:/demo"}')
  })

  await $.command.run({ command: 'remember', ...RUN })
  expect(runs[0]?.argv.slice(0, 3)).toEqual(['python', '-m', 'windvane.remember'])
  expect(runs[0]?.argv).not.toContain('--file')
  expect(runs[0]?.init?.env?.WINDVANE_DIR).toBe('C:/Users/nobody/.windvane')
})

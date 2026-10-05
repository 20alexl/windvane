// windvane: the first interactive session is offered the semantic tier
// once; the answer installs the extra and turns the row on, or is kept so
// the question is not asked again (for a week, or ever).
import type { SessionUsage } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

import {
  EXTRA_PACKAGES,
  RETRY_AFTER_MS,
  SEMANTIC_OFFER_KEY,
  SEMANTIC_ROW,
  checkArgv,
  extraInstalledFrom,
  installArgv,
  offerDue,
  offerFor,
  recordOf,
  rowOnFrom,
} from './setup'

test('the decisions: when the question is due, what it asks, how the checks read', async () => {
  const now = 1_000_000_000_000
  expect(offerDue(undefined, now)).toBe(true)
  expect(offerDue('junk', now)).toBe(true)
  expect(offerDue(recordOf('done', now), now + 1)).toBe(false)
  expect(offerDue(recordOf('never', now), now + 10 * RETRY_AFTER_MS)).toBe(false)
  expect(offerDue(recordOf('later', now), now + RETRY_AFTER_MS - 1)).toBe(false)
  expect(offerDue(recordOf('later', now), now + RETRY_AFTER_MS)).toBe(true)

  expect(rowOnFrom([])).toBe(false)
  expect(rowOnFrom([{ key: SEMANTIC_ROW, value: true }])).toBe(true)
  expect(rowOnFrom([{ key: SEMANTIC_ROW, value: 'true' }])).toBe(true)
  expect(rowOnFrom([{ key: SEMANTIC_ROW, value: false }, { key: 'windvane.python', value: 'py' }])).toBe(false)

  expect(checkArgv('E:/py/python.exe')[0]).toBe('E:/py/python.exe')
  expect(checkArgv('python').join(' ')).toContain('semantic.available()')
  expect(extraInstalledFrom({ exitCode: 0, stdout: '1\n' })).toBe(true)
  expect(extraInstalledFrom({ exitCode: 0, stdout: 'warning: x\n0\n' })).toBe(false)
  expect(extraInstalledFrom({ exitCode: 1, stdout: '1' })).toBe(false)
  expect(installArgv('python')).toEqual(['python', '-m', 'pip', 'install', ...EXTRA_PACKAGES])

  expect(offerFor(true, true)).toBeUndefined()
  expect(offerFor(true, false)?.act).toBe('Install')
  expect(offerFor(true, false)?.question).toContain('is on but the extra')
  expect(offerFor(false, true)?.act).toBe('Turn on')
  expect(offerFor(false, true)?.question).toContain('the semantic row is off')
  expect(offerFor(false, false)?.act).toBe('Install and turn on')
  expect(offerFor(false, false)?.question).toContain('embedding model')
})

// The session beneath the offer: the register module's session.start with
// the engine's bottom faked the way register.test.ts fakes it, plus the
// row, the extra check, the pip run and the dialog.
const SID = 'aaaaaaaa-0000-4000-8000-00000000000b'
const STORE = 'C:/tmp/windvane-setup-store'
const START = { cwd: 'E:/demo/proj', surface: 'terminal' as const, isInteractive: true }

function usage(): SessionUsage {
  return {
    startedAt: 0,
    context: { tokens: 100_000, window: 1_000_000, percent: 10, breakdown: { rawMaxTokens: 750_000 } },
    rateLimits: [],
  } as unknown as SessionUsage
}

type Run = { argv: readonly string[]; init?: { env?: Record<string, string>; timeoutMs?: number } }
type World = { row: boolean; installed: boolean; answer: string | Error; pipExit?: number; prior?: unknown }

function ran(stdout: string, exitCode = 0, stderr = '') {
  return { value: { exitCode, stdout, stderr, isStdoutTruncated: false, isStderrTruncated: false } }
}

// The offer runs beside session.start, never awaited by it: wait for its
// answer to land in the store (or for the dialog to have been dismissed).
async function settled(w: { stored: Record<string, unknown>; asked: string[] }, dismissed = false): Promise<void> {
  for (let i = 0; i < 200; i++) {
    if (w.stored[SEMANTIC_OFFER_KEY] !== undefined || (dismissed && w.asked.length > 0)) break
    await new Promise(resolve => setTimeout(resolve, 5))
  }
}

function world(on: Parameters<Parameters<typeof test>[1]>[1], w: World) {
  const runs: Run[] = []
  const sets: Array<{ key: string; value: unknown }> = []
  const stored: Record<string, unknown> = {}
  if (w.prior !== undefined) stored[SEMANTIC_OFFER_KEY] = w.prior
  const asked: string[] = []
  mock.env(on, { WINDVANE_DIR: STORE, USERPROFILE: 'C:/Users/nobody', WINDVANE_PYTHON: 'E:/py/python.exe' })
  const clock = mock.clock(on)
  on('session.id', () => ({ value: SID }))
  on('session.model', () => ({ value: 'claude-test' }))
  on('session.root', () => ({ value: 'E:/demo' }))
  on('session.cwd', () => ({ value: 'E:/demo/proj' }))
  on('session.usage', () => ({ value: usage() }))
  on('fs.exists', ($, e) => ({ value: e.path.replace(/\\/g, '/').includes('/sessions') }))
  on('fs.read', () => ({ value: '' }))
  on('fs.write', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  const logs: string[] = []
  on('ui.log', ($, e) => {
    logs.push(String((e as unknown as { text: string }).text))
    return { value: undefined }
  })
  on('store.get', ($, e) => ({ value: stored[e.key] }))
  on('store.set', ($, e) => {
    stored[e.key] = e.value
    return { value: undefined }
  })
  on('config.list', () => ({ value: [{ key: SEMANTIC_ROW, label: 'Semantic scoring', kind: 'toggle', value: w.row }] }))
  on('config.set', ($, e) => {
    sets.push({ key: e.key, value: e.value })
    return { value: e.value }
  })
  // $.ui.ask is a tool.call of AskUserQuestion: the dialog's answer comes
  // back as the tool's result, a denied call as a dismissed dialog.
  on('tool.call', { tool: 'AskUserQuestion' }, ($, e) => {
    const questions = (e as unknown as { questions: Array<{ question: string }> }).questions
    const question = questions[0]?.question ?? ''
    asked.push(question)
    if (w.answer instanceof Error) return { deny: w.answer.message }
    return { result: { questions, answers: { [question]: w.answer } } }
  })
  on('process.run', ($, e) => {
    const run = e as Run
    runs.push(run)
    if (run.argv[1] === '-c') return ran(w.installed ? '1\n' : '0\n')
    if (run.argv[1] === '-m' && run.argv[2] === 'pip') return ran('', w.pipExit ?? 0, w.pipExit ? 'boom' : '')
    return ran('{"text": "ok", "isError": false}')
  })
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('turn.complete', () => ({ text: '' }))
  return { runs, sets, stored, asked, clock, logs }
}

test('a fresh interactive session is asked once; yes installs the extra and turns the row on', async ($, on) => {
  const w = world(on, { row: false, installed: false, answer: 'Install and turn on' })
  await $.session.start(START)
  await w.clock.advance(1)
  await settled(w)
  expect(w.asked.length).toBe(1)
  expect(w.asked[0]).toContain('embedding model')
  const pip = w.runs.find(r => r.argv[2] === 'pip')
  expect(w.logs.some(l => l.includes('semantic offer answered: Install and turn on'))).toBe(true)
  expect(pip?.argv.slice(0, 4)).toEqual(['E:/py/python.exe', '-m', 'pip', 'install'])
  expect(pip?.argv.slice(4)).toEqual(EXTRA_PACKAGES)
  expect(pip?.init?.env?.PYTHONPATH?.endsWith('/windvane')).toBe(true)
  expect(w.sets).toEqual([{ key: SEMANTIC_ROW, value: true }])
  expect((w.stored[SEMANTIC_OFFER_KEY] as { answer: string }).answer).toBe('done')
})

test('the extra already there: only the row is offered; both in place: nothing is asked', async ($, on) => {
  const w = world(on, { row: false, installed: true, answer: 'Turn on' })
  await $.session.start(START)
  await w.clock.advance(1)
  await settled(w)
  expect(w.asked[0]).toContain('the semantic row is off')
  expect(w.runs.some(r => r.argv[2] === 'pip')).toBe(false)
  expect(w.sets).toEqual([{ key: SEMANTIC_ROW, value: true }])
})

test('both in place: the offer closes without a question', async ($, on) => {
  const w = world(on, { row: true, installed: true, answer: 'Install' })
  await $.session.start(START)
  await w.clock.advance(1)
  await settled(w)
  expect(w.asked).toEqual([])
  expect((w.stored[SEMANTIC_OFFER_KEY] as { answer: string }).answer).toBe('done')
})

test('a failed install leaves the row off and asks again later; a dismissed dialog records nothing', async ($, on) => {
  const w = world(on, { row: false, installed: false, answer: 'Install and turn on', pipExit: 1 })
  await $.session.start(START)
  await w.clock.advance(1)
  await settled(w)
  expect(w.sets).toEqual([])
  expect((w.stored[SEMANTIC_OFFER_KEY] as { answer: string }).answer).toBe('later')
})

test('a prior never, or a not now within the week, is not asked again', async ($, on) => {
  const w = world(on, { row: false, installed: false, answer: 'Install and turn on', prior: recordOf('later', Date.now() - 1000) })
  await $.session.start(START)
  await w.clock.advance(1)
  await new Promise(resolve => setTimeout(resolve, 50))
  expect(w.asked).toEqual([])
  expect(w.runs.some(r => r.argv[1] === '-c')).toBe(false) // not even the check runs
})

test('a dismissed dialog records nothing, so the next session asks again', async ($, on) => {
  const w = world(on, { row: false, installed: false, answer: new Error('dismissed') })
  await $.session.start(START)
  await w.clock.advance(1)
  await settled(w, true)
  await new Promise(resolve => setTimeout(resolve, 50))
  expect(w.asked.length).toBe(1)
  expect(w.stored[SEMANTIC_OFFER_KEY]).toBeUndefined()
})

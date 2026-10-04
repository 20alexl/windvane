// windvane, the ledger: main-loop turns add their usage to the
// project's entry for the day, a subagent's turn adds nothing of its own, and
// /windvane-cost prints the sums.
//
// The test's hooks are the engine's bottom: an op event answers { value }, a
// core event its result object. The clock is mocked, the store is the test's.
import { expect, mock, test } from 'claude-code/testing'

function day(ms: number): string {
  const d = new Date(ms)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

let turns = 0
function turnEnd(usage: [number, number, number, number], agentId?: string) {
  turns += 1
  const [input, output, read, write] = usage
  return {
    answer: '',
    durationMs: 1,
    isAborted: false,
    turnId: `t${turns}`,
    reason: 'answer' as const,
    ...(agentId === undefined ? {} : { agentId }),
    usage: {
      input_tokens: input,
      output_tokens: output,
      cache_read_input_tokens: read,
      cache_creation_input_tokens: write,
      model: 'claude-test',
    },
  }
}

test('two turns accumulate under the project and day, and the command prints the sums', async ($, on) => {
  const clock = mock.clock(on)
  const today = day(clock.now())
  const yesterday = day(clock.now() - 86_400_000)
  // The store beneath the plugin, kept here so the test can read it (the
  // test's `$` carries the engine's events, not its op nouns).
  const store: Record<string, unknown> = {
    [`ledger:e:/demo/proj:${yesterday}`]: { input: 1, output: 2, cache_read: 3, cache_creation: 4, turns: 1, cost_usd: 1 },
    [`ledger:e:/other:${today}`]: { input: 10, output: 20, cache_read: 30, cache_creation: 40, turns: 2, cost_usd: 0.5 },
    unrelated: 7,
  }
  on('store.get', ($, e) => ({ value: store[e.key] }))
  on('store.set', ($, e) => {
    store[e.key] = JSON.parse(JSON.stringify(e.value))
    return { value: undefined }
  })
  on('store.keys', () => ({ value: Object.keys(store) }))

  let cost = 1.0
  const commands: string[] = []
  on('command.register', ($, e) => {
    commands.push(e.name)
    return { value: { command: e.name } }
  })
  // The engine hands the cwd in the host's spelling.
  on('session.cwd', () => ({ value: 'E:\\demo\\proj' }))
  on('session.usage', () => ({
    value: { startedAt: 0, context: { window: 200_000 }, rateLimits: [], cost: { usd: cost } },
  }))
  // register.ts's session.start reads these; no sessions folder keeps its mirror off.
  on('session.id', () => ({ value: 'aaaaaaaa-0000-4000-8000-00000000000b' }))
  on('session.model', () => ({ value: 'claude-test' }))
  on('session.root', () => ({ value: 'E:\\demo' }))
  on('env.get', () => ({ value: undefined }))
  on('fs.exists', () => ({ value: false }))
  on('fs.read', () => ({ value: '{}' }))
  on('ui.log', () => ({ value: undefined }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('turn.complete', () => ({ text: '' }))

  await $.session.start({ cwd: 'E:\\demo\\proj', surface: 'terminal', isInteractive: true })
  expect(commands).toContain('windvane-cost')

  cost = 1.25
  await $.turn.complete(turnEnd([100, 20, 1_000, 50]))
  // A subagent's turn: skipped; its spend lands on the next main turn.
  cost = 1.5
  await $.turn.complete(turnEnd([9_999, 9_999, 9_999, 9_999], 'agent-1'))
  cost = 1.75
  await $.turn.complete(turnEnd([200, 30, 2_000, 0]))

  expect(store[`ledger:e:/demo/proj:${today}`]).toEqual({
    input: 300,
    output: 50,
    cache_read: 3_000,
    cache_creation: 50,
    turns: 2,
    cost_usd: 0.75,
  })

  const ran = await $.command.run({
    command: 'windvane-cost',
    args: '',
    origin: { kind: 'composer' },
    presentation: { isFullscreen: false, columns: 120 },
  })
  expect(ran.text).toBe(
    [
      `windvane ledger for e:/demo/proj, ${today}`,
      'today, this project: 2 turns · input 300 · output 50 · cache read 3,000 · cache write 50 · $0.75',
      'this project, all days: 3 turns · input 301 · output 52 · cache read 3,003 · cache write 54 · $1.75',
      'today, all projects: 4 turns · input 310 · output 70 · cache read 3,030 · cache write 90 · $1.25',
    ].join('\n'),
  )
})

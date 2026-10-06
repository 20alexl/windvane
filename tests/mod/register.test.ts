// windvane: the mirror is written from the session's own figures; the
// compaction is the model's call alone (compact_now compacts once, at the
// turn boundary) and a checkpoint save never compacts, in the band or out
// of it; the status_segment option hides the status line segment.
//
// The test's hooks are the engine's bottom: an op event (session.id, fs.read,
// env.get ...) answers with { value }, a core event with its result object.
import type { On, SessionUsage } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

import { earlyCompactionOf } from '../../hooks/engine'

const SID = 'aaaaaaaa-0000-4000-8000-00000000000a'
const STORE = 'C:/tmp/windvane-test-store'
const SUMMARY = { role: 'user' as const, text: 'Summary of the conversation so far.', toolUses: [] }
const START = { cwd: 'E:/demo/proj', surface: 'terminal' as const, isInteractive: true }

// The usage the mod reads: the fill, the window, the compaction point (the
// breakdown's rawMaxTokens; the rest of the breakdown is not read).
function usageAt(tokens: number): SessionUsage {
  return {
    startedAt: 0,
    context: { tokens, window: 1_000_000, percent: Math.round(tokens / 10_000), breakdown: { rawMaxTokens: 750_000 } },
    rateLimits: [
      { kind: 'five_hour', percentUsed: 9, resetsAt: '2026-10-04T06:20:00.000Z' },
      { kind: 'seven_day', percentUsed: 30, resetsAt: '2026-10-09T00:00:00.000Z' },
      // A model-specific weekly window: a kind the flat keys do not name.
      { kind: 'seven_day_model', percentUsed: 92, resetsAt: '2026-10-08T12:00:00.000Z' },
    ],
  } as unknown as SessionUsage
}

let turns = 0
function turnEnd() {
  turns += 1
  return { answer: '', durationMs: 1, isAborted: false, turnId: `t${turns}`, reason: 'answer' as const }
}

test('writes the mirror from usage; a save never compacts, compact_now does once', async ($, on) => {
  const written: Record<string, string> = {}
  let tokens = 100_000
  let compactions = 0

  const env: Record<string, string> = { WINDVANE_DIR: STORE, USERPROFILE: 'C:/Users/nobody' }
  const trace: string[] = []
  on('env.get', ($, e) => {
    trace.push(`env.get ${e.name}`)
    return { value: env[e.name] }
  })
  const clock = mock.clock(on)

  on('session.id', () => ({ value: SID }))
  on('session.model', () => ({ value: 'claude-test' }))
  on('session.root', () => ({ value: 'E:/demo' }))
  on('session.cwd', () => ({ value: 'E:/demo/proj' }))
  on('session.usage', () => ({ value: usageAt(tokens) }))
  on('session.compact', () => {
    compactions += 1
    return { messages: [SUMMARY] }
  })
  const prompts: string[] = []
  on('prompt.submit', ($, e) => {
    prompts.push(e.text)
    return { text: e.text }
  })
  // The engine hands paths to the fs events in the host's spelling (backslashes
  // on Windows); the mod and the test speak forward slashes.
  const fwd = (p: string) => p.replace(/\\/g, '/')
  on('fs.exists', ($, e) => {
    const p = fwd(e.path)
    trace.push(`fs.exists ${p}`)
    return { value: p.includes('/sessions') || p.endsWith('manifest.json') || p.endsWith('latest_handoff.json') }
  })
  on('fs.read', ($, e) => {
    const p = fwd(e.path)
    if (p.endsWith('manifest.json')) return { value: JSON.stringify({ projects: { 'e:/demo/proj': { hash: 'abcd1234' } } }) }
    if (p.endsWith('latest_handoff.json')) return { value: JSON.stringify({ created: Date.now() / 1000 - 600, kind: 'manual' }) }
    return { value: '' }
  })
  on('fs.write', ($, e) => {
    written[fwd(e.path)] = e.text
    return { value: undefined }
  })
  on('ui.status', ($, e) => {
    trace.push(`ui.status ${JSON.stringify(e)}`)
    return { value: undefined }
  })
  on('ui.toast', ($, e) => {
    trace.push(`ui.toast ${JSON.stringify(e)}`)
    return { value: undefined }
  })
  on('ui.log', ($, e) => {
    trace.push(`ui.log ${JSON.stringify(e)}`)
    return { value: undefined }
  })
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  // The engine behind the plugin's checkpoint tool: no daemon port file (the
  // fs.read above answers ''), so the subprocess answers.
  on('process.run', () => ({
    value: { exitCode: 0, stdout: '{"text": "Checkpoint saved: task_1", "isError": false}', stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
  }))
  on('turn.complete', () => ({ text: '' }))

  await $.session.start(START)

  // The mirror record and the marker, from the session's own figures.
  expect([...Object.keys(written), ...trace].join('\n')).toContain(`${STORE}/sessions/${SID}.ctx.json`)
  const mirror = JSON.parse(written[`${STORE}/sessions/${SID}.ctx.json`] ?? '{}')
  expect(mirror.source).toBe('mod')
  expect(mirror.total_input_tokens).toBe(100_000)
  expect(mirror.context_window_size).toBe(1_000_000)
  expect(mirror.five_hour_pct).toBe(9)
  expect(mirror.seven_day_pct).toBe(30)
  // Every window by kind, the third one included.
  expect(Object.keys(mirror.rate_limits)).toEqual(['five_hour', 'seven_day', 'seven_day_model'])
  expect(mirror.rate_limits.seven_day_model).toEqual({ pct: 92, resets_at: Date.parse('2026-10-08T12:00:00.000Z') / 1000 })
  expect(mirror.rate_limits.five_hour.pct).toBe(9)
  expect(typeof mirror.five_hour_resets_at).toBe('number')
  // The window the mod measured against, for reading a store afterwards.
  expect(mirror.compaction_point).toBe(750_000)
  expect(written[`${STORE}/sessions/${SID}.mod`]).toContain('windvane')

  // Inside the band (750K point: trigger 718K, last call from 698K) a turn
  // boundary does not compact: the segment says "checkpoint now", the
  // decision is the model's.
  tokens = 700_000
  await clock.advance(10_000)
  expect(trace.at(-1)?.includes('checkpoint now') || trace.some(t => t.includes('checkpoint now'))).toBe(true)
  await $.turn.complete(turnEnd())
  await clock.advance(1_000)
  expect(compactions).toBe(0)

  // A list is no save: still no compaction.
  const listed = await $.tool.call({ tool: 'mcp__windvane__checkpoint', operation: 'list' } as never)
  expect(JSON.stringify(listed)).toContain('Checkpoint saved')
  await $.turn.complete(turnEnd())
  await clock.advance(1_000)
  expect(compactions).toBe(0)

  // A deliberate save in the band lands, and the next turn boundary still
  // does not compact: a save is a save (the model keeps the plain
  // checkpoint at every step end), never a request to compact.
  await $.tool.call({ tool: 'mcp__windvane__checkpoint', operation: 'save', task_description: 'x' } as never)
  await $.turn.complete(turnEnd())
  await clock.advance(1_000)
  expect(compactions).toBe(0)
  expect(prompts.length).toBe(0)

  // compact_now is the one request: the turn boundary compacts, and
  // windvane's own prompt resumes the work after the compaction it started.
  await $.tool.call({ tool: 'mcp__windvane__compact_now' } as never)
  await $.turn.complete(turnEnd())
  await clock.advance(1_000)
  expect(compactions).toBe(1)
  expect(prompts.length).toBe(1)

  // The compaction opened a new cycle: still in the band, no second
  // compaction until another compact_now.
  await clock.advance(10_000)
  await $.turn.complete(turnEnd())
  await clock.advance(1_000)
  expect(compactions).toBe(1)
  expect(prompts.length).toBe(1)
})

// A session in the band, the store's sessions folder present, the engine
// answering every tool call and every brief.
type Counters = {
  compactions: number
  statuses: string[]
  briefs: number
  prompts?: string[]
  veto?: boolean
  // The engine refuses the compaction (a turn had begun): the call rejects.
  refuse?: boolean
  // The compaction takes until this settles (the person may type meanwhile).
  hold?: Promise<void>
  logs?: string[]
  noSessions?: boolean
  // The session's priced cost, when the test moves it between turns.
  cost?: number
  // The fill, when the test moves it; else inBand's figure.
  tokens?: number
  // Every file the mod wrote, by forward-slash path, when the test reads them.
  written?: Record<string, string>
  toasts?: string[]
}

function inBand(
  on: On,
  counters: Counters,
  tokens = 700_000,
  tool = { exitCode: 0, stdout: '{"text": "Draft banked as task_9.", "isError": false}' },
) {
  on('env.get', ($, e) => ({ value: ({ WINDVANE_DIR: STORE, USERPROFILE: 'C:/Users/nobody' } as Record<string, string>)[e.name] }))
  on('session.id', () => ({ value: SID }))
  on('session.model', () => ({ value: 'claude-test' }))
  on('session.root', () => ({ value: 'E:/demo' }))
  on('session.cwd', () => ({ value: 'E:/demo/proj' }))
  on('session.usage', () => ({ value: { ...usageAt(counters.tokens ?? tokens), ...(counters.cost === undefined ? {} : { cost: { usd: counters.cost } }) } }))
  on('session.compact', async () => {
    if (counters.refuse) throw new Error('$.session.compact: a turn is running (t9); the conversation compacts between turns')
    if (counters.hold) await counters.hold
    if (counters.veto) return { skip: 'blocked by a hook' }
    counters.compactions += 1
    return { messages: [SUMMARY] }
  })
  // The engine beneath a plugin's $.prompt.submit: the prompt enters.
  on('prompt.submit', ($, e) => {
    ;(counters.prompts ??= []).push(e.text)
    return { text: e.text }
  })
  on('fs.exists', ($, e) => ({ value: counters.noSessions !== true && e.path.replace(/\\/g, '/').endsWith('/sessions') }))
  on('fs.read', () => ({ value: '' }))
  on('fs.write', ($, e) => {
    if (counters.written) counters.written[e.path.replace(/\\/g, '/')] = e.text
    return { value: undefined }
  })
  on('ui.status', ($, e) => {
    counters.statuses.push(JSON.stringify(e))
    return { value: undefined }
  })
  on('ui.toast', ($, e) => {
    counters.toasts?.push(JSON.stringify(e))
    return { value: undefined }
  })
  on('ui.log', ($, e) => {
    counters.logs?.push(JSON.stringify(e))
    return { value: undefined }
  })
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('process.run', ($, e) => {
    const brief = e.argv.includes('windvane.brief')
    if (brief) counters.briefs += 1
    const stdout = brief ? JSON.stringify({ rules: ['Rules (1, proj):', '  [r1] trash over rm'], files: {}, checkpoint: [] }) : tool.stdout
    return { value: { exitCode: brief ? 0 : tool.exitCode, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('turn.complete', () => ({ text: '' }))
}

test('compact_now banks, then compacts once when the turn ends', async ($, on) => {
  const counters: Counters = { compactions: 0, statuses: [], briefs: 0 }
  const clock = mock.clock(on)
  // Well below the band: compact_now does not need it.
  inBand(on, counters, 100_000)

  await $.session.start(START)
  const out = await $.tool.call({ tool: 'mcp__windvane__compact_now' } as never)
  expect(JSON.stringify(out)).toContain('Draft banked as task_9.')
  expect(JSON.stringify(out)).toContain('compacts as soon as this turn ends')
  // The turn is still running: nothing compacts yet.
  await clock.advance(1_000)
  expect(counters.compactions).toBe(0)

  await $.turn.complete(turnEnd())
  await clock.advance(1_000)
  // A compaction the plugin starts runs beneath the plugin's own hooks, so
  // compact.ts adds no brief to it; the engine's SessionStart(compact)
  // banner restores the rules and the checkpoint instead (no .briefed
  // marker was written). Pinned so a change in the engine shows here.
  expect({ compactions: counters.compactions, briefs: counters.briefs }).toEqual({ compactions: 1, briefs: 0 })
  // Then windvane's own prompt resumes the work, once, pointing at the
  // checkpoint the banner restored.
  expect(counters.prompts?.length).toBe(1)
  expect(counters.prompts?.[0]).toContain('Continue from the checkpoint')
  expect(counters.prompts?.[0]).toContain('session-start brief')

  // Asked once, compacted once.
  await clock.advance(10_000)
  await $.turn.complete(turnEnd())
  await clock.advance(1_000)
  expect(counters.compactions).toBe(1)
  expect(counters.prompts?.length).toBe(1)
})

test('a prompt the person typed while the compaction ran means no continue prompt', async ($, on) => {
  const counters: Counters = { compactions: 0, statuses: [], briefs: 0 }
  const clock = mock.clock(on)
  let release!: () => void
  counters.hold = new Promise<void>(resolve => {
    release = resolve
  })
  inBand(on, counters)

  await $.session.start(START)
  await $.tool.call({ tool: 'mcp__windvane__compact_now' } as never)
  // The turn ends and the compaction runs inside the turn-end hook. Typed
  // while it runs: the engine queues the prompt, and it runs before anything
  // a plugin submits, so a continue would be stale.
  const ending = $.turn.complete(turnEnd())
  await clock.advance(200)
  await $.prompt.submit({ text: 'and also rename the module', wait: false, origin: { kind: 'composer' } } as never)
  release()
  await ending
  await clock.advance(1_000)
  expect(counters.compactions).toBe(1)
  expect(counters.prompts).toEqual(['and also rename the module'])
})

test('a prompt typed over the running turn was delivered into it: the continue prompt still comes', async ($, on) => {
  const counters: Counters = { compactions: 0, statuses: [], briefs: 0 }
  const clock = mock.clock(on)
  inBand(on, counters)

  await $.session.start(START)
  await $.tool.call({ tool: 'mcp__windvane__compact_now' } as never)
  // Typed while the turn ran (`turnId` names it): the engine delivered it
  // into that turn, which answered it before the compaction. A session that
  // read such a prompt as the person continuing skipped the resume and sat
  // idle after the compaction (2026-10-05).
  await clock.advance(200)
  await $.prompt.submit({ text: 'that idea needs ironing out', wait: false, origin: { kind: 'composer' }, turnId: 't-running' } as never)
  await $.turn.complete({ ...turnEnd(), durationMs: 5_000 })
  await clock.advance(1_000)
  expect(counters.compactions).toBe(1)
  expect(counters.prompts?.length).toBe(2)
  expect(counters.prompts?.[0]).toBe('that idea needs ironing out')
})

test('a compaction the engine refuses is asked for again at the next turn end, with nothing said', async ($, on) => {
  const counters: Counters = { compactions: 0, statuses: [], briefs: 0, refuse: true, logs: [] }
  const clock = mock.clock(on)
  inBand(on, counters)

  await $.session.start(START)
  await $.tool.call({ tool: 'mcp__windvane__compact_now' } as never)
  await $.turn.complete(turnEnd())
  await clock.advance(1_000)
  expect(counters.compactions).toBe(0)
  // The refusal goes to the debug log alone, never into the transcript.
  const said = counters.logs!.filter(l => l.includes('compaction'))
  expect(said.length).toBe(1)
  expect(said[0]).toContain('"to":"debug"')
  // The request still stands: the next turn end compacts.
  counters.refuse = false
  await $.turn.complete(turnEnd())
  await clock.advance(1_000)
  expect(counters.compactions).toBe(1)
  expect(counters.prompts?.length).toBe(1)
})

test('after a compaction the fill reads stale until it changes: no band, no fill in the mirror', async ($, on) => {
  const counters: Counters = { compactions: 0, statuses: [], briefs: 0, written: {}, tokens: 700_000 }
  const clock = mock.clock(on)
  inBand(on, counters)
  const mirror = () => JSON.parse(counters.written?.[`${STORE}/sessions/${SID}.ctx.json`] ?? '{}')

  await $.session.start(START)
  expect(mirror().total_input_tokens).toBe(700_000)
  await $.tool.call({ tool: 'mcp__windvane__compact_now' } as never)
  await $.turn.complete(turnEnd())
  await clock.advance(1_000)
  expect(counters.compactions).toBe(1)

  // Claude Code reports the pre-compaction size until the next request: the
  // same figure says nothing, so the mirror carries no fill, the segment
  // shows none and the band stays closed.
  await clock.advance(10_000)
  expect(mirror().total_input_tokens).toBe(undefined)
  expect(mirror().stale_after_compaction).toBe(true)
  expect(counters.statuses.at(-1)).toContain('ctx ?')
  expect(counters.statuses.at(-1)).not.toContain('checkpoint now')

  // The reading changed: the fill is the rewritten conversation's.
  counters.tokens = 120_000
  await clock.advance(10_000)
  expect(mirror().total_input_tokens).toBe(120_000)
  expect(mirror().stale_after_compaction).toBe(undefined)
  expect(counters.statuses.at(-1)).toContain('ctx 16%')
})

test('a delivery into the running turn is not the person continuing: the continue prompt follows', async ($, on) => {
  const counters: Counters = { compactions: 0, statuses: [], briefs: 0 }
  const clock = mock.clock(on)
  inBand(on, counters)

  await $.session.start(START)
  await $.tool.call({ tool: 'mcp__windvane__compact_now' } as never)
  // Another session's message lands in the turn after it began; the turn
  // ends later and reports its length, so the delivery falls inside it.
  await clock.advance(2_500)
  await $.prompt.submit({ text: 'a message from another session', wait: false, origin: { kind: 'peer' } } as never)
  await clock.advance(2_500)
  await $.turn.complete({ ...turnEnd(), durationMs: 5_000 })
  await clock.advance(1_000)
  expect(counters.compactions).toBe(1)
  expect(counters.prompts?.length).toBe(2)
  expect(counters.prompts?.[1]).toContain('Continue from the checkpoint')
})

test('continue_after_compact off: the compaction happens, no prompt follows', { options: { continue_after_compact: false } }, async ($, on) => {
  const counters: Counters = { compactions: 0, statuses: [], briefs: 0 }
  const clock = mock.clock(on)
  inBand(on, counters, 100_000)

  await $.session.start(START)
  await $.tool.call({ tool: 'mcp__windvane__compact_now' } as never)
  await $.turn.complete(turnEnd())
  await clock.advance(1_000)
  expect(counters.compactions).toBe(1)
  expect(counters.prompts ?? []).toEqual([])
})

test('a compaction a hook vetoed gets no continue', async ($, on) => {
  // The veto a classic PreCompact hook makes: the conversation stays.
  const counters: Counters = { compactions: 0, statuses: [], briefs: 0, veto: true }
  const clock = mock.clock(on)
  inBand(on, counters, 100_000)

  await $.session.start(START)
  await $.tool.call({ tool: 'mcp__windvane__compact_now' } as never)
  await $.turn.complete(turnEnd())
  await clock.advance(1_000)
  expect(counters.prompts ?? []).toEqual([])
})

test('a compact_now that failed asks for nothing', async ($, on) => {
  const counters: Counters = { compactions: 0, statuses: [], briefs: 0 }
  const clock = mock.clock(on)
  inBand(on, counters, 100_000, { exitCode: 1, stdout: '{"text": "nothing to bank", "isError": true}' })

  await $.session.start(START)
  const out = await $.tool.call({ tool: 'mcp__windvane__compact_now' } as never)
  expect(JSON.stringify(out)).toContain('nothing to bank')
  await $.turn.complete(turnEnd())
  await clock.advance(1_000)
  expect(counters.compactions).toBe(0)
})

test('status_segment off: no status line segment, the mirror still runs', { options: { status_segment: false } }, async ($, on) => {
  const counters: Counters = { compactions: 0, statuses: [], briefs: 0 }
  const clock = mock.clock(on)
  inBand(on, counters)

  await $.session.start(START)
  await clock.advance(20_000)
  expect(counters.statuses).toEqual([])
})

test('no sessions folder yet: ticks wait for the engine to create it, then the mirror runs', async ($, on) => {
  const counters: Counters = { compactions: 0, statuses: [], briefs: 0, noSessions: true }
  const clock = mock.clock(on)
  inBand(on, counters, 100_000)

  await $.session.start(START)
  await clock.advance(10_000)
  expect(counters.statuses).toEqual([])

  // The engine's session-start hook made the folder: the next tick runs.
  counters.noSessions = false
  await clock.advance(10_000)
  // 100K of the 750K compaction window, the figure /context shows.
  expect(counters.statuses.join('\n')).toContain('windvane ctx 13%')
})

test('status_segment on (the default): the segment names the fill', async ($, on) => {
  const counters: Counters = { compactions: 0, statuses: [], briefs: 0 }
  mock.clock(on)
  inBand(on, counters)

  await $.session.start(START)
  expect(counters.statuses.join('\n')).toContain('windvane ctx 93%')
})

// The early_compaction row: a fill or a turn cost opens the band below the
// engine's last call; the segment and the mirror say so, and the compaction
// stays the model's call.
test('the early_compaction row reads a fill or a turn cost, nothing else', async () => {
  expect(earlyCompactionOf('40%')).toEqual({ label: '40%', percent: 40 })
  expect(earlyCompactionOf(' 62.5 % ')).toEqual({ label: '62.5%', percent: 62.5 })
  expect(earlyCompactionOf('$0.40')).toEqual({ label: '$0.4', usd: 0.4 })
  expect(earlyCompactionOf('$ 2')).toEqual({ label: '$2', usd: 2 })
  for (const off of ['', '0.4', '40', '0%', '100%', '$0', 'forty', 40, true, undefined]) {
    expect(earlyCompactionOf(off)).toBe(undefined)
  }
})

test('early_compaction at a fill: the band opens there and the engine is told; only compact_now compacts', { options: { early_compaction: '45%' } }, async ($, on) => {
  const counters: Counters = { compactions: 0, statuses: [], briefs: 0, written: {}, toasts: [] }
  const clock = mock.clock(on)
  // 60% of the point: far below the engine's last call (698K), above the row.
  inBand(on, counters, 450_000)

  await $.session.start(START)
  // The band is open for the row's reason: the segment names the mark in its
  // own words (never "now") and the mirror carries the row's value for the
  // engine's note.
  expect(counters.statuses.join('\n')).toContain('compact at step end')
  expect(counters.statuses.join('\n')).not.toContain('checkpoint now')
  const mirror = JSON.parse(counters.written?.[`${STORE}/sessions/${SID}.ctx.json`] ?? '{}')
  expect(mirror.early_band).toBe('45%')

  // A turn boundary does not compact, with or without a save.
  await $.turn.complete(turnEnd())
  await clock.advance(1_000)
  await $.tool.call({ tool: 'mcp__windvane__checkpoint', operation: 'save' } as never)
  await $.turn.complete(turnEnd())
  await clock.advance(1_000)
  expect(counters.compactions).toBe(0)

  // compact_now does, and the toast names the fill as a percent of the point.
  await $.tool.call({ tool: 'mcp__windvane__compact_now' } as never)
  await $.turn.complete(turnEnd())
  await clock.advance(1_000)
  expect(counters.compactions).toBe(1)
  expect(counters.toasts?.join('\n')).toContain('compact_now: draft banked, compacting at 60%')

  // Past the last call the mark changes with the fill.
  counters.tokens = 700_000
  await clock.advance(10_000)
  expect(counters.statuses.at(-1)).toContain('checkpoint now')
})

test('early_compaction at a turn cost: the band opens after a turn that cost that much', { options: { early_compaction: '$0.50' } }, async ($, on) => {
  const counters: Counters = { compactions: 0, statuses: [], briefs: 0, written: {}, cost: 0 }
  const clock = mock.clock(on)
  // The ledger records each counted turn in the store; the turn's cost is
  // what the row reads.
  const store: Record<string, unknown> = {}
  on('store.get', ($, e) => ({ value: store[e.key] }))
  on('store.set', ($, e) => {
    store[e.key] = e.value
    return { value: undefined }
  })
  // 20% of the window: no fill reason at all.
  inBand(on, counters, 200_000)
  const counted = () => ({ ...turnEnd(), usage: { input_tokens: 10, output_tokens: 5, cache_read_input_tokens: 0, cache_creation_input_tokens: 0, model: 'claude-test' } })

  await $.session.start(START)
  expect(JSON.parse(counters.written?.[`${STORE}/sessions/${SID}.ctx.json`] ?? '{}').early_band).toBe(undefined)

  // A cheap turn: the band stays shut.
  counters.cost = 0.3
  await $.turn.complete(counted())
  await clock.advance(10_000)
  expect(counters.statuses.join('\n')).not.toContain('compact at step end')
  expect(JSON.parse(counters.written?.[`${STORE}/sessions/${SID}.ctx.json`] ?? '{}').early_band).toBe(undefined)

  // A turn that cost 0.60: the band opens at its end, before any tick, and
  // the mirror carries the row's value at the next tick. Nothing compacts.
  counters.cost = 0.9
  await $.turn.complete(counted())
  await clock.advance(10_000)
  expect(counters.compactions).toBe(0)
  expect(counters.statuses.at(-1)).toContain('compact at step end')
  expect(JSON.parse(counters.written?.[`${STORE}/sessions/${SID}.ctx.json`] ?? '{}').early_band).toBe('$0.5')

  // The next turn cost nothing more, so the dollar reason is gone again:
  // the row re-arms per turn, and the mirror says so.
  await $.turn.complete(counted())
  await clock.advance(10_000)
  expect(JSON.parse(counters.written?.[`${STORE}/sessions/${SID}.ctx.json`] ?? '{}').early_band).toBe(undefined)
  expect(counters.compactions).toBe(0)
})

// windvane: the mirror is written from the session's own figures, and a turn
// boundary inside the checkpoint band compacts once a deliberate
// checkpoint save has landed, never before; compact_now compacts once; the
// status_segment option hides the status line segment.
//
// The test's hooks are the engine's bottom: an op event (session.id, fs.read,
// env.get ...) answers with { value }, a core event with its result object.
import type { On, SessionUsage } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

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

test('writes the mirror from usage and compacts in the band only after a save', async ($, on) => {
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
  expect(written[`${STORE}/sessions/${SID}.mod`]).toContain('windvane')

  // Inside the band (750K point: trigger 718K, band from 698K) with no
  // save, a turn boundary does not compact.
  tokens = 700_000
  await clock.advance(10_000)
  await $.turn.complete(turnEnd())
  await clock.advance(1_000)
  expect(compactions).toBe(0)

  // A list is no save: still no compaction.
  const listed = await $.tool.call({ tool: 'mcp__windvane__checkpoint', operation: 'list' } as never)
  expect(JSON.stringify(listed)).toContain('Checkpoint saved')
  await $.turn.complete(turnEnd())
  await clock.advance(1_000)
  expect(compactions).toBe(0)

  // A deliberate save lands; the next turn boundary compacts.
  await $.tool.call({ tool: 'mcp__windvane__checkpoint', operation: 'save', task_description: 'x' } as never)
  await $.turn.complete(turnEnd())
  await clock.advance(1_000)
  expect(compactions).toBe(1)
  // windvane's own prompt resumes the work after the compaction it started.
  expect(prompts.length).toBe(1)

  // The compaction opened a new cycle: still in the band, no second
  // compaction until another save.
  await clock.advance(10_000)
  await $.turn.complete(turnEnd())
  await clock.advance(1_000)
  expect(compactions).toBe(1)
  expect(prompts.length).toBe(1)
})

// A session in the band, the store's sessions folder present, the engine
// answering every tool call and every brief.
type Counters = { compactions: number; statuses: string[]; briefs: number; prompts?: string[]; veto?: boolean; noSessions?: boolean }

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
  on('session.usage', () => ({ value: usageAt(tokens) }))
  on('session.compact', () => {
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
  on('fs.write', () => ({ value: undefined }))
  on('ui.status', ($, e) => {
    counters.statuses.push(JSON.stringify(e))
    return { value: undefined }
  })
  on('ui.toast', () => ({ value: undefined }))
  on('ui.log', () => ({ value: undefined }))
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
  expect(counters.statuses.join('\n')).toContain('windvane ctx 10%')
})

test('status_segment on (the default): the segment names the fill', async ($, on) => {
  const counters: Counters = { compactions: 0, statuses: [], briefs: 0 }
  mock.clock(on)
  inBand(on, counters)

  await $.session.start(START)
  expect(counters.statuses.join('\n')).toContain('windvane ctx 70%')
})

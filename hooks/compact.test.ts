// windvane: the conversation a compaction becomes carries windvane's
// rules and the checkpoint the SessionStart(compact) banner would restore,
// as one user-role message right after the summary. A precompute, a
// subagent's own compaction, a skip and an empty brief pass through
// untouched.
//
// The test's hooks are the engine's bottom: op events answer { value },
// session.compact answers { messages }.
import type { On } from 'claude-code'
import { expect, test } from 'claude-code/testing'

const SID = 'aaaaaaaa-0000-4000-8000-00000000000c'
const RULES = ['Rules (1, proj):', '  [r1] never push without the word']
const CHECKPOINT = ['CHECKPOINT [manual, 0.2h ago, proj, task_7]: wire the brief', '  Pending (1):', '    - the compact hook']

const SUMMARY = { role: 'user' as const, text: 'Summary of the conversation so far.', toolUses: [] }
const KEPT = { role: 'assistant' as const, text: 'Continuing.', toolUses: [] }
const BEFORE = [
  { role: 'user' as const, text: 'Build the thing.', toolUses: [] },
  { role: 'assistant' as const, text: 'Building.', toolUses: [] },
]

function engine(on: On, answer: () => object, runs: (readonly string[])[], writes: string[] = []) {
  on('env.get', ($, e) => ({ value: e.name === 'USERPROFILE' ? 'C:/Users/nobody' : undefined }))
  on('session.id', () => ({ value: SID }))
  on('session.cwd', () => ({ value: 'E:/demo/proj' }))
  on('ui.log', () => ({ value: undefined }))
  on('fs.write', ($, e) => {
    writes.push(e.path.replace(/\\/g, '/'))
    return { value: undefined }
  })
  on('process.run', ($, e) => {
    runs.push(e.argv)
    return { value: { exitCode: 0, stdout: JSON.stringify(answer()), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
}

test('the compacted conversation carries the rules and the checkpoint after the summary', async ($, on) => {
  const runs: (readonly string[])[] = []
  const writes: string[] = []
  let answer: object = { rules: RULES, files: {}, checkpoint: CHECKPOINT }
  engine(on, () => answer, runs, writes)
  on('session.compact', () => ({ messages: [SUMMARY, KEPT], tokensBefore: 700_000, tokensAfter: 20_000 }))

  const out = await $.session.compact({ trigger: 'auto', messages: BEFORE })
  expect(runs.length).toBe(1)
  // The banner is told the conversation already carries them.
  expect(writes).toEqual([`C:/Users/nobody/.windvane/sessions/${SID}.briefed`])
  expect(runs[0]!).toContain('--checkpoint')
  expect(runs[0]!.slice(0, 3)).toEqual(['python', '-m', 'windvane.brief'])
  expect(runs[0]!.join(' ')).toContain(`--project E:/demo/proj --session ${SID} --json`)
  expect(out.skip).toBeUndefined()
  const msgs = out.messages ?? []
  expect(msgs.length).toBe(3)
  expect(msgs[0]!.text).toBe(SUMMARY.text)
  expect(msgs[1]!.role).toBe('user')
  expect(msgs[1]!.text).toBe('<windvane-compact>\n' + RULES.join('\n') + '\n\n' + CHECKPOINT.join('\n') + '\n</windvane-compact>')
  expect(msgs[2]!.text).toBe(KEPT.text)
  expect(out.skip === undefined ? out.tokensBefore : 0).toBe(700_000)

  // A /compact the person typed carries it too.
  const manual = await $.session.compact({ trigger: 'manual', messages: BEFORE })
  expect((manual.messages ?? []).length).toBe(3)

  // Nothing to say: the conversation stays as core handed it up.
  answer = { rules: [], files: {}, checkpoint: [] }
  const plain = await $.session.compact({ trigger: 'auto', messages: BEFORE })
  expect((plain.messages ?? []).map(m => m.text)).toEqual([SUMMARY.text, KEPT.text])
})

test('a precompute, a subagent compaction and a skip pass through untouched', async ($, on) => {
  const runs: (readonly string[])[] = []
  engine(on, () => ({ rules: RULES, files: {}, checkpoint: CHECKPOINT }), runs)
  let skip = false
  on('session.compact', () => (skip ? { skip: 'blocked by a test' } : { messages: [SUMMARY, KEPT] }))

  // A precompute's result is kept for a later compaction, which is then
  // dispatched with its own trigger: the message is added there, not here.
  const pre = await $.session.compact({ trigger: 'precompute', messages: BEFORE })
  expect((pre.messages ?? []).length).toBe(2)

  // A subagent's own transcript: the main session's checkpoint is not its.
  const sub = await $.session.compact({ trigger: 'auto', agentId: 'agent-1', messages: BEFORE })
  expect((sub.messages ?? []).length).toBe(2)

  skip = true
  const skipped = await $.session.compact({ trigger: 'auto', messages: BEFORE })
  expect(skipped.skip).toBe('blocked by a test')
  expect(runs.length).toBe(0)
})

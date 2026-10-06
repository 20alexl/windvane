// windvane: the band above the prompt says what the model last read
// from windvane, counted from the hook-context rows windvane's hooks append, and
// draws nothing until there is something to say or once it is hidden.
//
// The test's hooks are the engine's bottom: an op event answers { value },
// a core event its result object.
//
// session.append is the exception: on 2.1.289 the kit has no stand-in for
// its bottom (a test hook that answers the row without next is skipped, one
// that calls next reaches nothing, for every door), so each append rejects
// with "no implementation for session.append" once the plugins have run.
// The band records its reading before it calls next, so `append` below
// raises the row through the plugins and swallows exactly that rejection.
import type { Engine } from 'claude-code/testing'
import { expect, mock, test } from 'claude-code/testing'

import { bandLine, parseWindvane } from '../../hooks/band'

const SID = 'aaaaaaaa-0000-4000-8000-00000000000b'
const STORE = 'C:/tmp/windvane-band-store'
const SURFACES = ['terminal', 'desktop'] as const

const BAND = {
  component: 'AbovePrompt' as const,
  props: { hasSurvey: false, isWorking: false, maxRows: 6, bodyColumns: 100, scroll: { offset: 0, bodyRows: 6 }, view: {} },
}

let rows = 0
function hookRow(text: string, door: 'hook-context' | 'note' = 'hook-context') {
  rows += 1
  return {
    message: { type: 'attachment' as const, name: 'hook_additional_context', content: [{ type: 'text', text }] },
    door,
    origin: { kind: 'hook' as const, event: 'PreToolUse' },
    uuid: `row-${rows}`,
  }
}

// The band's line: a Text keeps no key, so it is found by what it shows.
const LINE = { type: 'Text', text: /^windvane/ }

async function append($: Engine, row: ReturnType<typeof hookRow>): Promise<void> {
  try {
    await $.session.append(row)
  } catch (err) {
    if (!String(err).includes('no implementation for session.append')) throw err
  }
}

const BANNER = [
  'windvane session started (compact)',
  'Rules (3, demo):',
  '  [r1] always run the targeted tests',
  '  [r2] never push',
  '  [r3] trash over rm',
  'Past mistakes: 7 tracked for demo (file-specific, shown before edits)',
].join('\n')

const PRE_EDIT = [
  '<windvane-edit-reminder>',
  'AUTO-CHECK: Past mistakes with this file:',
  '  - dropped the index in db.py',
  '  - db.py: forgot the migration',
  '',
  'Relevant memories for this file:',
  '  - something else',
  '</windvane-edit-reminder>',
].join('\n')

const RULE_AND_CHECKPOINT = [
  '<windvane-rule>This call matches a rule with a detector:',
  '  [r9] trash over rm -- rm -rf build',
  '  Recorded for the run report. A permission prompt stands between you and the call.',
  '</windvane-rule>',
  '<windvane-context>CHECKPOINT NOW: 18K tokens to the auto-compaction trigger (~718K).</windvane-context>',
].join('\n')
const EARLY_BAND =
  '<windvane-context>CHECKPOINT AT THE END OF THIS STEP: the early_compaction setting (45%) opens the checkpoint band here, with 395K tokens to the auto-compaction trigger (~718K), so there is no hurry.</windvane-context>'

test('the band counts what windvane injected and hides on request', async ($, on) => {
  const stored: Record<string, unknown> = {}
  on('store.get', ($, e) => ({ value: stored[e.key] }))
  on('store.set', ($, e) => {
    stored[e.key] = e.value
    return { value: undefined }
  })
  mock.env(on, { WINDVANE_DIR: STORE })
  mock.clock(on, { now: Date.now() })

  on('session.id', () => ({ value: SID }))
  on('session.model', () => ({ value: 'claude-test' }))
  on('session.root', () => ({ value: 'E:\\demo' }))
  on('session.cwd', () => ({ value: 'E:\\demo' }))
  on('session.usage', () => ({
    value: { startedAt: 0, context: { tokens: 590_000, window: 1_000_000, percent: 59 }, rateLimits: [] },
  }))
  const fwd = (p: string) => p.replace(/\\/g, '/')
  on('fs.exists', ($, e) => ({ value: fwd(e.path).endsWith('/sessions') || fwd(e.path).endsWith('latest_handoff.json') }))
  on('fs.read', ($, e) => {
    const p = fwd(e.path)
    if (p.endsWith('manifest.json')) return { value: JSON.stringify({ projects: { 'e:/demo': { hash: 'dddd0001' } } }) }
    if (p.endsWith('latest_handoff.json')) return { value: JSON.stringify({ created: Date.now() / 1000 - 12 * 60, kind: 'manual' }) }
    return { value: '' }
  })
  on('fs.write', () => ({ value: undefined }))
  on('command.register', ($, e) => ({ value: { command: e.name } }))
  on('ui.status', () => ({ value: undefined }))
  on('ui.log', () => ({ value: undefined }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  // The engine's own band, where windvane passes: an empty Box keyed apart.
  on('ui.render', ($, e) => $.ui.resolve(e).Box({ key: 'engine-own' }))

  await $.session.start({ cwd: 'E:/demo', surface: 'terminal', isInteractive: true })

  // Nothing read from windvane yet: nothing drawn.
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ plugin: 'windvane', surface, ...BAND })
    expect(await ui.find(LINE)).toBeUndefined()
    await ui.unmount()
  }

  // The session banner: its rule count, with the status line's figures. The
  // fill is against the compaction window: no breakdown in the usage above,
  // so the default point on a 1M window, 967K, and 590K of it is 61%.
  await append($, hookRow(BANNER))
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ plugin: 'windvane', surface, ...BAND })
    expect((await ui.find(LINE))?.text).toContain('windvane · session compact · 3 rules · ckpt 12m · ctx 61%')
    await ui.unmount()
  }

  // A pre-edit row: the mistakes it showed, nothing else counted.
  await append($, hookRow(PRE_EDIT))
  // A row of another door, and a hook row with nothing of windvane's, change nothing.
  await append($, hookRow('<windvane-rule>\n  [x1] not windvane\n</windvane-rule>', 'note'))
  await append($, hookRow('some other hook said this'))
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ plugin: 'windvane', surface, ...BAND })
    const text = (await ui.find(LINE))?.text ?? ''
    expect(text).toContain('windvane · 2 mistakes · ckpt 12m')
    expect(text).not.toContain('rule')
    await ui.unmount()
  }

  // A rule match and the checkpoint call in one row.
  await append($, hookRow(RULE_AND_CHECKPOINT))
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ plugin: 'windvane', surface, ...BAND })
    expect((await ui.find(LINE))?.text).toContain('windvane · 1 rule · CHECKPOINT NOW')
    await ui.unmount()
  }

  // The early band's note asks for the save at the end of the step, and the
  // band says so in its own words, never NOW.
  await append($, hookRow(EARLY_BAND))
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ plugin: 'windvane', surface, ...BAND })
    const text = (await ui.find(LINE))?.text ?? ''
    expect(text).toContain('windvane · checkpoint at step end')
    expect(text).not.toContain('NOW')
    await ui.unmount()
  }

  // A row with only a tag names the tag.
  await append($, hookRow('<windvane-read-context>db.py: 3 importers</windvane-read-context>'))
  const ui = await $.ui.mount({ plugin: 'windvane', surface: 'desktop', ...BAND })
  expect((await ui.find(LINE))?.text).toContain('windvane · read-context')

  // Hide: nothing drawn, and the press is kept for the next session.
  await ui.press({ key: 'windvane-hide' })
  expect(await ui.find(LINE)).toBeUndefined()
  expect(stored.bandHidden).toBe(true)
  await ui.unmount()
  for (const surface of SURFACES) {
    const again = await $.ui.mount({ plugin: 'windvane', surface, ...BAND })
    expect(await again.find(LINE)).toBeUndefined()
    await again.unmount()
  }
})

test('a Hide kept in the store holds from the session start', async ($, on) => {
  mock.store(on, { bandHidden: true })
  mock.env(on, { WINDVANE_DIR: STORE })
  on('session.id', () => ({ value: SID }))
  on('session.model', () => ({ value: 'claude-test' }))
  on('session.root', () => ({ value: 'E:/demo' }))
  on('session.cwd', () => ({ value: 'E:/demo' }))
  on('fs.exists', () => ({ value: false }))
  on('fs.read', () => ({ value: '{}' }))
  on('command.register', ($, e) => ({ value: { command: e.name } }))
  on('ui.log', () => ({ value: undefined }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('ui.render', ($, e) => $.ui.resolve(e).Box({ key: 'engine-own' }))

  await $.session.start({ cwd: 'E:/demo', surface: 'terminal', isInteractive: true })
  await append($, hookRow(BANNER))
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ plugin: 'windvane', surface, ...BAND })
    expect(await ui.find(LINE)).toBeUndefined()
    await ui.unmount()
  }
})

test('the counts are read from the text alone', () => {
  // Three rule lines in two blocks; the context line under a block is no rule.
  const rules = parseWindvane(
    '<windvane-rule>m:\n  [a1] one -- x\n  [a2] two -- y\n  Recorded.\n</windvane-rule>\n<windvane-rule>m:\n  [a3] three -- z\n</windvane-rule>',
    1,
  )
  expect(rules?.rules).toBe(3)
  // Mistake lines end at the first line that is not one.
  const mistakes = parseWindvane(`<windvane-edit-reminder>\n${PRE_EDIT}`, 1)
  expect(mistakes?.mistakes).toBe(2)
  // The heads-up and a stall, each named; nothing else counted.
  const r = parseWindvane('<windvane-context>Context pressure: 650K used</windvane-context>\n<windvane-stall>3 turns</windvane-stall>', 5)
  expect(r && bandLine(r, null, 0)).toBe('windvane · heads-up · stall')
  expect(r && bandLine(r, { percent: 61 }, 0)).toBe('windvane · heads-up · stall · ckpt none · ctx 61%')
  // Not windvane's: nothing.
  expect(parseWindvane('a hook from somewhere else', 1)).toBe(null)
})

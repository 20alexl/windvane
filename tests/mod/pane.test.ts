// windvane: /windvane opens a pane with the newest checkpoint whole,
// the project's rules (its own and the ones it inherits) and the mistakes
// for the file the model last touched, read from the store's own files.
//
// The test's hooks are the engine's bottom: an op event answers { value },
// a core event its result object. Paths reach fs hooks in Windows spelling.
import { expect, mock, test } from 'claude-code/testing'

const SID = 'aaaaaaaa-0000-4000-8000-00000000000c'
const STORE = 'C:/tmp/windvane-pane-store'
const SURFACES = ['terminal', 'desktop'] as const
const NOW = Date.now() / 1000

const PANE = {
  component: 'Pane' as const,
  requestId: 'windvane',
  props: {
    title: 'windvane',
    isFocused: false,
    bodyColumns: 100,
    placement: 'dock' as const,
    scroll: { offset: 0, bodyRows: 60 },
    view: {},
  },
}

const MANIFEST = { projects: { 'e:/demo': { hash: 'root0001' }, 'e:/demo/proj': { hash: 'proj0001' } } }

// The project's ring: a deliberate checkpoint, every list in the ring's
// vocabulary. The root's ring holds an older one.
const RING_PROJ = {
  created: NOW - 5 * 60,
  kind: 'manual',
  task_id: 'task_7',
  project_path: 'E:/demo/proj',
  task_description: 'Port the index to the new schema',
  summary: 'Index ported; the migration test is next',
  current_step: 'Step 3: migration test',
  completed_steps: ['Step 1: schema', 'Step 2: port'],
  next_steps: ['Step 3: migration test', 'Step 4: docs'],
  files_in_progress: ['src/db.py', 'tests/test_db.py'],
  warnings: ['Do not touch legacy.py'],
  context_needed: ['docs/schema.md'],
  goal: 'ship the schema',
}
const RING_ROOT = { created: NOW - 3600, kind: 'auto', task_description: 'older root work' }

const MEMORY_PROJ = {
  entries: [
    { id: 'r-own', category: 'rule', content: 'run the targeted tests only', relevance: 9 },
    { id: 'm-new', category: 'mistake', content: 'db.py: forgot the index on user_id', created_at: 300 },
    { id: 'm-old', category: 'discovery', content: 'MISTAKE: src/db.py dropped the migration', created_at: 100 },
    { id: 'm-gone', category: 'mistake', content: 'db.py: archived one', created_at: 400, archived_at: 500 },
    { id: 'm-file', category: 'mistake', content: 'the pool closed early', related_files: ['E:\\demo\\proj\\src\\db.py'], created_at: 200 },
    { id: 'm-other', category: 'mistake', content: 'other.py: wrong import', created_at: 350 },
    { id: 'm-near', category: 'mistake', content: 'mydb.py: not this file', created_at: 360 },
    { id: 'd-1', category: 'decision', content: 'DECISION: use sqlite', created_at: 10 },
  ],
}
const MEMORY_ROOT = {
  entries: [
    { id: 'r-root', category: 'rule', content: 'never push without the word', relevance: 5 },
    { id: 'r-own', category: 'rule', content: 'a stale root copy of the same id', relevance: 1 },
  ],
}

function files(): Record<string, string> {
  return {
    [`${STORE}/manifest.json`]: JSON.stringify(MANIFEST),
    [`${STORE}/projects/proj0001/latest_handoff.json`]: JSON.stringify(RING_PROJ),
    [`${STORE}/projects/root0001/latest_handoff.json`]: JSON.stringify(RING_ROOT),
    [`${STORE}/projects/proj0001/memory.json`]: JSON.stringify(MEMORY_PROJ),
    [`${STORE}/projects/root0001/memory.json`]: JSON.stringify(MEMORY_ROOT),
  }
}

const RUN = { args: '', origin: { kind: 'composer' as const }, presentation: { isFullscreen: true, columns: 160 } }

test('/windvane shows the checkpoint, the rules and the touched file mistakes', async ($, on) => {
  const disk = files()
  const fwd = (p: string) => p.replace(/\\/g, '/')
  const opened: unknown[] = []
  const stored: Record<string, unknown> = { bandHidden: true }
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
  on('session.cwd', () => ({ value: 'E:\\demo\\proj' }))
  on('fs.exists', ($, e) => ({ value: fwd(e.path) in disk }))
  on('fs.read', ($, e) => {
    const p = fwd(e.path)
    const text = disk[p]
    if (text === undefined) throw new Error(`ENOENT ${p}`)
    return { value: text }
  })
  const commands: string[] = []
  on('command.register', ($, e) => {
    commands.push(e.name)
    return { value: { command: e.name } }
  })
  on('ui.open', ($, e) => {
    opened.push(e)
    return { value: { isPlaced: true as const } }
  })
  // The pane asks for rows inline, takes the keys (the arrows scroll it) and
  // closes on Escape.
  const openArgs = () => opened[0] as { rows?: number; focus?: true; closeOnEscape?: true }
  on('ui.log', () => ({ value: undefined }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('tool.call', () => ({ result: { content: [{ type: 'text', text: 'ok' }] } }))

  await $.session.start({ cwd: 'E:/demo/proj', surface: 'terminal', isInteractive: true })
  expect(commands).toContain('windvane')
  expect(commands).toContain('remember')

  // Before any touch: no file named.
  await $.command.run({ command: 'windvane', ...RUN })
  expect(opened).toHaveLength(1)
  expect(openArgs().rows).toBe(24)
  expect(openArgs().focus).toBe(true)
  expect(openArgs().closeOnEscape).toBe(true)
  const first = await $.ui.mount({ plugin: 'windvane', surface: 'terminal', ...PANE })
  expect(await first.find({ type: 'Text', text: 'no file touched yet' })).toBeDefined()
  await first.unmount()

  // The model edits db.py (a subagent's read of other.py does not count).
  await $.tool.call({ tool: 'Edit', file_path: 'E:\\demo\\proj\\src\\db.py', old_string: 'a', new_string: 'b' } as never)
  await $.tool.call({ tool: 'Read', file_path: 'E:\\demo\\proj\\src\\other.py', agentId: 'sub-1' } as never)
  const answer = await $.command.run({ command: 'windvane', ...RUN })
  expect(answer.text).toContain('windvane')

  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ plugin: 'windvane', surface, ...PANE })
    const text = (await ui.findAll({ type: 'Text' })).map(t => t.text).join('\n')

    // The summary row: what the pane holds (no pressure figures in this test).
    expect(text).toContain('2 rules · 3 mistakes for db.py')

    // The checkpoint whole, from the project's ring (newer than the root's):
    // a labelled column, one item per row, paths by their last segment.
    expect(text).toContain('Checkpoint · manual · 5m ago · task_7 · proj')
    expect(text).toContain('  Task       Port the index to the new schema')
    expect(text).toContain('  Step       Step 3: migration test')
    expect(text).toContain('  Done 2     Step 1: schema\n             Step 2: port')
    expect(text).toContain('  Pending 2  Step 3: migration test\n             Step 4: docs')
    expect(text).toContain('  Files 2    db.py, test_db.py')
    expect(text).toContain('  Warnings 1 Do not touch legacy.py')
    expect(text).toContain('  Needed 1   docs/schema.md')
    expect(text).toContain('  Handoff    Index ported; the migration test is next')
    expect(text).toContain('  Goal       ship the schema')
    expect(text).not.toContain('older root work')

    // The rules: the project's own first, the inherited one after, one copy per id.
    expect(text).toContain('Rules · 2 · proj\n  [r-own] run the targeted tests only\n  [r-root] never push without the word')
    expect(text).not.toContain('stale root copy')

    // The mistakes for db.py, newest first: by name, by MISTAKE: prefix, by related file.
    expect(text).toContain('Mistakes · 3 · db.py')
    expect(text).toContain('  [m-new] db.py: forgot the index on user_id\n  [m-file] the pool closed early\n  [m-old] src/db.py dropped the migration')
    expect(text).not.toContain('archived one')
    expect(text).not.toContain('other.py')
    expect(text).not.toContain('mydb.py')

    expect(await ui.find({ key: 'windvane-refresh' })).toBeDefined()
    await ui.unmount()
  }

  // The band was hidden last session: the pane offers it back.
  const ui = await $.ui.mount({ plugin: 'windvane', surface: 'desktop', ...PANE })
  await ui.press({ key: 'windvane-show-band' })
  expect(stored.bandHidden).toBe(false)
  expect(await ui.find({ key: 'windvane-show-band' })).toBeUndefined()

  // Refresh reads the store again.
  disk[`${STORE}/projects/proj0001/latest_handoff.json`] = JSON.stringify({ ...RING_PROJ, current_step: 'Step 4: docs' })
  await ui.press({ key: 'windvane-refresh' })
  expect(await ui.find({ type: 'Text', text: 'Step       Step 4: docs' })).toBeDefined()
  await ui.unmount()
})

test('an empty store says so instead of failing', async ($, on) => {
  mock.store(on)
  mock.env(on, { WINDVANE_DIR: STORE })
  on('session.root', () => ({ value: 'E:/elsewhere' }))
  on('session.cwd', () => ({ value: 'E:/elsewhere' }))
  on('fs.exists', () => ({ value: false }))
  on('fs.read', ($, e) => {
    throw new Error(`ENOENT ${e.path}`)
  })
  on('ui.open', () => ({ value: { isPlaced: true as const } }))

  await $.command.run({ command: 'windvane', ...RUN })
  for (const surface of SURFACES) {
    const ui = await $.ui.mount({ plugin: 'windvane', surface, ...PANE })
    const text = (await ui.findAll({ type: 'Text' })).map(t => t.text).join('\n')
    expect(text).toContain('Checkpoint · none in the ring')
    expect(text).toContain('Rules · 0\n  none')
    await ui.unmount()
  }
})

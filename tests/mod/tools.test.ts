// windvane: the tools the model calls are declared at session.start and
// served by the mod. Each call goes to the daemon first (POST /tool, the
// port from the store's daemon_port file) and to `python -m windvane.tools`
// when the daemon is down, the same request on stdin with the engine on
// PYTHONPATH and the session id in env. The model reads what the engine
// answered; a failure is a deny. compact_now compacts after it returns.
//
// The test's hooks are the engine's bottom: an op event answers { value },
// a core event its result object.
import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'

const STORE = 'C:/tmp/windvane-tools-store'
const SID = 'aaaaaaaa-0000-4000-8000-00000000000a'
const PORT = 47124
const TOKEN = 'f'.repeat(64) // the store's daemon_token; the daemon refuses a POST without it

type Run = { argv: readonly string[]; init?: { stdin?: string; env?: Record<string, string>; timeoutMs?: number } }
type Fetched = { url: string; headers: Record<string, string>; body: Record<string, unknown> }

function ran(stdout: string, exitCode = 0, stderr = '') {
  return { value: { exitCode, stdout, stderr, isStdoutTruncated: false, isStderrTruncated: false } }
}

function answered(status: number, body: object | string) {
  return { value: { status, ok: status === 200, headers: {}, text: typeof body === 'string' ? body : JSON.stringify(body) } }
}

type Daemon = 'none' | 'throws' | { status: number; body: object | string } | 'hangs'

// The session, the store and the engine. `daemon` says what the daemon does;
// `reply` is the subprocess's answer.
function engine(on: On, setup: { daemon: Daemon; reply?: ReturnType<typeof ran> | 'throws'; env?: Record<string, string> }) {
  const runs: Run[] = []
  const fetched: Fetched[] = []
  const clock = mock.clock(on)
  const env: Record<string, string> = { WINDVANE_DIR: STORE, USERPROFILE: 'C:/Users/nobody', ...(setup.env ?? {}) }
  on('env.get', ($, e) => ({ value: env[e.name] }))
  on('session.cwd', () => ({ value: 'E:\\demo\\proj' }))
  on('session.id', () => ({ value: SID }))
  on('ui.log', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  on('fs.read', ($, e) => {
    const p = e.path.replace(/\\/g, '/')
    if (p === `${STORE}/daemon_port` && setup.daemon !== 'none') return { value: `${PORT}\n` }
    if (p === `${STORE}/daemon_token` && setup.daemon !== 'none') return { value: `${TOKEN}\n` }
    throw new Error(`ENOENT ${p}`)
  })
  on('http.fetch', ($, e) => {
    fetched.push({ url: e.url, headers: e.init?.headers ?? {}, body: JSON.parse(e.init?.body ?? '{}') })
    const d = setup.daemon
    if (d === 'throws') throw new Error('ECONNREFUSED')
    if (d === 'hangs') return new Promise<never>(() => {})
    if (d === 'none') throw new Error('no daemon')
    return answered(d.status, d.body)
  })
  on('process.run', ($, e) => {
    runs.push(e as Run)
    const r = setup.reply ?? ran('{"text": "from the subprocess", "isError": false, "ms": 900}')
    if (r === 'throws') throw new Error('ENOENT')
    return r
  })
  return { runs, fetched, clock }
}

test('session.start declares the six tools', async ($, on) => {
  const registered: string[] = []
  mock.env(on, { WINDVANE_DIR: STORE, USERPROFILE: 'C:/Users/nobody' })
  mock.clock(on)
  on('session.id', () => ({ value: SID }))
  on('session.model', () => ({ value: 'claude-x' }))
  on('session.usage', () => ({ value: { startedAt: 0, context: { tokens: 1000, window: 1_000_000, percent: 0 }, rateLimits: [] } }))
  on('session.cwd', () => ({ value: 'E:\\demo\\proj' }))
  on('session.root', () => ({ value: 'E:\\demo' }))
  on('fs.exists', () => ({ value: false }))
  on('fs.read', () => ({ value: '' }))
  on('store.get', () => ({ value: undefined }))
  on('ui.log', () => ({ value: undefined }))
  on('command.register', ($, e) => ({ value: { command: e.name } }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  const schemas: Record<string, unknown> = {}
  on('tool.register', ($, e) => {
    registered.push(e.name)
    schemas[e.name] = e.inputSchema
    return { value: { tool: `mcp__windvane__${e.name}` } }
  })

  await $.session.start({ cwd: 'E:/demo/proj', surface: 'terminal', isInteractive: true })

  expect(registered).toEqual(['checkpoint', 'compact_now', 'memory', 'log', 'mine', 'deps'])
  const ops = (name: string) => ((schemas[name] as { properties: { operation: { enum: string[] } } }).properties.operation.enum)
  expect(ops('checkpoint')).toEqual(['save', 'restore', 'list'])
  expect(ops('log')).toEqual(['mistake', 'decision'])
  expect(ops('deps')).toEqual(['map', 'impact'])
  expect(ops('memory')).toEqual([
    'remember', 'recall', 'recent', 'search', 'archive_search', 'forget', 'add_rule', 'list_rules',
    'modify', 'delete', 'promote', 'archive', 'restore', 'list_mistakes', 'acknowledge_mistake', 'set_detector',
  ])
  expect(ops('mine')).toEqual(['search', 'decisions', 'errors', 'struggles', 'replay', 'timeline', 'run_report', 'run_status', 'status', 'reindex'])
  const mineMode = (schemas.mine as { properties: { mode: { enum: string[] } } }).properties.mode
  expect(mineMode.enum).toEqual(['bootstrap', 'incremental'])
})

test('mine reindex passes its mode to the engine as given', async ($, on) => {
  const t = engine(on, { daemon: { status: 200, body: { text: 'Reindex started (incremental).', isError: false, ms: 40 } } })
  const out = await $.tool.call({ tool: 'mcp__windvane__mine', operation: 'reindex', mode: 'incremental' } as never)
  expect(out.result).toBe('Reindex started (incremental).')
  expect(t.fetched[0]?.body.arguments).toEqual({ operation: 'reindex', mode: 'incremental' })
})

test('memory archive_search sends its query and limit', async ($, on) => {
  const t = engine(on, { daemon: 'none' })
  await $.tool.call({ tool: 'mcp__windvane__memory', operation: 'archive_search', query: 'auth', limit: 5 } as never)
  expect(JSON.parse(t.runs[0]?.init?.stdin ?? '{}').arguments).toEqual({ operation: 'archive_search', query: 'auth', limit: 5 })
})

test('the daemon answers first: one POST, no process', async ($, on) => {
  const t = engine(on, { daemon: { status: 200, body: { text: 'Checkpoint saved: task_123', isError: false, ms: 12 } } })

  const out = await $.tool.call({ tool: 'mcp__windvane__checkpoint', operation: 'save', current_step: 'wiring', tool_use_id: 'tu-1' } as never)

  expect(out.deny).toBe(undefined)
  expect(out.result).toBe('Checkpoint saved: task_123')
  expect(t.runs).toHaveLength(0)
  expect(t.fetched).toHaveLength(1)
  const f = t.fetched[0]!
  expect(f.url).toBe(`http://127.0.0.1:${PORT}/tool`)
  expect(f.headers['X-Windvane-Hook']).toBe('1')
  expect(f.headers['X-Windvane-Token']).toBe(TOKEN)
  expect(f.body.tool).toBe('checkpoint')
  const args = f.body.arguments as Record<string, unknown>
  expect(args.operation).toBe('save')
  expect(args.current_step).toBe('wiring')
  // No project_path given: none is sent; the engine resolves the project.
  expect('project_path' in args).toBe(false)
  expect(args.tool_use_id).toBe(undefined)
  // The session id keys the engine's state; the session's working directory
  // stands in for the daemon's own, which is the engine folder.
  expect(f.body.env).toEqual({ CLAUDE_CODE_SESSION_ID: SID, WINDVANE_DIR: STORE, CLAUDE_PROJECT_DIR: 'E:\\demo\\proj' })
})

test('a refused connection falls back to the subprocess with the same request', async ($, on) => {
  const t = engine(on, { daemon: 'throws', env: { WINDVANE_PYTHON: 'E:/py/venv/Scripts/python.exe' } })

  const out = await $.tool.call({ tool: 'mcp__windvane__memory', operation: 'remember', content: 'The cache is sqlite.', project_path: 'E:/other' } as never)

  expect(out.result).toBe('from the subprocess')
  expect(t.fetched).toHaveLength(1)
  expect(t.runs).toHaveLength(1)
  const run = t.runs[0]!
  expect(run.argv).toEqual(['E:/py/venv/Scripts/python.exe', '-m', 'windvane.tools'])
  const request = JSON.parse(run.init?.stdin ?? '{}')
  expect(request.tool).toBe('memory')
  expect(request.arguments.content).toBe('The cache is sqlite.')
  expect(request.arguments.project_path).toBe('E:/other')
  expect(request.env).toEqual({ CLAUDE_CODE_SESSION_ID: SID, WINDVANE_DIR: STORE, CLAUDE_PROJECT_DIR: 'E:\\demo\\proj' })
  // The engine ships inside the plugin; the process has no session env of its own.
  const env = run.init?.env ?? {}
  expect(env.PYTHONPATH?.endsWith('/windvane')).toBe(true)
  expect(env.PYTHONPATH!.length).toBeGreaterThan('/windvane'.length)
  expect(env.CLAUDE_CODE_SESSION_ID).toBe(SID)
  expect(env.WINDVANE_DIR).toBe(STORE)
  expect(env.CLAUDE_PROJECT_DIR).toBe('E:\\demo\\proj')
  expect(env.PYTHONIOENCODING).toBe('utf-8')
})

test('no port file: nothing is fetched, the subprocess answers', async ($, on) => {
  const t = engine(on, { daemon: 'none' })
  const out = await $.tool.call({ tool: 'mcp__windvane__deps', operation: 'map', symbol: 'storePath' } as never)
  expect(out.result).toBe('from the subprocess')
  expect(t.fetched).toHaveLength(0)
  expect(t.runs[0]?.argv).toEqual(['python', '-m', 'windvane.tools'])
  expect(JSON.parse(t.runs[0]?.init?.stdin ?? '{}').tool).toBe('deps')
})

test('a daemon that answers 500 or a body that is no answer falls back', async ($, on) => {
  const t = engine(on, { daemon: { status: 500, body: 'boom' } })
  expect((await $.tool.call({ tool: 'mcp__windvane__log', operation: 'decision', decision: 'sqlite' } as never)).result).toBe('from the subprocess')
  expect(t.runs).toHaveLength(1)
})

test('the python option names the interpreter; WINDVANE_PYTHON wins over it', { options: { python: 'D:/py/python.exe' } }, async ($, on) => {
  const t = engine(on, { daemon: 'none' })
  await $.tool.call({ tool: 'mcp__windvane__mine', operation: 'status' } as never)
  expect(t.runs[0]?.argv[0]).toBe('D:/py/python.exe')
})

test('an engine failure reaches the model as a deny, never a thrown hook', async ($, on) => {
  engine(on, { daemon: { status: 200, body: { text: 'checkpoint failed: ValueError: no project', isError: true, ms: 5 } } })
  const viaDaemon = await $.tool.call({ tool: 'mcp__windvane__checkpoint', operation: 'restore' } as never)
  expect(viaDaemon.result).toBe(undefined)
  expect(viaDaemon.deny).toContain('ValueError: no project')
})

test('a subprocess that exits without an answer is a deny with its stderr', async ($, on) => {
  engine(on, { daemon: 'none', reply: ran('', 1, 'No module named windvane') })
  const out = await $.tool.call({ tool: 'mcp__windvane__memory', operation: 'recall' } as never)
  expect(out.deny).toContain('No module named windvane')
})

test('no python at all is said in the deny, with the option to set', async ($, on) => {
  engine(on, { daemon: 'none', reply: 'throws' })
  const out = await $.tool.call({ tool: 'mcp__windvane__memory', operation: 'recall' } as never)
  expect(out.deny).toContain('python option')
  expect(out.deny).toContain('WINDVANE_PYTHON')
})

test('a daemon that times out is not repeated through the subprocess', async ($, on) => {
  const t = engine(on, { daemon: 'hangs' })
  const pending = $.tool.call({ tool: 'mcp__windvane__memory', operation: 'remember', content: 'x' } as never)
  await t.clock.advance(61_000)
  const out = await pending
  expect(out.deny).toContain('may still have run')
  expect(t.runs).toHaveLength(0)
})

test('compact_now answers with the bank and says the compaction waits for the turn to end', async ($, on) => {
  let compactions = 0
  const setup: { daemon: Daemon } = { daemon: { status: 200, body: { text: 'Draft banked as task_4.', isError: false, ms: 30 } } }
  const t = engine(on, setup)
  on('session.compact', () => {
    compactions += 1
    return { messages: [{ role: 'user' as const, text: 'Summary.', toolUses: [] }] }
  })

  const out = await $.tool.call({ tool: 'mcp__windvane__compact_now' } as never)
  expect(out.result).toBe('Draft banked as task_4.\nwindvane: the conversation compacts as soon as this turn ends; end the turn now.')
  expect(t.fetched[0]?.body.tool).toBe('compact_now')
  expect(t.fetched[0]?.body.arguments).toEqual({})
  // Not from the tool call: $.session.compact rejects while a turn runs.
  await t.clock.advance(1_000)
  expect(compactions).toBe(0)

  setup.daemon = { status: 200, body: { text: 'compact_now: nothing to bank', isError: true } }
  const failed = await $.tool.call({ tool: 'mcp__windvane__compact_now' } as never)
  expect(failed.deny).toContain('nothing to bank')
})

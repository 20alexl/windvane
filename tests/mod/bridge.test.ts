// windvane: the hook bridge answers windvane's classic hook events
// from the daemon over loopback HTTP, folds several hook types' answers the
// way the engine folds settings hooks, and hands the event to the settings
// hooks (next) whenever it cannot answer for all of them: no port file, a
// failed request, another hook on the event, a type the daemon does not serve.
//
// The test's hooks are the engine's bottom: op events answer { value }; a
// test's on('classic.<Event>') is reached only when the bridge called next.
import type { On } from 'claude-code'
import { expect, mock, test } from 'claude-code/testing'
import { foldClassic, foldPreToolUse, matcherMatches, plan, readOutput, toClassic } from '../../hooks/bridge'

const STORE = 'C:/tmp/windvane-bridge-store'
const PORT = 47123
const PY = 'E:/demo/venv/Scripts/python.exe'
const CLIENT = 'E:/demo/plugins/windvane/windvane/daemon_client.py'

const client = (type: string) => ({ type: 'command', command: PY, args: ['-S', CLIENT, type], timeout: 1000 })
const remind = (type: string) => ({ type: 'command', command: PY, args: ['-m', 'windvane.daemon_client', type], timeout: 2000 })

// windvane's command hooks as a settings table holds them.
const WINDVANE_HOOKS = {
  UserPromptSubmit: [{ matcher: '', hooks: [client('prompt_json')] }],
  PreToolUse: [
    { matcher: 'Edit|Write', hooks: [client('pre_edit_json')] },
    { matcher: 'Read', hooks: [client('pre_read_json')] },
    { matcher: 'Bash|PowerShell', hooks: [client('pre_bash_json')] },
    { matcher: '', hooks: [client('pre_tool_json')] },
  ],
  PostToolUse: [
    { matcher: 'Bash', hooks: [client('bash_json')] },
    { matcher: 'Edit|Write', hooks: [client('post_edit_json')] },
    { matcher: 'ExitPlanMode|TaskUpdate', hooks: [remind('post_milestone_json')] },
  ],
  Stop: [{ matcher: '', hooks: [remind('stop_json')] }],
  SessionEnd: [{ matcher: '', hooks: [remind('session_end_json')] }],
  Notification: [
    { matcher: '', hooks: [remind('notification_json')] },
    { matcher: 'permission_prompt|agent_needs_input', hooks: [{ type: 'command', command: 'powershell.exe', args: ['-File', 'toast.ps1'] }] },
  ],
}

type Fetched = { url: string; hook: string; stdin: Record<string, unknown>; env: Record<string, string>; headers: Record<string, string> }
type Answer = { status: number; text: string } | 'throw'

type Setup = {
  hooks?: Record<string, unknown>
  portFile?: boolean
  answer?: (hook: string) => Answer
  plugins?: Record<string, unknown> // plugin id -> its hooks.json
  self?: Record<string, unknown> // this plugin's own hooks.json `hooks`
}

function engine(on: On, setup: Setup) {
  const fetched: Fetched[] = []
  const logs: string[] = []
  const reached: string[] = []
  mock.clock(on)
  const env: Record<string, string> = { WINDVANE_DIR: STORE, USERPROFILE: 'C:/Users/nobody', WINDVANE_AUTONOMY: '1' }
  on('env.get', ($, e) => ({ value: env[e.name] }))
  on('session.id', () => ({ value: 'sid-bridge' }))
  on('session.cwd', () => ({ value: 'E:/demo/proj' }))
  on('session.root', () => ({ value: 'E:/demo' }))
  on('ui.log', ($, e) => {
    logs.push(e.text)
    return { value: undefined }
  })
  const plugins = setup.plugins ?? {}
  on('settings.read', ($, e) => {
    if (e.source === 'user') return { value: { hooks: setup.hooks ?? WINDVANE_HOOKS } }
    if (e.source === undefined) return { value: { enabledPlugins: Object.fromEntries(Object.keys(plugins).map(id => [id, true])) } }
    return { value: {} }
  })
  const files: Record<string, string> = {}
  if (setup.portFile !== false) files[`${STORE}/daemon_port`] = `${PORT}\n`
  files['C:/Users/nobody/.claude/plugins/installed_plugins.json'] = JSON.stringify({
    version: 2,
    plugins: Object.fromEntries(Object.keys(plugins).map(id => [id, [{ installPath: `C:\\plugins\\${id}` }]])),
  })
  for (const [id, hooks] of Object.entries(plugins)) files[`C:/plugins/${id}/hooks/hooks.json`] = JSON.stringify({ hooks })
  const fwd = (p: string) => p.replace(/\\/g, '/')
  // This plugin's own hooks.json, wherever its folder is.
  const own = (p: string) => setup.self !== undefined && !p.startsWith('C:/plugins/') && p.endsWith('/hooks/hooks.json')
  on('fs.exists', ($, e) => ({ value: fwd(e.path) in files || own(fwd(e.path)) }))
  on('fs.read', ($, e) => {
    const p = fwd(e.path)
    if (own(p)) return { value: JSON.stringify({ modules: ['./register.ts'], hooks: setup.self }) }
    if (!(p in files)) throw new Error(`ENOENT ${p}`)
    return { value: files[p]! }
  })
  on('http.fetch', ($, e) => {
    const body = JSON.parse(e.init?.body ?? '{}')
    fetched.push({ url: e.url, hook: body.hook_event, stdin: JSON.parse(body.stdin), env: body.env, headers: e.init?.headers ?? {} })
    const a = (setup.answer ?? (() => ({ status: 200, text: JSON.stringify({ output: '' }) })))(body.hook_event)
    if (a === 'throw') throw new Error('ECONNREFUSED')
    return { value: { status: a.status, ok: a.status === 200, headers: {}, text: a.text } }
  })
  const bottom = (name: string, result: object = {}) =>
    on(`classic.${name}` as 'classic.Stop', () => {
      reached.push(name)
      return result
    })
  return { fetched, logs, reached, bottom }
}

const ok = (output: string | object) => ({ status: 200, text: JSON.stringify({ output: typeof output === 'string' ? output : JSON.stringify(output) }) })
const ctx = (event: string, text: string) => ({ hookSpecificOutput: { hookEventName: event, additionalContext: text } })

test('a prompt is answered by the daemon: one POST, the context back, no settings hook', async ($, on) => {
  const t = engine(on, { answer: () => ok(ctx('UserPromptSubmit', '<windvane-context>heads-up</windvane-context>')) })
  t.bottom('UserPromptSubmit', { additionalContext: ['from the settings hooks'] })

  const out = await $.classic.UserPromptSubmit({ prompt: 'switch the store to postgres' })
  expect(out.additionalContext).toEqual(['<windvane-context>heads-up</windvane-context>'])
  expect(t.reached).toEqual([])
  expect(t.fetched.length).toBe(1)
  const f = t.fetched[0]!
  expect(f.url).toBe(`http://127.0.0.1:${PORT}/hook`)
  expect(f.hook).toBe('prompt_json')
  expect(f.headers['X-Windvane-Hook']).toBe('1')
  expect(f.stdin.prompt).toBe('switch the store to postgres')
  expect(f.stdin.hook_event_name).toBe('UserPromptSubmit')
  expect(f.env.CLAUDE_PROJECT_DIR).toBe('E:/demo')
  expect(f.env.WINDVANE_AUTONOMY).toBe('1')
  expect(f.env.CLAUDE_CODE_AUTO_COMPACT_WINDOW).toBe('')
})

test('plain stdout is context on UserPromptSubmit only; block and stop shapes map', async ($, on) => {
  let output: string | object = 'plain banner text\n'
  engine(on, { answer: () => ok(output) })
  expect((await $.classic.UserPromptSubmit({ prompt: 'hello there' })).additionalContext).toEqual(['plain banner text'])
  // Stop's plain stdout is shown to the person, never handed to the model.
  expect(await $.classic.Stop({ stop_hook_active: false, last_assistant_message: 'done' })).toEqual({})

  output = { decision: 'block', reason: 'keep going' }
  expect((await $.classic.Stop({ stop_hook_active: false, last_assistant_message: 'done' })).block).toBe('keep going')
  output = { continue: false, stopReason: 'halted' }
  const stopped = await $.classic.Stop({ stop_hook_active: false, last_assistant_message: 'done' })
  expect(stopped.preventContinuation).toBe(true)
  expect(stopped.stopReason).toBe('halted')
})

test('a PostToolUse answer carries its context and a rewritten tool output', async ($, on) => {
  const t = engine(on, {
    answer: () => ok({ hookSpecificOutput: { hookEventName: 'PostToolUse', additionalContext: 'PASS Test tracked', updatedToolOutput: 'trimmed' } }),
  })
  const out = await $.classic.PostToolUse({ tool_name: 'Bash', tool_input: { command: 'pytest -q' }, tool_response: { stdout: '1 passed' }, tool_use_id: 'tu-9' })
  expect(out.additionalContext).toEqual(['PASS Test tracked'])
  expect(out.updatedToolOutput).toBe('trimmed')
  expect(t.fetched.map(f => f.hook)).toEqual(['bash_json'])
})

test('a 500, an error body, a refused connection and a missing port file each take next', async ($, on) => {
  let answer: Answer = { status: 500, text: 'boom' }
  const t = engine(on, { answer: () => answer })
  t.bottom('UserPromptSubmit', { additionalContext: ['settings'] })

  expect((await $.classic.UserPromptSubmit({ prompt: 'first prompt here' })).additionalContext).toEqual(['settings'])
  answer = { status: 200, text: JSON.stringify({ error: "unsupported hook_event 'prompt_json'" }) }
  expect((await $.classic.UserPromptSubmit({ prompt: 'second prompt here' })).additionalContext).toEqual(['settings'])
  answer = 'throw'
  expect((await $.classic.UserPromptSubmit({ prompt: 'third prompt here' })).additionalContext).toEqual(['settings'])
  expect(t.reached.length).toBe(3)
  expect(t.logs.join('\n')).toContain('daemon answered 500')
  expect(t.logs.join('\n')).toContain('daemon error')
  expect(t.logs.join('\n')).toContain('fetch failed')
})

test('no port file: the settings hooks run and nothing is fetched', async ($, on) => {
  const t = engine(on, { portFile: false })
  t.bottom('UserPromptSubmit', { additionalContext: ['settings'] })
  expect((await $.classic.UserPromptSubmit({ prompt: 'a prompt with no daemon' })).additionalContext).toEqual(['settings'])
  expect(t.fetched.length).toBe(0)
  expect(t.logs.join('\n')).toContain('no daemon port file')
})

test('PreToolUse on Bash runs pre_bash_json then pre_tool_json, and a deny from either wins', async ($, on) => {
  let denyFrom = 'pre_tool_json'
  const t = engine(on, {
    answer: hook =>
      hook === denyFrom
        ? ok({ hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny', permissionDecisionReason: `halted by ${hook}` } })
        : ok(ctx('PreToolUse', '<windvane-rule>never rm -rf</windvane-rule>')),
  })
  on('tool.call', () => ({ result: { content: [{ type: 'text', text: 'ran' }] } }))

  const first = await $.tool.call({ tool: 'Bash', command: 'ls', tool_use_id: 'tu-1' })
  expect(t.fetched.map(f => f.hook)).toEqual(['pre_bash_json', 'pre_tool_json'])
  expect(JSON.stringify(first)).toContain('halted by pre_tool_json')
  const f = t.fetched[0]!
  expect(f.stdin.hook_event_name).toBe('PreToolUse')
  expect(f.stdin.tool_name).toBe('Bash')
  expect(f.stdin.tool_input).toEqual({ command: 'ls' })
  expect(f.stdin.tool_use_id).toBe('tu-1')
  expect(f.stdin.session_id).toBe('sid-bridge')
  expect(f.stdin.cwd).toBe('E:/demo/proj')
  expect(f.stdin.agent_id).toBeUndefined()

  denyFrom = 'pre_bash_json'
  const second = await $.tool.call({ tool: 'Bash', command: 'rm -rf build', tool_use_id: 'tu-2' })
  expect(JSON.stringify(second)).toContain('halted by pre_bash_json')

  // A subagent's call reaches windvane as one: the loop was noted at tool.call.
  denyFrom = 'none'
  // (agentId rides the engine's own calls; the call's type omits it.)
  const subCall = { tool: 'Read' as const, file_path: 'a.py', tool_use_id: 'tu-3', agentId: 'agent-7' }
  const sub = await $.tool.call(subCall)
  expect(JSON.stringify(sub)).toContain('ran')
  const last = t.fetched.filter(x => x.stdin.tool_use_id === 'tu-3')
  expect(last.map(x => x.hook)).toEqual(['pre_read_json', 'pre_tool_json'])
  expect(last[0]!.stdin.agent_id).toBe('agent-7')
})

test('another hook on the event hands it to the settings hooks', async ($, on) => {
  const t = engine(on, { plugins: { 'loop@market': { Stop: [{ hooks: [{ type: 'command', command: 'bash stop-hook.sh' }] }] } } })
  t.bottom('Stop', { block: 'loop continues' })
  t.bottom('Notification', {})

  expect((await $.classic.Stop({ stop_hook_active: false, last_assistant_message: 'x' })).block).toBe('loop continues')
  expect(t.reached).toEqual(['Stop'])
  expect(t.logs.join('\n')).toContain('plugin loop@market')

  // The toast hook's matcher: an idle prompt is windvane's alone, a permission
  // prompt is the toast's too.
  await $.classic.Notification({ message: 'idle', notification_type: 'idle_prompt' })
  expect(t.reached).toEqual(['Stop'])
  expect(t.fetched.map(f => f.hook)).toEqual(['notification_json'])
  await $.classic.Notification({ message: 'needs you', notification_type: 'permission_prompt' })
  expect(t.reached).toEqual(['Stop', 'Notification'])
})

test("the plugin's own command hooks are read from its folder", async ($, on) => {
  const own = {
    UserPromptSubmit: [{ hooks: [{ type: 'command', command: 'python "${CLAUDE_PLUGIN_ROOT}/windvane/daemon_client.py" prompt_json' }] }],
  }
  const t = engine(on, { hooks: {}, self: own, answer: () => ok(ctx('UserPromptSubmit', 'from the daemon')) })
  t.bottom('UserPromptSubmit', { additionalContext: ['from the command hook'] })

  const out = await $.classic.UserPromptSubmit({ prompt: 'a prompt for the plugin hooks' })
  expect(out.additionalContext).toEqual(['from the daemon'])
  expect(t.fetched.map(f => f.hook)).toEqual(['prompt_json'])
  expect(t.reached).toEqual([])
})

test('SessionEnd stays with its settings hook and logs the counts', async ($, on) => {
  const t = engine(on, { answer: () => ok('') })
  t.bottom('SessionEnd', {})
  await $.classic.UserPromptSubmit({ prompt: 'one bridged prompt' })
  await $.classic.SessionEnd({ reason: 'other' })
  expect(t.reached).toEqual(['SessionEnd'])
  expect(t.fetched.map(f => f.hook)).toEqual(['prompt_json'])
  expect(t.logs.join('\n')).toContain('not daemon-served: session_end_json')
  expect(t.logs.join('\n')).toMatch(/\d+ bridged, \d+ to the settings hooks/)
})

test('the helpers: matchers, the plan, output shapes and the folds', () => {
  expect(matcherMatches('', 'Bash')).toBe(true)
  expect(matcherMatches('Bash|PowerShell', 'PowerShell')).toBe(true)
  expect(matcherMatches('Edit|Write', 'Read')).toBe(false)
  expect(matcherMatches('mcp__gh__.*', 'mcp__gh__issue')).toBe(true)
  expect(matcherMatches('Read', undefined)).toBe(true)

  const p = plan('PostToolUse', 'Read', [
    { origin: 'user settings', hooks: { PostToolUse: [{ matcher: 'Edit|Write|Read', hooks: [{ type: 'command', command: 'python', args: ['-c', 'write .last_file'] }] }] } },
  ])
  expect(p).toEqual({ types: [], foreign: ['user settings'] })

  expect(readOutput('')).toEqual({})
  expect(readOutput('{"a": 1}')).toEqual({ json: { a: 1 } })
  expect(readOutput('{not json')).toEqual({ text: '{not json' })
  // A field the event does not read is dropped.
  expect(toClassic('PreCompact', { json: ctx('PreCompact', 'x') })).toEqual({})

  expect(foldClassic([{ additionalContext: ['a'] }, { additionalContext: ['b'], block: 'no' }, { block: 'later' }])).toEqual({
    additionalContext: ['a', 'b'],
    block: 'no',
  })
  expect(foldPreToolUse([{ additionalContext: ['rule'] }, { additionalContext: ['nudge'] }])).toEqual({ additionalContext: ['rule', 'nudge'] })
  expect(foldPreToolUse([{ allow: true }, { ask: 'sure?' }, { deny: 'no' }])).toEqual({ deny: 'no' })
})

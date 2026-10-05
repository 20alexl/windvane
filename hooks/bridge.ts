// windvane: the hook bridge.
//
// Each of windvane's command hooks spawns a process: Claude Code runs
// `python .../windvane/daemon_client.py <type>`, which makes one round trip
// to the daemon or runs the handler in-process. Here one `classic.*` hook
// answers instead: for each classic event it finds the windvane hook types
// the command hooks would have run (their own matchers, read from the
// settings and the plugins' hooks.json), POSTs the event's stdin JSON to the
// daemon's HTTP endpoint (`POST /hook`) once per type, and turns each
// handler's stdout into the event's result, folded the way the engine folds
// command hooks (contexts concatenate, a deny or a block wins). No process
// is started.
//
// The chain is [managed settings hooks, hooks modules, the other command
// hooks as core]: a module that answers without `next` stops EVERY command
// hook beneath it, windvane's and anyone else's, plugins' command hooks
// included. So the bridge answers alone only when the census says every
// command hook that would fire for this call is windvane's and the daemon
// serves each of their types; otherwise it calls `next(e)` and the command
// hooks run exactly as before (windvane's among them). With the bridge
// answering, windvane's own command hooks never run, so no double-fire
// marker is needed.
//
// Any failure before a handler ran (no port file, a refused connection, a
// non-200, an `error` body, a body that is not the daemon's) also takes
// `next(e)`. A request that timed out may have run, so it does not: the
// event gets an empty answer, as a settings hook that timed out gives, and
// the next 30 s go to the settings hooks while the daemon recovers.
//
// The pure helpers are exported for the tests. register.ts hooks classic.*
// and asks bridgeDecision whether to answer or to pass; this module never
// holds `$`, it takes the Host register.ts builds.
import type { ClassicResult, PreToolUseResult, Timer } from 'claude-code'
import { PLUGIN, type Host } from './engine'
import { normalizePath } from './ring'

// The hook types the daemon runs in-process.
// session_end_json is left to its settings hook (it runs while Claude Code
// is exiting, where a mod's request may never be sent).
export const SERVED = new Set([
  'pre_edit_json',
  'post_edit_json',
  'bash_json',
  'prompt_json',
  'pre_read_json',
  'tool_failure_json',
  'post_batch_json',
  'pre_bash_json',
  'pre_tool_json',
  'post_milestone_json',
  'session_start_json',
  'stop_json',
  'pre_compact_json',
  'post_compact_json',
  'stop_failure_json',
  'notification_json',
])

// The handlers that may take longer than a tool-path hook (the banner, the
// drafted checkpoint, the turn's close).
const SLOW = new Set(['session_start_json', 'pre_compact_json', 'stop_json'])
const TIMEOUT_MS = 2_000
const SLOW_TIMEOUT_MS = 5_000
const COOL_DOWN_MS = 30_000
const CENSUS_TTL_MS = 60_000

// ClassicResultFields, mirrored: the fields of a result each event reads.
const FIELDS: Record<string, readonly string[]> = {
  UserPromptSubmit: ['additionalContext', 'sessionTitle', 'suppressOriginalPrompt'],
  SessionStart: ['additionalContext', 'initialUserMessage', 'sessionTitle', 'watchPaths', 'reloadSkills'],
  PostToolUse: ['additionalContext', 'updatedToolOutput', 'updatedMCPToolOutput'],
  PostToolUseFailure: ['additionalContext'],
  PostToolBatch: ['additionalContext'],
  Stop: ['additionalContext'],
  SubagentStop: ['additionalContext'],
}
const COMMON = ['block', 'preventContinuation', 'stopReason']

// The events whose plain (non-JSON) stdout Claude Code hands to the model as
// context; every other event's plain stdout is shown to the person only.
const PLAIN_TO_CONTEXT = new Set(['UserPromptSubmit', 'SessionStart'])

// The env a command hook process inherits that windvane's handlers read per
// session (the daemon's per-request session env); CLAUDE_PROJECT_DIR is the
// session root.
export type SessionEnv = Record<string, string>

export type Json = Record<string, unknown>

const isRecord = (v: unknown): v is Json => typeof v === 'object' && v !== null && !Array.isArray(v)

// ---------------------------------------------------------------- matching

// Claude Code's matcher: empty or `*` matches everything; a plain
// `A|B` alternation matches those names exactly; anything else is a regex.
export function matcherMatches(matcher: unknown, subject: string | undefined): boolean {
  if (matcher === undefined || matcher === null || matcher === '' || matcher === '*') return true
  if (subject === undefined) return true // the event takes no matcher
  const m = String(matcher)
  if (/^[\w|]+$/.test(m)) return m.split('|').includes(subject)
  try {
    return new RegExp(`^(?:${m})$`).test(subject)
  } catch {
    return false
  }
}

// What an event's matcher is tested against.
export function subjectOf(event: string, e: Json): string | undefined {
  const s = (k: string) => (typeof e[k] === 'string' ? (e[k] as string) : undefined)
  switch (event) {
    case 'PreToolUse':
      return s('tool') ?? s('tool_name')
    case 'PostToolUse':
    case 'PostToolUseFailure':
    case 'PermissionRequest':
    case 'PermissionDenied':
      return s('tool_name')
    case 'Notification':
      return s('notification_type')
    case 'SessionStart':
      return s('source')
    case 'PreCompact':
    case 'PostCompact':
      return s('trigger')
    case 'SessionEnd':
      return s('reason')
    case 'StopFailure':
      return s('error_type')
    default:
      return undefined
  }
}

// ------------------------------------------------------------------ census

// One source of command hooks: a settings file's `hooks`, or a plugin's.
export type HookSource = { origin: string; hooks: Json }

// The windvane hook type a command hook runs, or undefined when it is not
// windvane's (`.../windvane/daemon_client.py <type>`, or
// `-m windvane.daemon_client <type>`).
export function windvaneType(hook: unknown): string | undefined {
  if (!isRecord(hook) || (hook.type !== undefined && hook.type !== 'command')) return undefined
  const args = Array.isArray(hook.args) ? hook.args.map(String) : []
  const line = [String(hook.command ?? ''), ...args].join(' ')
  if (!/windvane[\\/.]daemon_client(\.py)?\b/.test(line)) return undefined
  const words = line.trim().split(/\s+/)
  const last = (words[words.length - 1] ?? '').replace(/^["']|["']$/g, '')
  return /^[a-z_]+_json$/.test(last) ? last : undefined
}

// What the command hooks would run for one call: windvane's hook types (in
// settings order, once each) and whether any other hook would fire too.
export function plan(event: string, subject: string | undefined, sources: readonly HookSource[]): { types: string[]; foreign: string[] } {
  const types: string[] = []
  const foreign: string[] = []
  for (const src of sources) {
    const groups = src.hooks[event]
    if (!Array.isArray(groups)) continue
    for (const g of groups) {
      if (!isRecord(g) || !matcherMatches(g.matcher, subject)) continue
      for (const h of Array.isArray(g.hooks) ? g.hooks : []) {
        const t = windvaneType(h)
        if (t === undefined) foreign.push(src.origin)
        else if (!types.includes(t)) types.push(t)
      }
    }
  }
  return { types, foreign }
}

// ---------------------------------------------------------------- the call

// The stdin a settings PreToolUse hook would have read, rebuilt from the
// envelope (`tool`, `tool_use_id`, the tool's own arguments at the top).
export function preToolUseStdin(e: Json, base: { session_id: string; cwd: string; transcript_path: string; permission_mode?: string; agent_id?: string }): Json {
  const { tool, tool_use_id, consent: _consent, agentId: _agentId, ...args } = e
  const out: Json = {
    session_id: base.session_id,
    transcript_path: base.transcript_path,
    cwd: base.cwd,
    hook_event_name: 'PreToolUse',
    tool_name: tool,
    tool_input: args,
    tool_use_id: tool_use_id ?? '',
  }
  if (base.permission_mode) out.permission_mode = base.permission_mode
  if (base.agent_id) out.agent_id = base.agent_id
  return out
}

// A handler's stdout, read as Claude Code reads a command hook's: a JSON
// object, else plain text.
export type HookOutput = { json?: Json; text?: string }

export function readOutput(stdout: string): HookOutput {
  const text = stdout.trim()
  if (!text) return {}
  if (text.startsWith('{')) {
    try {
      const v: unknown = JSON.parse(text)
      if (isRecord(v)) return { json: v }
    } catch {
      // plain text after all
    }
  }
  return { text }
}

// One handler's output as this event's result.
export function toClassic(event: string, out: HookOutput): ClassicResult {
  const r: ClassicResult = {}
  const ctx: string[] = []
  const j = out.json
  if (j) {
    if (j.decision === 'block') r.block = String(j.reason ?? '') || 'Blocked by hook'
    if (j.continue === false) {
      r.preventContinuation = true
      if (typeof j.stopReason === 'string') r.stopReason = j.stopReason
    }
    const h = isRecord(j.hookSpecificOutput) ? j.hookSpecificOutput : undefined
    if (h) {
      if (typeof h.additionalContext === 'string' && h.additionalContext) ctx.push(h.additionalContext)
      if ('updatedToolOutput' in h) r.updatedToolOutput = h.updatedToolOutput
      if ('updatedMCPToolOutput' in h) r.updatedMCPToolOutput = h.updatedMCPToolOutput
      if (typeof h.sessionTitle === 'string') r.sessionTitle = h.sessionTitle
      if (typeof h.initialUserMessage === 'string') r.initialUserMessage = h.initialUserMessage
    }
  } else if (out.text && PLAIN_TO_CONTEXT.has(event)) {
    ctx.push(out.text)
  }
  if (ctx.length > 0) r.additionalContext = ctx
  const allowed = new Set([...COMMON, ...(FIELDS[event] ?? [])])
  const kept: Json = {}
  for (const [k, v] of Object.entries(r)) if (allowed.has(k)) kept[k] = v
  return kept as ClassicResult
}

// One handler's output as a PreToolUse result.
export function toPreToolUse(out: HookOutput): PreToolUseResult {
  const j = out.json
  if (!j) return {}
  const h = isRecord(j.hookSpecificOutput) ? j.hookSpecificOutput : {}
  const reason = String(h.permissionDecisionReason ?? j.reason ?? '')
  const ctx = typeof h.additionalContext === 'string' && h.additionalContext ? [h.additionalContext] : undefined
  const extra: { additionalContext?: string[]; updatedInput?: Record<string, unknown> } = {}
  if (ctx) extra.additionalContext = ctx
  if (isRecord(h.updatedInput)) extra.updatedInput = h.updatedInput
  const decision = h.permissionDecision ?? (j.decision === 'block' ? 'deny' : j.decision === 'approve' ? 'allow' : undefined)
  if (decision === 'deny') return { deny: reason || 'Denied by hook', ...extra }
  if (decision === 'ask') return { ask: reason || 'Hook asks', ...extra }
  if (decision === 'allow') return { allow: true, ...extra }
  return extra
}

// Several handlers' results folded as the engine folds settings hooks:
// contexts concatenate in order, the first block (and stop reason) stands,
// any preventContinuation stops, the last tool-output rewrite wins.
export function foldClassic(results: readonly ClassicResult[]): ClassicResult {
  const out: ClassicResult = {}
  const ctx: string[] = []
  for (const r of results) {
    if (r.additionalContext) ctx.push(...r.additionalContext)
    if (r.block !== undefined && out.block === undefined) out.block = r.block
    if (r.preventContinuation) out.preventContinuation = true
    if (r.stopReason !== undefined && out.stopReason === undefined) out.stopReason = r.stopReason
    if ('updatedToolOutput' in r) out.updatedToolOutput = r.updatedToolOutput
    if ('updatedMCPToolOutput' in r) out.updatedMCPToolOutput = r.updatedMCPToolOutput
    if (r.sessionTitle !== undefined) out.sessionTitle = r.sessionTitle
    if (r.initialUserMessage !== undefined) out.initialUserMessage = r.initialUserMessage
  }
  if (ctx.length > 0) out.additionalContext = ctx
  return out
}

// PreToolUse: a deny beats an ask beats an allow.
export function foldPreToolUse(results: readonly PreToolUseResult[]): PreToolUseResult {
  const ctx: string[] = []
  let updatedInput: Record<string, unknown> | undefined
  let deny: string | undefined
  let ask: string | undefined
  let allow = false
  for (const r of results) {
    if (r.additionalContext) ctx.push(...r.additionalContext)
    if (r.updatedInput) updatedInput = r.updatedInput
    if (r.deny !== undefined && deny === undefined) deny = r.deny
    if (r.ask !== undefined && ask === undefined) ask = r.ask
    if (r.allow) allow = true
  }
  const extra: { additionalContext?: string[]; updatedInput?: Record<string, unknown> } = {}
  if (ctx.length > 0) extra.additionalContext = ctx
  if (updatedInput) extra.updatedInput = updatedInput
  if (deny !== undefined) return { deny, ...extra }
  if (ask !== undefined) return { ask, ...extra }
  if (allow) return { allow: true, ...extra }
  return extra
}

// The daemon's HTTP answer: the handler's stdout, or why there is none (in
// which case no handler ran and the settings hooks may run instead).
export function readAnswer(status: number, body: string): { output: string } | { reason: string } {
  if (status !== 200) return { reason: `daemon answered ${status}` }
  let v: unknown
  try {
    v = JSON.parse(body)
  } catch {
    return { reason: 'daemon body is not JSON' }
  }
  if (!isRecord(v)) return { reason: 'daemon body is not an object' }
  if (typeof v.error === 'string') return { reason: `daemon error: ${v.error.slice(0, 120)}` }
  if (typeof v.output !== 'string') return { reason: 'daemon body has no output' }
  return { output: v.output }
}

// --------------------------------------------- the main loop's tool calls

// classic.PreToolUse's envelope names no loop, but a subagent's call must
// reach windvane as one (no injection, never halted). The plugin's matcher-less
// tool.call hook (pane.tsx), which runs above every classic.PreToolUse,
// notes the loop of each call by its tool_use_id.
const callLoops = new Map<string, string>()

export function noteCallLoop(toolUseId: unknown, agentId: string | undefined): void {
  if (typeof toolUseId !== 'string' || !toolUseId || agentId === undefined) return
  callLoops.set(toolUseId, agentId)
  if (callLoops.size > 512) {
    const first = callLoops.keys().next().value
    if (first !== undefined) callLoops.delete(first)
  }
}

export function forgetCall(toolUseId: unknown): void {
  if (typeof toolUseId === 'string') callLoops.delete(toolUseId)
}

export function loopOf(toolUseId: unknown): string | undefined {
  return typeof toolUseId === 'string' ? callLoops.get(toolUseId) : undefined
}

// ------------------------------------------------------- per-load state

type Census = { at: number; sources: HookSource[]; disabled: boolean; env: SessionEnv; store: string }

export type BridgeCounts = { bridged: number; fellBack: number; partial: number; timedOut: number }

const counts: BridgeCounts = { bridged: 0, fellBack: 0, partial: 0, timedOut: 0 }
let census: Census | undefined
let censusLoading: Promise<Census> | undefined
let port: number | undefined
let downUntil = 0
// The main loop's last transcript path and permission mode, from the classic
// events that carry them (every one but PreToolUse).
let lastTranscript = ''
let lastMode: string | undefined

export function bridgeCounts(): BridgeCounts {
  return { ...counts }
}

// --------------------------------------------------------- host helpers

async function readText(host: Host, path: string): Promise<string | undefined> {
  if (!(await host.exists(path))) return undefined
  return host.read(path)
}

async function readJson(host: Host, path: string): Promise<unknown> {
  const text = await readText(host, path)
  return text === undefined ? undefined : (JSON.parse(text) as unknown)
}

// The command hooks one installed plugin declares: hooks/hooks.json and the
// `hooks` of its manifest (a path, several, or the object inline).
async function pluginHooks(host: Host, root: string): Promise<Json[]> {
  const found: Json[] = []
  const take = (v: unknown) => {
    if (isRecord(v) && isRecord(v.hooks)) found.push(v.hooks)
  }
  take(await readJson(host, `${root}/hooks/hooks.json`))
  const manifest = await readJson(host, `${root}/.claude-plugin/plugin.json`)
  if (isRecord(manifest)) {
    const h = manifest.hooks
    const paths = typeof h === 'string' ? [h] : Array.isArray(h) ? h.filter(x => typeof x === 'string') : []
    for (const p of paths) take(await readJson(host, `${root}/${String(p).replace(/^\.\//, '')}`))
    if (isRecord(h)) found.push(isRecord(h.hooks) ? h.hooks : h)
  }
  return found
}

// Every command hook the session would run beneath the modules: the four
// settings sources (policy hooks run above the modules either way), the
// enabled plugins' hooks, and this plugin's own hooks. Another plugin loaded
// from a session folder (--plugin-dir) is not listed anywhere a mod can
// read, so its hooks are missing from the census; this plugin's own are read
// from its folder however it was loaded.
async function loadCensus(host: Host): Promise<Census> {
  const sources: HookSource[] = []
  for (const source of ['user', 'project', 'local', 'flag'] as const) {
    const s = await host.settings(source)
    if (isRecord(s.hooks)) sources.push({ origin: `${source} settings`, hooks: s.hooks })
  }
  const merged = await host.settings()
  const disabled = merged.disableAllHooks === true || merged.allowManagedHooksOnly === true
  const configDir = await host.configDir()
  const enabled = isRecord(merged.enabledPlugins)
    ? Object.entries(merged.enabledPlugins).filter(([, flag]) => flag === true).map(([id]) => id)
    : []
  const self = normalizePath(host.pluginRoot)
  let selfListed = false
  if (enabled.length > 0) {
    const installed = await readJson(host, `${configDir}/plugins/installed_plugins.json`)
    const table = isRecord(installed) && isRecord(installed.plugins) ? installed.plugins : {}
    for (const id of enabled) {
      const entries = table[id]
      const first = Array.isArray(entries) ? entries[0] : undefined
      const root = isRecord(first) && typeof first.installPath === 'string' ? first.installPath.replace(/\\/g, '/') : ''
      if (!root) continue // enabled, not installed: nothing loads
      if (normalizePath(root) === self) selfListed = true
      for (const hooks of await pluginHooks(host, root)) sources.push({ origin: `plugin ${id}`, hooks })
    }
  }
  if (!selfListed && self) {
    for (const hooks of await pluginHooks(host, self)) sources.push({ origin: `plugin ${PLUGIN}`, hooks })
  }
  return { at: Date.now(), sources, disabled, env: await host.sessionEnv(), store: await host.store() }
}

async function currentCensus(host: Host): Promise<Census> {
  if (census && Date.now() - census.at < CENSUS_TTL_MS) return census
  if (!censusLoading) {
    censusLoading = loadCensus(host).finally(() => {
      censusLoading = undefined
    })
  }
  census = await censusLoading
  return census
}

async function daemonPort(host: Host, store: string): Promise<number | undefined> {
  if (port !== undefined) return port
  const text = await readText(host, `${store}/daemon_port`)
  const n = text === undefined ? NaN : parseInt(text.trim(), 10)
  port = Number.isInteger(n) && n > 0 ? n : undefined
  return port
}

type Posted = { output: string } | { reason: string; ran: false } | { timedOut: true }

async function post(host: Host, at: number, type: string, stdin: Json, env: SessionEnv): Promise<Posted> {
  let timer: Timer | undefined
  const timeout = new Promise<'timeout'>(resolve => {
    timer = host.after(SLOW.has(type) ? SLOW_TIMEOUT_MS : TIMEOUT_MS, () => resolve('timeout'))
  })
  try {
    const res = await Promise.race([
      host.post(at, 'hook', JSON.stringify({ hook_event: type, stdin: JSON.stringify(stdin), env })),
      timeout,
    ])
    if (res === 'timeout') return { timedOut: true }
    const got = readAnswer(res.status, res.text)
    return 'output' in got ? got : { reason: got.reason, ran: false }
  } catch (err) {
    port = undefined // re-read: a restarted daemon writes a new port
    return { reason: `fetch failed: ${String(err).slice(0, 120)}`, ran: false }
  } finally {
    timer?.cancel()
  }
}

// ------------------------------------------------------------ the decision

export type BridgeDecision = { pass: true } | { answer: ClassicResult | PreToolUseResult }

const PASS: BridgeDecision = { pass: true }

// One classic event: the answer the bridge gives for it, or pass, in which
// case register.ts calls next(e) and the command hooks run as before.
export async function bridgeDecision(host: Host, input: Json): Promise<BridgeDecision> {
  // The event is named by its input: every classic hook's stdin carries
  // hook_event_name, except PreToolUse, whose e is the tool call's envelope.
  const named = typeof input.hook_event_name === 'string' ? input.hook_event_name : undefined
  const pre = named === undefined && typeof input.tool === 'string'
  if (named === undefined && !pre) return PASS
  const event = pre ? 'PreToolUse' : String(named)

  // The settings hooks run this one: the reason goes to the debug log.
  const noteFallBack = (reason: string) => {
    counts.fellBack += 1
    host.debug(`${PLUGIN}: bridge: ${event} -> settings hooks (${reason})`)
  }

  // The main loop's transcript and mode, for the PreToolUse envelope.
  if (!pre && input.agent_id === undefined) {
    if (typeof input.transcript_path === 'string' && input.transcript_path) lastTranscript = input.transcript_path
    if (typeof input.permission_mode === 'string' && input.permission_mode) lastMode = input.permission_mode
  }
  if (event === 'SessionEnd') {
    const c = counts
    host.debug(`${PLUGIN}: bridge: ${c.bridged} bridged, ${c.fellBack} to the settings hooks, ${c.partial} partial, ${c.timedOut} timed out`)
  }

  let found: Census
  try {
    found = await currentCensus(host)
  } catch (err) {
    noteFallBack(`hook census failed: ${String(err).slice(0, 120)}`)
    return PASS
  }
  const { types, foreign } = plan(event, subjectOf(event, input), found.sources)
  if (types.length === 0) return PASS // windvane has no hook here
  if (found.disabled) return PASS // hooks are off: nothing of windvane's would run
  if (foreign.length > 0) {
    noteFallBack(`other hooks fire here: ${[...new Set(foreign)].join(', ')}`)
    return PASS
  }
  const unserved = types.filter(t => !SERVED.has(t))
  if (unserved.length > 0) {
    noteFallBack(`not daemon-served: ${unserved.join(', ')}`)
    return PASS
  }
  if (Date.now() < downUntil) {
    noteFallBack('cooling down after a timeout')
    return PASS
  }

  let at: number | undefined
  try {
    at = await daemonPort(host, found.store)
  } catch (err) {
    noteFallBack(`port file unreadable: ${String(err).slice(0, 80)}`)
    return PASS
  }
  if (at === undefined) {
    noteFallBack('no daemon port file')
    return PASS
  }

  let stdin: Json
  if (pre) {
    const loop = loopOf(input.tool_use_id) ?? (typeof input.agentId === 'string' ? input.agentId : undefined)
    stdin = preToolUseStdin(input, {
      session_id: await host.sessionId(),
      cwd: await host.cwd(),
      transcript_path: lastTranscript,
      permission_mode: lastMode,
      agent_id: loop,
    })
  } else {
    stdin = { ...input, hook_event_name: input.hook_event_name ?? event }
  }

  const outputs: HookOutput[] = []
  let timedOut = false
  for (const type of types) {
    const got = await post(host, at, type, stdin, found.env)
    if ('timedOut' in got) {
      // It may have run: never run it again through the settings hooks.
      timedOut = true
      counts.timedOut += 1
      downUntil = Date.now() + COOL_DOWN_MS
      host.debug(`${PLUGIN}: bridge: ${event} ${type} timed out; settings hooks for ${COOL_DOWN_MS / 1000}s`)
      break
    }
    if ('reason' in got) {
      if (outputs.length === 0) {
        noteFallBack(`${type}: ${got.reason}`)
        return PASS
      }
      // An earlier type already ran here: running the settings hooks now
      // would run it twice. Keep what ran; this type is lost this once.
      counts.partial += 1
      host.debug(`${PLUGIN}: bridge: ${event} ${type} lost: ${got.reason}`)
      continue
    }
    outputs.push(readOutput(got.output))
  }

  if (!timedOut || outputs.length > 0) counts.bridged += 1
  if (pre) return { answer: foldPreToolUse(outputs.map(toPreToolUse)) }
  return { answer: foldClassic(outputs.map(o => toClassic(event, o))) }
}

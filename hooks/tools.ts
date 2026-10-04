// windvane: the tools the model calls, served by the mod.
//
// register.ts declares each tool in TOOL_SPECS with $.tool.register at
// session.start; the engine lists it to the model as mcp__windvane__<name>.
// A call is served here, from a tool.call hook matched on the tool's full
// name. Its arguments go to the engine's tool handlers as one JSON request,
// `{ tool, arguments, env }`:
//
// 1. first to the daemon, `POST http://127.0.0.1:<port>/tool` with the
//    header `X-Windvane-Hook: 1`, the port read from `<store>/daemon_port`
//    (warm imports, one handler instance across calls);
// 2. when the daemon is down (no port file, a refused connection, a non-200,
//    a body that is not an answer), to `python -m windvane.tools` with the
//    same request on stdin.
//
// Either answers `{ text, isError, ms }`. The model reads `text`; an error
// is returned as a deny, which the model receives as an error result with
// the reason. A daemon request that timed out may still have run, so it is
// not retried through the subprocess: the model is told to check first.
//
// compact_now: the engine banks the drafted checkpoint and answers. The
// compaction itself cannot run here: $.session.compact() rejects while a
// turn runs, and the model's turn goes on after a tool result. register.ts
// watches the call and compacts at the turn boundary, from a timer, the way
// it compacts inside the checkpoint band.
import type { EngineInterface, On, Timer, ToolSpec } from 'claude-code'

import { PLUGIN, clip, engineEnv, lastJsonLine, pythonHint, pythonOf, type Settings } from './engine'
import { storePath } from './ring'

export const TOOL_TIMEOUT_MS = 60_000

// What compact_now's answer adds for the model.
export const COMPACT_NOTE = `${PLUGIN}: the conversation compacts as soon as this turn ends; end the turn now.`

const PROJECT = {
  type: 'string',
  description: 'The project directory; leave it out and windvane uses the project this session is working in.',
} as const
const LIST = { type: 'array', items: { type: 'string' } } as const

const CHECKPOINT_FIELDS = {
  operation: {
    type: 'string',
    enum: ['save', 'restore', 'list'],
    description:
      'save banks the task state (with no other argument it accepts the record the recorder drafted from the session; a field given amends that field); restore reads one back; list shows the ring newest first.',
  },
  task_description: { type: 'string', description: 'save: what the task is, in one sentence.' },
  current_step: { type: 'string', description: 'save: the step in progress.' },
  completed_steps: { ...LIST, description: 'save: the steps done.' },
  pending_steps: { ...LIST, description: 'save: the steps left, next first.' },
  files_involved: { ...LIST, description: 'save: the files being worked on.' },
  handoff_summary: { type: 'string', description: 'save: the note the next session reads first.' },
  handoff_warnings: { ...LIST, description: 'save: what the next session must not do.' },
  handoff_context_needed: { ...LIST, description: 'save: what the next session should read first.' },
  index: { type: 'integer', description: 'restore: which record, 0 = newest (see list).' },
  project_path: PROJECT,
} as const

const COMPACT_FIELDS = {
  project_path: PROJECT,
} as const

const MEMORY_FIELDS = {
  operation: {
    type: 'string',
    enum: [
      'remember',
      'recall',
      'search',
      'forget',
      'add_rule',
      'list_rules',
      'modify',
      'delete',
      'promote',
      'archive',
      'restore',
      'list_mistakes',
      'acknowledge_mistake',
      'set_detector',
    ],
    description:
      'remember stores a discovery; recall lists the project memory; search finds entries by query; forget clears the project memory; add_rule stores a permanent rule (with reason); list_rules lists them; modify, delete, promote (to a rule), archive, restore and acknowledge_mistake act on one entry by id; list_mistakes lists the tracked mistakes; set_detector attaches a detector to a rule.',
  },
  content: { type: 'string', description: 'remember / add_rule / modify: the text.' },
  reason: { type: 'string', description: 'add_rule / promote: why the rule exists.' },
  query: { type: 'string', description: 'search: what to look for.' },
  memory_id: { type: 'string', description: 'modify / delete / promote / restore / acknowledge_mistake / set_detector: the id shown in brackets.' },
  limit: { type: 'integer', description: 'recall / search / list_mistakes: how many.' },
  detector: {
    type: 'object',
    description:
      'add_rule / set_detector: when the rule applies, so its matches are recorded. Keys: tools (tool names, empty = any), command (regex against a shell command), paths (globs against an edited path), input (regex against the tool input JSON), note. {} clears.',
  },
  project_path: PROJECT,
} as const

const LOG_FIELDS = {
  operation: {
    type: 'string',
    enum: ['mistake', 'decision'],
    description: 'mistake records what went wrong and how to avoid it; decision records a choice with its reason and the alternatives.',
  },
  description: { type: 'string', description: 'mistake: what went wrong.' },
  how_to_avoid: { type: 'string', description: 'mistake: how to keep it from happening again.' },
  decision: { type: 'string', description: 'decision: what was decided.' },
  reason: { type: 'string', description: 'decision: why.' },
  alternatives: { ...LIST, description: 'decision: the options not taken.' },
  project_path: PROJECT,
} as const

const MINE_FIELDS = {
  operation: {
    type: 'string',
    enum: ['search', 'decisions', 'errors', 'struggles', 'replay', 'timeline', 'run_report', 'run_status', 'status'],
    description:
      'search finds past conversation by query (kind narrows the hits); decisions finds when and why something was decided; errors lists recurring errors; struggles lists the areas of repeated difficulty; replay finds the discussions of a file; timeline is the project history; run_report writes and returns this session\'s run report; run_status is the /goal run as recorded; status is the mining index coverage.',
  },
  query: { type: 'string', description: 'search / decisions: what to look for.' },
  file_path: { type: 'string', description: 'replay: the file.' },
  kind: { type: 'string', description: 'search: one kind of hit (decision, next-step, error, narration).' },
  limit: { type: 'integer', description: 'How many results (default 10).' },
  project_path: PROJECT,
} as const

const DEPS_FIELDS = {
  operation: {
    type: 'string',
    enum: ['map', 'impact'],
    description: 'map says where a symbol is defined (file, signature) and what imports it; impact lists what depends on a file before it is changed.',
  },
  symbol: { type: 'string', description: 'map: the name to look up.' },
  file_path: { type: 'string', description: 'map / impact: the file.' },
  project_path: PROJECT,
} as const

// Registered at session.start by register.ts, served below.
export const TOOL_SPECS: readonly ToolSpec[] = [
  {
    name: 'checkpoint',
    description:
      'Bank, read back or list the task checkpoints windvane restores after a compaction and in the next session (save, restore, list); a bare save accepts the record the recorder drafted.',
    inputSchema: { type: 'object', properties: CHECKPOINT_FIELDS, required: ['operation'] },
  },
  {
    name: 'compact_now',
    description:
      'Bank the drafted checkpoint and compact the conversation as soon as this turn ends, so the compaction happens at a step boundary with the state saved; use it when a phase is done and the context is filling.',
    inputSchema: { type: 'object', properties: COMPACT_FIELDS },
  },
  {
    name: 'memory',
    description:
      "Store and manage this project's memory: discoveries, rules and mistakes (remember, recall, search, forget, add_rule, list_rules, modify, delete, promote, archive, restore, list_mistakes, acknowledge_mistake, set_detector).",
    inputSchema: { type: 'object', properties: MEMORY_FIELDS, required: ['operation'] },
  },
  {
    name: 'log',
    description: 'Record a mistake the hooks did not catch, or a decision with its reason, in the project memory (mistake, decision).',
    inputSchema: { type: 'object', properties: LOG_FIELDS, required: ['operation'] },
  },
  {
    name: 'mine',
    description:
      'Search the history of past sessions on this project: conversations, decisions, recurring errors and struggles, a file\'s discussions, the timeline, and this session\'s run report (search, decisions, errors, struggles, replay, timeline, run_report, run_status, status).',
    inputSchema: { type: 'object', properties: MINE_FIELDS, required: ['operation'] },
  },
  {
    name: 'deps',
    description: 'Ask the code index where a symbol is defined and what imports it, or what depends on a file before changing it (map, impact).',
    inputSchema: { type: 'object', properties: DEPS_FIELDS, required: ['operation'] },
  },
]

// The name the model calls each tool by, `mcp__<plugin>__<name>`, spelled
// out so a hook's matcher names one tool.
export const TOOL_NAMES = {
  checkpoint: 'mcp__windvane__checkpoint',
  compact_now: 'mcp__windvane__compact_now',
  memory: 'mcp__windvane__memory',
  log: 'mcp__windvane__log',
  mine: 'mcp__windvane__mine',
  deps: 'mcp__windvane__deps',
} as const

type ToolShort = keyof typeof TOOL_NAMES

type Request = { tool: string; arguments: Record<string, unknown>; env: Record<string, string> }
type Reply = { text: string; isError: boolean; ms?: number }
type Asked = Reply | 'down' | 'timeout'

// The call's arguments, less the engine's envelope fields.
export function argumentsOf(e: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(e)) {
    if (key === 'tool' || key === 'tool_use_id' || key === 'agentId' || key === 'consent' || value === undefined) continue
    out[key] = value
  }
  return out
}

// The engine's answer, or undefined when the value is not one.
export function readReply(raw: unknown): Reply | undefined {
  if (typeof raw !== 'object' || raw === null) return undefined
  const r = raw as { text?: unknown; isError?: unknown; ms?: unknown }
  if (typeof r.text !== 'string') return undefined
  return { text: r.text, isError: r.isError === true, ms: typeof r.ms === 'number' ? r.ms : undefined }
}

// The daemon's answer; 'down' when no handler ran (the caller then runs the
// subprocess), 'timeout' when the request may have run.
async function askDaemon($: EngineInterface, store: string, request: Request): Promise<Asked> {
  let portText: string
  try {
    portText = String(await $.fs.read(`${store}/daemon_port`))
  } catch {
    return 'down'
  }
  const port = parseInt(portText.trim(), 10)
  if (!Number.isInteger(port) || port <= 0) return 'down'

  let timer: Timer | undefined
  const timeout = new Promise<'timeout'>(resolve => {
    timer = $.clock.after(TOOL_TIMEOUT_MS, () => resolve('timeout'))
  })
  try {
    const res = await Promise.race([
      $.http.fetch(`http://127.0.0.1:${port}/tool`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Windvane-Hook': '1' },
        body: JSON.stringify(request),
      }),
      timeout,
    ])
    if (res === 'timeout') return 'timeout'
    if (res.status !== 200) return 'down'
    return readReply(JSON.parse(res.text)) ?? 'down'
  } catch {
    return 'down'
  } finally {
    timer?.cancel()
  }
}

// The subprocess: the request on stdin, one JSON line back.
async function askProcess($: EngineInterface, python: string, store: string, request: Request, sessionId: string): Promise<Reply> {
  let run
  try {
    run = await $.process.run([python, '-m', 'windvane.tools'], {
      stdin: JSON.stringify(request),
      env: engineEnv($.plugin.root, store, sessionId),
      timeoutMs: TOOL_TIMEOUT_MS,
    })
  } catch (err) {
    return { text: `${request.tool}: ${pythonHint(python, err)}`, isError: true }
  }
  const reply = readReply(lastJsonLine(run.stdout))
  if (!reply) return { text: `${request.tool}: ${clip(run.stderr, 300) || `exit ${run.exitCode}, no answer`}`, isError: true }
  return { ...reply, isError: reply.isError || run.exitCode !== 0 }
}

async function serve($: EngineInterface, name: ToolShort, e: Record<string, unknown>, settings: Settings): Promise<{ result: string } | { deny: string }> {
  const store = storePath(await $.env.get('WINDVANE_DIR'), await $.env.get('USERPROFILE'), await $.env.get('HOME'))
  // The arguments as the model gave them: with no project_path the engine
  // resolves the project from the session's own edits (the cwd is often
  // another folder, and a save filed under it lands in the wrong ring).
  const args = argumentsOf(e)
  // The handlers key the recorder's draft and the session state by the
  // session id; a process the mod starts has no session environment.
  const sessionId = await $.session.id()
  const request: Request = { tool: name, arguments: args, env: { CLAUDE_CODE_SESSION_ID: sessionId, WINDVANE_DIR: store } }

  let reply: Reply
  const asked = await askDaemon($, store, request)
  if (asked === 'timeout') {
    reply = {
      text: `${name}: the daemon did not answer within ${TOOL_TIMEOUT_MS / 1000} s and the call may still have run, so it was not repeated. Check its effect before calling again.`,
      isError: true,
    }
  } else if (asked === 'down') {
    reply = await askProcess($, pythonOf(await $.env.get('WINDVANE_PYTHON'), settings.python), store, request, sessionId)
  } else {
    reply = asked
  }

  if (reply.isError) return { deny: reply.text }
  return { result: name === 'compact_now' ? `${reply.text}\n${COMPACT_NOTE}` : reply.text }
}

// One matched hook per tool; each matcher names its tool literally.
export function registerTools(on: On, settings: Settings): void {
  const args = (e: unknown) => e as Record<string, unknown>
  on('tool.call', { tool: TOOL_NAMES.checkpoint }, async ($, e) => serve($, 'checkpoint', args(e), settings))
  on('tool.call', { tool: TOOL_NAMES.compact_now }, async ($, e) => serve($, 'compact_now', args(e), settings))
  on('tool.call', { tool: TOOL_NAMES.memory }, async ($, e) => serve($, 'memory', args(e), settings))
  on('tool.call', { tool: TOOL_NAMES.log }, async ($, e) => serve($, 'log', args(e), settings))
  on('tool.call', { tool: TOOL_NAMES.mine }, async ($, e) => serve($, 'mine', args(e), settings))
  on('tool.call', { tool: TOOL_NAMES.deps }, async ($, e) => serve($, 'deps', args(e), settings))
}

// windvane: the compacted conversation carries the checkpoint.
//
// A compaction replaces the conversation with a summary the model wrote
// under pressure. windvane's checkpoint (the state it banked deliberately)
// and its rules would arrive only afterwards, through the
// SessionStart(compact) banner. Here they travel inside the compaction
// itself: once the engine has compacted, one user-role message is placed
// right after the summary, carrying the rules block and the checkpoint the
// banner would restore (this session's own newest deliberate one, rewinds
// skipped, the project's newest as the fallback), rendered whole by the
// engine (`python -m windvane.brief --checkpoint`).
//
// SessionCompacted.messages is the conversation as it reads afterwards; a
// message a hook adds without a handle is built from its role and text.
// A precompute (kept for a later compaction) and a subagent's own
// compaction pass through untouched, as does a skip.
//
// A compaction windvane itself starts ($.session.compact from register.ts)
// runs beneath windvane's own hooks, so that hook never sees it: there the
// SessionStart(compact) banner carries the rules and the checkpoint, and
// register.ts's continue prompt points the model at it.
//
// register.ts hooks session.compact and places the message compactBrief
// renders; this module never holds `$`.
import { BRIEF_TIMEOUT_MS, briefArgv, joinBlocks, parseBrief, type Brief } from './brief'
import { PLUGIN, engineEnv, type Host } from './engine'

const OPEN_TAG = '<windvane-compact>'
const CLOSE_TAG = '</windvane-compact>'

async function runBrief(host: Host, python: string, extra: string[]): Promise<Brief | undefined> {
  const project = await host.cwd()
  const sid = await host.sessionId()
  const argv = briefArgv(await host.python(python), project, sid, extra)
  const env = engineEnv(host.pluginRoot, await host.store(), sid)
  try {
    const got = parseBrief(await host.run(argv, { cwd: project, env, timeoutMs: BRIEF_TIMEOUT_MS }))
    if (typeof got !== 'string') return got
    host.log(`${PLUGIN}: compact: ${got}`)
  } catch (err) {
    host.log(`${PLUGIN}: compact: brief failed: ${String(err)}`)
  }
  return undefined
}

// The text of the message that follows the summary: the rules block and the
// checkpoint, or undefined when there is nothing to place. Once rendered, the
// marker sessions/<sid>.briefed tells windvane's SessionStart(compact) hook
// the rules and the checkpoint are already in the conversation: its banner
// leaves them out while the marker is fresh.
export async function compactBrief(host: Host, python: string): Promise<string | undefined> {
  const got = await runBrief(host, python, ['--checkpoint'])
  if (!got) return undefined
  const body = joinBlocks([got.rules, got.checkpoint])
  if (!body) return undefined
  try {
    await host.write(`${await host.store()}/sessions/${await host.sessionId()}.briefed`, JSON.stringify({ plugin: PLUGIN, ts: Date.now() / 1000 }))
  } catch (err) {
    host.log(`${PLUGIN}: compact: briefed marker not written: ${String(err)}`)
  }
  return `${OPEN_TAG}\n${body}\n${CLOSE_TAG}`
}

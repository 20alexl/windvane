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
import type { EngineInterface, On, SessionMessage } from 'claude-code'
import { BRIEF_TIMEOUT_MS, briefArgv, joinBlocks, parseBrief, type Brief } from './brief'
import { PLUGIN, engineEnv, pythonOf, type Settings } from './engine'
import { storePath } from './ring'

const OPEN_TAG = '<windvane-compact>'
const CLOSE_TAG = '</windvane-compact>'

async function storeOf($: EngineInterface): Promise<string> {
  return storePath(await $.env.get('WINDVANE_DIR'), await $.env.get('USERPROFILE'), await $.env.get('HOME'))
}

async function runBrief($: EngineInterface, python: string, extra: string[]): Promise<Brief | undefined> {
  const project = await $.session.cwd()
  const sid = await $.session.id()
  const argv = briefArgv(pythonOf(await $.env.get('WINDVANE_PYTHON'), python), project, sid, extra)
  const env = engineEnv($.plugin.root, await storeOf($), sid)
  try {
    const got = parseBrief(await $.process.run(argv, { cwd: project, env, timeoutMs: BRIEF_TIMEOUT_MS }))
    if (typeof got !== 'string') return got
    $.ui.log(`${PLUGIN}: compact: ${got}`)
  } catch (err) {
    $.ui.log(`${PLUGIN}: compact: brief failed: ${String(err)}`)
  }
  return undefined
}

export function registerCompact(on: On, settings: Settings): void {
  // Every trigger but precompute; the matcher also keeps this hook apart
  // from the matcher-less one in register.ts.
  on('session.compact', { trigger: ['manual', 'auto', 'plugin'] }, async ($, e, next) => {
    const out = await next(e)
    if (out.messages === undefined || e.agentId !== undefined) return out

    const got = await runBrief($, settings.python, ['--checkpoint'])
    if (!got) return out
    const body = joinBlocks([got.rules, got.checkpoint])
    if (!body) return out

    const restore: SessionMessage = { role: 'user', text: `${OPEN_TAG}\n${body}\n${CLOSE_TAG}`, toolUses: [] }
    const messages = [...out.messages]
    // After the summary (the first message core hands up), ahead of what it kept.
    messages.splice(messages.length > 0 ? 1 : 0, 0, restore)

    // Tell windvane's SessionStart(compact) hook the rules and the checkpoint
    // are already in the conversation: its banner leaves them out while
    // sessions/<sid>.briefed is fresh.
    try {
      await $.fs.write(`${await storeOf($)}/sessions/${await $.session.id()}.briefed`, JSON.stringify({ plugin: PLUGIN, ts: Date.now() / 1000 }))
    } catch (err) {
      $.ui.log(`${PLUGIN}: compact: briefed marker not written: ${String(err)}`)
    }
    return { ...out, messages }
  })
}

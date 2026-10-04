// windvane: the project's rules reach subagents.
//
// A subagent starts from its prompt alone: none of windvane's SessionStart
// banner, and the pre-edit hook stays silent for it (subagents are skipped
// to save their context). So the rules it should follow and the mistakes
// already made on the files it is sent to touch never reach it. This hook
// rewrites the Agent call's prompt with a header: the project's rules block
// and, for each file the prompt names, the past-mistakes lines, both
// rendered by the engine (`python -m windvane.brief`) exactly as the banner
// and the pre-edit hook render them.
//
// The rules are cached per project for 60 s and each file's lines likewise,
// so a burst of Agent calls costs one engine run. An empty brief, an absent
// engine or a fork (it inherits the whole conversation, rules included)
// passes the call through untouched.
import type { EngineInterface, On } from 'claude-code'
import { BRIEF_TIMEOUT_MS, briefArgv, joinBlocks, parseBrief, type Brief } from './brief'
import { PLUGIN, engineEnv, pythonOf, type Settings } from './engine'
import { storePath } from './ring'

export const BRIEF_TTL_MS = 60_000
const MAX_FILES = 8
const OPEN_TAG = '<windvane-brief>'
const CLOSE_TAG = '</windvane-brief>'

const FILE_EXT =
  /\.(py|pyi|ts|tsx|js|jsx|mjs|cjs|json|md|toml|ya?ml|rs|go|java|kt|c|h|cc|cpp|hpp|cs|rb|php|sh|ps1|lua|luau|sql|html|css|txt|cfg|ini)$/i

// The files a prompt names: path-like tokens with a known extension, URLs
// left out, first mention first, at most MAX_FILES.
export function filesNamed(text: string): string[] {
  const out: string[] = []
  const plain = text.replace(/\b[a-z][a-z0-9+.-]*:\/\/\S+/gi, ' ')
  for (const m of plain.matchAll(/(?:[A-Za-z]:)?[\w./\\-]+/g)) {
    const tok = m[0].replace(/[.\-]+$/, '') // a sentence's closing period
    if (!FILE_EXT.test(tok)) continue
    if (!out.includes(tok)) out.push(tok)
    if (out.length >= MAX_FILES) break
  }
  return out
}

type Cached<T> = { at: number; value: T }

async function runBrief($: EngineInterface, python: string, extra: string[]): Promise<Brief | undefined> {
  const project = await $.session.cwd()
  const sid = await $.session.id()
  const argv = briefArgv(pythonOf(await $.env.get('WINDVANE_PYTHON'), python), project, sid, extra)
  const store = storePath(await $.env.get('WINDVANE_DIR'), await $.env.get('USERPROFILE'), await $.env.get('HOME'))
  const env = engineEnv($.plugin.root, store, sid)
  try {
    const got = parseBrief(await $.process.run(argv, { cwd: project, env, timeoutMs: BRIEF_TIMEOUT_MS }))
    if (typeof got !== 'string') return got
    $.ui.log(`${PLUGIN}: agents: ${got}`)
  } catch (err) {
    $.ui.log(`${PLUGIN}: agents: brief failed: ${String(err)}`)
  }
  return undefined
}

export function registerAgents(on: On, settings: Settings): void {
  // Per-load caches; a reload starts them over.
  const rulesCache = new Map<string, Cached<string[]>>()
  const fileCache = new Map<string, Cached<string[]>>()

  on('tool.call', { tool: 'Agent' }, async ($, e, next) => {
    if (e.subagent_type === 'fork' || e.prompt.includes(OPEN_TAG)) return next(e)

    const project = await $.session.cwd()
    const files = filesNamed(e.prompt)
    const now = Date.now()
    const fresh = <T>(c: Cached<T> | undefined): c is Cached<T> => c !== undefined && now - c.at < BRIEF_TTL_MS
    const fileKey = (f: string) => `${project}\u0000${f}`

    const missing = files.filter(f => !fresh(fileCache.get(fileKey(f))))
    if (!fresh(rulesCache.get(project)) || missing.length > 0) {
      const got = await runBrief($, settings.python, missing.length > 0 ? ['--files', ...missing] : [])
      if (!got) return next(e)
      rulesCache.set(project, { at: now, value: got.rules })
      for (const f of missing) fileCache.set(fileKey(f), { at: now, value: got.files[f] ?? [] })
    }

    const rules = rulesCache.get(project)?.value ?? []
    const perFile = files.map(f => fileCache.get(fileKey(f))?.value ?? [])
    const body = joinBlocks([rules, ...perFile])
    if (!body) return next(e)
    return next({ ...e, prompt: `${OPEN_TAG}\n${body}\n${CLOSE_TAG}\n\n${e.prompt}` })
  })
}

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
//
// register.ts hooks the Agent call and asks briefFor for the rewritten
// prompt; this module never holds `$`.
import { BRIEF_TIMEOUT_MS, briefArgv, joinBlocks, parseBrief, type Brief } from './brief'
import { PLUGIN, engineEnv, type Host } from './engine'

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

async function runBrief(host: Host, python: string, extra: string[]): Promise<Brief | undefined> {
  const project = await host.cwd()
  const sid = await host.sessionId()
  const argv = briefArgv(await host.python(python), project, sid, extra)
  const env = engineEnv(host.pluginRoot, await host.store(), sid)
  try {
    const got = parseBrief(await host.run(argv, { cwd: project, env, timeoutMs: BRIEF_TIMEOUT_MS }))
    if (typeof got !== 'string') return got
    host.log(`${PLUGIN}: agents: ${got}`)
  } catch (err) {
    host.log(`${PLUGIN}: agents: brief failed: ${String(err)}`)
  }
  return undefined
}

// Per-load caches; a reload starts them over.
const rulesCache = new Map<string, Cached<string[]>>()
const fileCache = new Map<string, Cached<string[]>>()

// The Agent call's prompt with the brief at its head, or undefined when the
// call passes through untouched (a fork, a prompt already briefed, an empty
// brief, no engine).
export async function briefFor(host: Host, python: string, call: { subagent_type?: string; prompt: string }): Promise<string | undefined> {
  if (call.subagent_type === 'fork' || call.prompt.includes(OPEN_TAG)) return undefined

  const project = await host.cwd()
  const files = filesNamed(call.prompt)
  const now = Date.now()
  const fresh = <T>(c: Cached<T> | undefined): c is Cached<T> => c !== undefined && now - c.at < BRIEF_TTL_MS
  const fileKey = (f: string) => `${project}\u0000${f}`

  const missing = files.filter(f => !fresh(fileCache.get(fileKey(f))))
  if (!fresh(rulesCache.get(project)) || missing.length > 0) {
    const got = await runBrief(host, python, missing.length > 0 ? ['--files', ...missing] : [])
    if (!got) return undefined
    rulesCache.set(project, { at: now, value: got.rules })
    for (const f of missing) fileCache.set(fileKey(f), { at: now, value: got.files[f] ?? [] })
  }

  const rules = rulesCache.get(project)?.value ?? []
  const perFile = files.map(f => fileCache.get(fileKey(f))?.value ?? [])
  const body = joinBlocks([rules, ...perFile])
  if (!body) return undefined
  return `${OPEN_TAG}\n${body}\n${CLOSE_TAG}\n\n${call.prompt}`
}

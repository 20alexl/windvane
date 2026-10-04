// windvane: windvane's store as the mod reads it. Shared by the status line
// (register.ts), the pane (pane.tsx), the band (band.tsx) and /remember.
//
// Everything here reads the files windvane's Python engine writes; nothing
// is written. The layout is the engine's: manifest.json maps a normalized
// project path to a hash, projects/<hash>/memory.json holds the entries,
// projects/<hash>/latest_handoff.json the newest ring record.
//
// The engine follows $ only into functions of the file that holds the hook,
// never across an import, so the readers here take an Io: two closures the
// calling module builds over its own $ (`ioOf` in each).

// File reads, as $.fs answers them.
export type Io = { read(path: string): Promise<string>; exists(path: string): Promise<boolean> }

export type Manifest = { projects?: Record<string, { hash?: string }> }

// One ring record. The ring writes one name per concept (next_steps,
// files_in_progress, warnings, context_needed); records written before that
// carry the checkpoint-side twins, so every reader takes either.
export type RingRecord = {
  created?: number
  timestamp?: number
  kind?: string
  task_id?: string
  session_id?: string
  project_path?: string
  task_description?: string
  summary?: string
  handoff_summary?: string
  current_step?: string
  completed_steps?: string[]
  next_steps?: string[]
  pending_steps?: string[]
  files_in_progress?: string[]
  files_involved?: string[]
  warnings?: string[]
  handoff_warnings?: string[]
  context_needed?: string[]
  handoff_context_needed?: string[]
  goal?: string
}

// A memory.json entry, the fields the mod reads.
export type MemoryEntry = {
  id?: string
  content?: string
  category?: string
  relevance?: number
  created_at?: number
  archived_at?: number
  related_files?: string[] | string
}

// The engine's path normalization: forward slashes, lowercase drive letter.
export function normalizePath(p: string): string {
  let s = p.replace(/\\/g, '/')
  if (s.length >= 2 && s[1] === ':') s = s.charAt(0).toLowerCase() + s.slice(1)
  return s
}

// The store: WINDVANE_DIR, else ~/.windvane, from the three variables as
// $.env.get answers them (the engine's config.store_dir).
export function storePath(override: string | undefined, userProfile: string | undefined, home: string | undefined): string {
  const base = userProfile ?? home ?? ''
  return ((override && override.trim()) || `${base}/.windvane`).replace(/\\/g, '/')
}

export async function readManifest(io: Io, store: string): Promise<Manifest> {
  try {
    return JSON.parse(await io.read(`${store}/manifest.json`)) as Manifest
  } catch {
    return {}
  }
}

// The newest checkpoint record among the rings that can hold this session's
// saves: the current directory's project, the session root's, and every
// registered project beneath the root (windvane files a save under the work
// project, which may sit below the directory the session was opened in).
// The global ring is the last resort.
export async function readLatest(io: Io, rings: string[], store: string): Promise<RingRecord | undefined> {
  let best: RingRecord | undefined
  for (const p of [...rings, `${store}/checkpoints/latest_handoff.json`]) {
    if (!p) continue
    try {
      if (!(await io.exists(p))) continue
      const rec = JSON.parse(await io.read(p)) as RingRecord
      if (!best || (rec.created ?? 0) > (best.created ?? 0)) best = rec
    } catch {
      // unreadable: try the next
    }
  }
  return best
}

// The rings worth reading, from the store's manifest (normalized path to hash).
export function ringsFor(manifest: Manifest, store: string, cwd: string, root: string): string[] {
  const out: string[] = []
  const projects = manifest.projects ?? {}
  for (const [path, info] of Object.entries(projects)) {
    if (!info?.hash) continue
    if (path === cwd || path === root || path.startsWith(root + '/') || path.startsWith(cwd + '/')) {
      out.push(`${store}/projects/${info.hash}/latest_handoff.json`)
    }
  }
  return out
}

// The registered projects at or above `path`, deepest first: the project a
// path belongs to, then the ancestors whose rules and mistakes it inherits
// (the engine's project memory loader walks the same way).
export function projectChain(manifest: Manifest, path: string): { path: string; hash: string }[] {
  const p = normalizePath(path)
  const out: { path: string; hash: string }[] = []
  for (const [proj, info] of Object.entries(manifest.projects ?? {})) {
    if (!info?.hash) continue
    if (p === proj || p.startsWith(proj.endsWith('/') ? proj : proj + '/')) out.push({ path: proj, hash: info.hash })
  }
  return out.sort((a, b) => b.path.length - a.path.length)
}

// The entries of every project in the chain, the deepest copy of an id kept.
export async function readEntries(io: Io, store: string, chain: { hash: string }[]): Promise<MemoryEntry[]> {
  const seen = new Set<string>()
  const out: MemoryEntry[] = []
  for (const { hash } of chain) {
    let entries: MemoryEntry[] = []
    try {
      const data = JSON.parse(await io.read(`${store}/projects/${hash}/memory.json`)) as { entries?: MemoryEntry[] }
      entries = data.entries ?? []
    } catch {
      continue
    }
    for (const e of entries) {
      const id = e.id ?? ''
      if (id && seen.has(id)) continue
      if (id) seen.add(id)
      out.push(e)
    }
  }
  return out
}

// The project's rules as the engine lists them: most relevant first.
export function rulesOf(entries: MemoryEntry[]): { id: string; content: string }[] {
  return entries
    .filter(e => e.category === 'rule')
    .sort((a, b) => (b.relevance ?? 5) - (a.relevance ?? 5))
    .map(e => ({ id: e.id ?? '', content: e.content ?? '' }))
}

// The engine's generic basenames: names every project has, where only a
// full-path mention says which file is meant.
const GENERIC_BASENAMES = new Set([
  '__init__.py', '__main__.py', '__init__.ts', 'index.js', 'index.ts', 'index.tsx', 'mod.rs', 'setup.py',
  'conftest.py', 'types.ts', 'readme.md', 'claude.md', 'agents.md', 'changelog.md', 'errors.md',
  'learnings.md', 'handoff.md', 'notes.md', 'todo.md', 'plan.md', 'license', 'pyproject.toml', 'setup.cfg',
  'requirements.txt', 'package.json', 'tsconfig.json', 'cargo.toml', 'makefile',
])

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

// The pre-edit check's matching, as the engine runs it: the live mistakes
// (not archived) that name this file, newest first. A specific
// basename matches as a word in the text; a generic one needs the full path;
// a related_files entry naming the file matches either way.
export function mistakesFor(entries: MemoryEntry[], file: string): { id: string; content: string }[] {
  const full = normalizePath(file).toLowerCase()
  const name = full.slice(full.lastIndexOf('/') + 1)
  const generic = GENERIC_BASENAMES.has(name)
  const pattern = new RegExp('(?:^|[\\s/\\\\:])' + escapeRegExp(name) + '(?:[\\s:,.]|$)')
  const out: { id: string; content: string; created: number }[] = []
  for (const e of entries) {
    if (e.archived_at) continue
    const content = e.content ?? ''
    const isMistake = content.toUpperCase().startsWith('MISTAKE:') || e.category === 'mistake'
    if (!isMistake) continue
    const text = content.replace(/^MISTAKE: ?/i, '')
    const lower = content.replace(/\\/g, '/').toLowerCase()
    const related = (Array.isArray(e.related_files) ? e.related_files : e.related_files ? [e.related_files] : []).map(
      f => normalizePath(f).toLowerCase(),
    )
    const byFile = related.some(f => f === full || (!generic && f.endsWith('/' + name)))
    const byText = generic ? lower.includes(full) : pattern.test(content.toLowerCase())
    if (byFile || byText) out.push({ id: e.id ?? '', content: text, created: e.created_at ?? 0 })
  }
  return out.sort((a, b) => b.created - a.created).map(({ id, content }) => ({ id, content }))
}

export function ageText(created?: number, nowMs: number = Date.now()): string {
  if (!created) return 'ckpt none'
  const mins = Math.max(0, Math.round((nowMs / 1000 - created) / 60))
  if (mins < 60) return `ckpt ${mins}m`
  if (mins < 48 * 60) return `ckpt ${(mins / 60).toFixed(1)}h`
  return `ckpt ${Math.round(mins / 1440)}d`
}

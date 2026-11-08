// windvane: tool results trimmed and secrets redacted at the door,
// deterministically, before the row is stored and read.
//
// (a) A tool result's text longer than the budget keeps its head (75% of the
//     budget) and its tail (25%), with one line between them naming how much
//     was cut. The budget is WINDVANE_RESULT_BUDGET when set, else the
//     plugin's result_budget option, else 60_000 characters.
// (b) Secrets in a tool result are redacted: a private key block, a vendor
//     key by its prefix, and a literal value assigned to a key, token or
//     password word (the value alone, so the line stays readable).
//
// The tool-result door only. The person's own prompt is theirs (a token
// pasted there was pasted on purpose), and the model's reply holds nothing
// the model has not already seen. The shapes are narrower than the engine's
// storage gate for mined decisions on purpose: that gate refuses to STORE
// a line, this door rewrites what the
// model reads, and a false positive here breaks the next edit (the redacted
// text no longer matches the file). So no email shape (every git author),
// no bare-token shape (tool ids, base64 in lockfiles), and a value must be a
// literal run of 16+ token characters, so `os.environ["DB_PASSWORD"]` and
// `process.env.OPENAI_KEY` pass.
//
// Only text is touched: a text block, and a tool_result's content (a string,
// or its text blocks). Images and documents pass as they came. A row that
// needs no change goes on as `next(e)`, untouched.
//
// register.ts hooks session.append and rewrites the row with these; this
// module never holds `$`.
import { budgetOf, type Host } from './engine'

export const DEFAULT_BUDGET = 60_000
const HEAD_SHARE = 0.75

export type Block = { type: string; [field: string]: unknown }

// A PEM private key, header to footer (or to the end of the text when the
// footer was cut off).
const PEM_BLOCK = /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)/g
// Vendor keys by prefix: AWS access key id, GitHub tokens, OpenAI-style
// `sk-`, Slack `xox?-`.
const PREFIXED_KEY = /\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}|sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b/g
// A JSON web token by its shape, with or without a key word before it.
const JWT = /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/g
// `api_key = <literal>`, `"password": "<literal>"`, `Bearer <literal>`: group 1
// is kept, the rest is the value. Its first segment is 16+ token characters, so
// a dotted name in source (`password = settings.database_password`) is left
// alone; a dotted tail goes with a value that long. windvane/privacy.py holds
// the same patterns for what the engine writes.
const ASSIGNED_VALUE = /\b((?:api[_ -]?key|secret|token|password|passwd)\b["']?\s*[:=]\s*["']?|bearer\s+)[A-Za-z0-9_\-/+=]{16,}(?:\.[A-Za-z0-9_\-/+=]+)*/gi

export const REDACTED = '[redacted]'

export function redact(text: string): string {
  return text
    .replace(PEM_BLOCK, REDACTED)
    .replace(PREFIXED_KEY, REDACTED)
    .replace(JWT, REDACTED)
    .replace(ASSIGNED_VALUE, (_m: string, head: string) => head + REDACTED)
}

export function trimMarker(cut: number): string {
  return `\n[windvane: ${cut} characters trimmed at the door]\n`
}

// Head and tail of a text over budget; a cut never splits a surrogate pair.
export function trim(text: string, budget: number): string {
  if (text.length <= budget) return text
  let head = Math.floor(budget * HEAD_SHARE)
  let tailStart = text.length - (budget - head)
  if (head > 0 && isHigh(text.charCodeAt(head - 1))) head -= 1
  if (tailStart < text.length && isLow(text.charCodeAt(tailStart))) tailStart += 1
  return text.slice(0, head) + trimMarker(tailStart - head) + text.slice(tailStart)
}

function isHigh(c: number): boolean {
  return c >= 0xd800 && c <= 0xdbff
}

function isLow(c: number): boolean {
  return c >= 0xdc00 && c <= 0xdfff
}

// One text through the door: redacted, then trimmed to the budget.
function pass(text: string, budget: number): string {
  return trim(redact(text), budget)
}

// What each text keeps of a shared budget. A text under its fair share keeps
// every character and the rest split what that leaves, so a short header beside
// a huge body stays whole. Shortest first: each one's fair share is what is left
// over the texts not yet served.
function shares(lengths: readonly number[], budget: number): number[] {
  const order = lengths.map((_, i) => i).sort((a, b) => (lengths[a] ?? 0) - (lengths[b] ?? 0))
  const out = lengths.map(() => 0)
  let left = budget
  order.forEach((i, served) => {
    const kept = Math.min(lengths[i] ?? 0, Math.floor(left / (order.length - served)))
    out[i] = kept
    left -= kept
  })
  return out
}

// The blocks inside one tool result: their texts share its budget. Each took
// the whole budget before, so a result of ten text blocks kept ten budgets. A
// marker is not counted, as for a single text; a text past its share always
// keeps its marker, so none comes out empty.
function rewriteShared(blocks: readonly Block[], budget: number): Block[] | undefined {
  const texts = blocks.map(b => (b.type === 'text' && typeof b.text === 'string' ? redact(b.text) : undefined))
  const kept = shares(texts.filter(t => t !== undefined).map(t => t.length), budget)
  let next = 0
  let changed = false
  const out = blocks.map((b, i) => {
    const text = texts[i]
    if (text === undefined) {
      const nested = b.type === 'tool_result' ? rewrite([b], budget)?.[0] : undefined
      if (nested === undefined) return b
      changed = true
      return nested
    }
    const trimmed = trim(text, kept[next] ?? 0)
    next += 1
    if (trimmed === b.text) return b
    changed = true
    return { ...b, text: trimmed }
  })
  return changed ? out : undefined
}

// The row's blocks through the door; undefined when nothing changed. Each tool
// result has its own budget, parallel results included.
export function rewrite(content: readonly Block[], budget: number): Block[] | undefined {
  let changed = false
  const out = content.map(block => {
    if (block.type === 'text' && typeof block.text === 'string') {
      const text = pass(block.text, budget)
      if (text === block.text) return block
      changed = true
      return { ...block, text }
    }
    if (block.type === 'tool_result') {
      const inner = block.content
      if (typeof inner === 'string') {
        const text = pass(inner, budget)
        if (text === inner) return block
        changed = true
        return { ...block, content: text }
      }
      if (Array.isArray(inner)) {
        const rewritten = rewriteShared(inner as Block[], budget)
        if (rewritten === undefined) return block
        changed = true
        return { ...block, content: rewritten }
      }
    }
    return block
  })
  return changed ? out : undefined
}

// WINDVANE_RESULT_BUDGET when it is a positive whole number of characters,
// else the option's budget, else the default.
export async function readBudget(host: Host, configured: number | undefined): Promise<number> {
  return budgetOf(await host.resultBudgetEnv()) ?? configured ?? DEFAULT_BUDGET
}

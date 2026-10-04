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
import type { EngineInterface, On } from 'claude-code'

import { budgetOf, type Settings } from './engine'

export const DEFAULT_BUDGET = 60_000
const HEAD_SHARE = 0.75

type Block = { type: string; [field: string]: unknown }

// A PEM private key, header to footer (or to the end of the text when the
// footer was cut off).
const PEM_BLOCK = /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)/g
// Vendor keys by prefix: AWS access key id, GitHub tokens, OpenAI-style
// `sk-`, Slack `xox?-`.
const PREFIXED_KEY = /\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}|sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b/g
// `api_key = <literal>`, `password: "<literal>"`, `Bearer <literal>`: group 1
// is kept, group 2 (16+ token characters, no dot) is the value.
const ASSIGNED_VALUE = /\b((?:api[_ -]?key|secret|token|password|passwd)\b\s*[:=]\s*["']?|bearer\s+)([A-Za-z0-9_\-/+=]{16,})/gi

export const REDACTED = '[redacted]'

export function redact(text: string): string {
  return text
    .replace(PEM_BLOCK, REDACTED)
    .replace(PREFIXED_KEY, REDACTED)
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

// The row's blocks through the door; undefined when nothing changed.
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
        const next = rewrite(inner as Block[], budget)
        if (next === undefined) return block
        changed = true
        return { ...block, content: next }
      }
    }
    return block
  })
  return changed ? out : undefined
}

// WINDVANE_RESULT_BUDGET when it is a positive whole number of characters,
// else the option's budget, else the default.
async function readBudget($: EngineInterface, configured: number | undefined): Promise<number> {
  return budgetOf(await $.env.get('WINDVANE_RESULT_BUDGET')) ?? configured ?? DEFAULT_BUDGET
}

export function registerDoor(on: On, settings: Pick<Settings, 'resultBudget'>): void {
  let budget: number | undefined // read once per load

  on('session.append', async ($, e, next) => {
    if (e.door !== 'tool-result') return next(e)
    if (budget === undefined) budget = await readBudget($, settings.resultBudget)
    const content = rewrite(e.message.content, budget)
    if (content === undefined) return next(e)
    return next({ ...e, message: { ...e.message, content } })
  })
}

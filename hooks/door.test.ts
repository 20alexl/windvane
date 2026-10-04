// windvane, the door: a tool result over budget keeps its head and
// tail with one marker line, a short one passes byte-identical, a secret in a
// tool result is redacted, source that merely names a secret is not, and the
// model's own reply (thinking, text, tool_use) is never touched.
//
// The test's session.append hook records the row exactly as the plugin handed
// it down. It cannot store it: the kit (2.1.289) holds every session.append
// hook to the event's rule, so an answer without `next` is skipped, and beneath
// the test's hooks there is no store ("no implementation for session.append").
// `append` below therefore expects that one rejection. A skipped plugin hook
// does not show in it, so each pass-through test first sends a canary row the
// door must change (`proveLive`): a dead hook fails there. Every secret here
// is a fake shape.
import type { On } from 'claude-code'
import type { Engine } from 'claude-code/testing'
import { expect, test } from 'claude-code/testing'

type AppendInput = Parameters<Engine['session']['append']>[0]
type Block = { type: string; [field: string]: unknown }
type Row = { type: string; role?: string; content: Block[] }

const MARKER = (n: number) => `\n[windvane: ${n} characters trimmed at the door]\n`
// A fake AWS-style key id and a PEM private key header with a made-up body.
const FAKE_KEY_ID = 'AKIAFAKE0000EXAMPLE1'
const FAKE_PEM = '-----BEGIN RSA PRIVATE KEY-----\nnot-a-key-just-a-test-body\n-----END RSA PRIVATE KEY-----'

function setup(on: On, env: Record<string, string> = {}): Row[] {
  const stored: Row[] = []
  on('env.get', ($, e) => ({ value: env[e.name] }))
  on('session.append', ($, e, next) => {
    stored.push(e.message)
    return next(e)
  })
  return stored
}

async function append($: Engine, row: AppendInput): Promise<void> {
  let error = ''
  try {
    await $.session.append(row)
  } catch (err) {
    error = String(err)
  }
  expect(error).toContain('no implementation for session.append')
}

// Block `b` of stored row `r`, as the plugin handed it down.
function block(stored: Row[], r: number, b = 0): Block {
  const found = stored[r]?.content[b]
  if (found === undefined) throw new Error(`no block ${b} in row ${r}`)
  return found
}

// The door hook is live: a canary row comes down redacted.
async function proveLive($: Engine, stored: Row[]): Promise<void> {
  await append($, toolResultRow(`canary ${FAKE_KEY_ID}`))
  expect(block(stored, stored.length - 1).content).toBe('canary [redacted]')
}

let n = 0
function toolResultRow(content: unknown): AppendInput {
  n += 1
  return {
    door: 'tool-result',
    origin: { kind: 'tool', tool: 'Bash' },
    uuid: `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`,
    message: {
      type: 'user',
      role: 'user',
      content: [{ type: 'tool_result', tool_use_id: `toolu_${n}`, content }],
    },
  }
}

function responseRow(uuid: string, content: Block[]): AppendInput {
  return {
    door: 'response',
    origin: { kind: 'model', model: 'claude-test' },
    uuid,
    message: { type: 'assistant', role: 'assistant', content },
  }
}

test('a long tool result keeps head and tail with one marker line', async ($, on) => {
  const stored = setup(on)
  const text = 'h'.repeat(65_000) + 't'.repeat(15_000) // 80_000 characters
  const row = toolResultRow(text)
  await append($, row)

  const out = String(block(stored, 0).content)
  // The manifest's default budget, 60_000: head 45_000, tail 15_000, 20_000 cut.
  expect(out).toBe('h'.repeat(45_000) + MARKER(20_000) + 't'.repeat(15_000))
  expect(out.length).toBe(60_000 + MARKER(20_000).length)
  expect(block(stored, 0).tool_use_id).toBe(block([row.message], 0).tool_use_id)
})

test('the result_budget option sets the budget', { options: { result_budget: '2000' } }, async ($, on) => {
  const stored = setup(on)
  await append($, toolResultRow('b'.repeat(5_000)))
  expect(block(stored, 0).content).toBe('b'.repeat(1_500) + MARKER(3_000) + 'b'.repeat(500))
})

test('an option that is no number leaves the default budget', { options: { result_budget: 'lots' } }, async ($, on) => {
  const stored = setup(on)
  await append($, toolResultRow('c'.repeat(70_000)))
  expect(block(stored, 0).content).toBe('c'.repeat(45_000) + MARKER(10_000) + 'c'.repeat(15_000))
})

test('the budget follows WINDVANE_RESULT_BUDGET, and text blocks inside a tool_result are trimmed', { options: { result_budget: '5000' } }, async ($, on) => {
  const stored = setup(on, { WINDVANE_RESULT_BUDGET: '1000' })
  const text = 'a'.repeat(3_000)
  const image = { type: 'image', source: { type: 'base64', data: 'AAAA' } }
  await append($, toolResultRow([{ type: 'text', text }, image]))

  const inner = block(stored, 0).content as Block[]
  expect(inner[0]?.text).toBe('a'.repeat(750) + MARKER(2_000) + 'a'.repeat(250))
  expect(inner[1]).toEqual(image)
})

test('a short tool result passes byte-identical', async ($, on) => {
  const stored = setup(on)
  await proveLive($, stored)
  const row = toolResultRow('exit 0\nall 12 checks passed\n')
  await append($, row)
  expect(JSON.stringify(stored[1])).toBe(JSON.stringify(row.message))
})

test('a fake AWS key id, a private key block and an assigned literal are redacted in a tool result', async ($, on) => {
  const stored = setup(on)
  await append($, toolResultRow(`config:\naws_access_key_id = ${FAKE_KEY_ID}\n${FAKE_PEM}\nend`))
  expect(block(stored, 0).content).toBe('config:\naws_access_key_id = [redacted]\n[redacted]\nend')

  // A key word with a literal value: the value alone goes, the line stays
  // readable. A header's bearer token likewise.
  await append($, toolResultRow('set api_key="fakefakefakefake1234" then\nAuthorization: Bearer fakefakefakefake5678.rest'))
  expect(block(stored, 1).content).toBe('set api_key="[redacted]" then\nAuthorization: Bearer [redacted].rest')

  // The model's reply is not a tool result: it passes as written.
  const reply = responseRow('00000000-0000-4000-8000-0000000000ff', [{ type: 'text', text: `set api_key=fakefakefakefake1234 and ${FAKE_KEY_ID}` }])
  await append($, reply)
  expect(JSON.stringify(stored[2])).toBe(JSON.stringify(reply.message))
})

test('source that names a secret, an author line and a tool id are not secrets', async ($, on) => {
  const stored = setup(on)
  await proveLive($, stored)
  const row = toolResultRow(
    [
      'password = os.environ["DB_PASSWORD"]',
      'api_key: process.env.OPENAI_KEY,',
      'token = settings.TOKEN  # short: "abc12345"',
      'Author: Some Dev <dev@example.com>',
      'toolu_014HmZXPp6gLad7FhHybvYFk integrity sha512-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789==',
      'the Bearer token is sent in the header',
    ].join('\n'),
  )
  await append($, row)
  expect(JSON.stringify(stored[1])).toBe(JSON.stringify(row.message))
})

test('a commit hash is an id, not a secret', async ($, on) => {
  const stored = setup(on)
  await proveLive($, stored)
  const row = toolResultRow('HEAD is 9fa2aa6c0ffee1234567890abcdef0123456789a')
  await append($, row)
  expect(JSON.stringify(stored[1])).toBe(JSON.stringify(row.message))
})

test('a reply with thinking, text and tool_use blocks is never touched', async ($, on) => {
  const stored = setup(on)
  await proveLive($, stored)
  const thinking = { type: 'thinking', thinking: `the key is ${FAKE_KEY_ID}`, signature: 'sig' }
  const toolUse = { type: 'tool_use', id: 'toolu_x', name: 'Bash', input: { command: `echo ${FAKE_KEY_ID}` } }
  const reply = responseRow('00000000-0000-4000-8000-0000000000fe', [thinking, { type: 'text', text: `using ${FAKE_KEY_ID}` }, toolUse])
  await append($, reply)
  expect(JSON.stringify(stored[1])).toBe(JSON.stringify(reply.message))
})

// windvane: a subagent's prompt opens with the project's rules and the
// past mistakes for the files it names, as the engine renders them; an empty
// brief, a failed engine run and a fork leave the call untouched; a burst of
// calls inside the cache window costs one engine run.
//
// The test's hooks are the engine's bottom: op events answer { value },
// tool.call answers { result }.
import type { On } from 'claude-code'
import { expect, test } from 'claude-code/testing'

const SID = 'aaaaaaaa-0000-4000-8000-00000000000b'
const RULES = ['Rules (2, proj):', '  [r1] never push without the word', '  [r2] trash over rm']
const MISTAKES = ['AUTO-CHECK: Past mistakes with src/loader.py:', '  - loader.py read the whole file']

type Run = { argv: readonly string[]; cwd?: string; env?: Record<string, string> }

function engine(on: On, answer: () => object, runs: Run[], prompts: string[]) {
  const env: Record<string, string> = { WINDVANE_PYTHON: 'C:/venv/python.exe', WINDVANE_DIR: 'C:/tmp/windvane-agents-store' }
  on('env.get', ($, e) => ({ value: env[e.name] }))
  on('session.id', () => ({ value: SID }))
  on('session.cwd', () => ({ value: 'E:/demo/proj' }))
  on('ui.log', () => ({ value: undefined }))
  on('process.run', ($, e) => {
    runs.push({ argv: e.argv, cwd: e.init?.cwd, env: e.init?.env })
    return { value: { exitCode: 0, stdout: JSON.stringify(answer()), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('tool.call', ($, e) => {
    prompts.push(String((e as unknown as { prompt?: string }).prompt))
    return { result: { content: [{ type: 'text', text: 'agent done' }] } }
  })
}

test('the brief heads the subagent prompt, file mistakes included', async ($, on) => {
  const runs: Run[] = []
  const prompts: string[] = []
  engine(on, () => ({ rules: RULES, files: { 'src/loader.py': MISTAKES }, checkpoint: [] }), runs, prompts)

  const prompt = 'Fix the cache bug in src/loader.py (see https://example.com/a.py).'
  await $.tool.call({ tool: 'Agent', description: 'fix cache', prompt })

  expect(runs.length).toBe(1)
  expect(runs[0]!.argv.slice(0, 3)).toEqual(['C:/venv/python.exe', '-m', 'windvane.brief'])
  // The engine package inside the plugin, the store and the session ride in env.
  expect(runs[0]!.env?.PYTHONPATH?.endsWith('/windvane')).toBe(true)
  expect(runs[0]!.env?.WINDVANE_DIR).toBe('C:/tmp/windvane-agents-store')
  expect(runs[0]!.env?.CLAUDE_CODE_SESSION_ID).toBe(SID)
  expect(runs[0]!.argv).toContain('--json')
  expect(runs[0]!.argv.join(' ')).toContain(`--project E:/demo/proj --session ${SID}`)
  // Files go last, the URL's path is not one of them.
  expect(runs[0]!.argv.slice(-2)).toEqual(['--files', 'src/loader.py'])

  const sent = prompts[0]!
  expect(sent.startsWith('<windvane-brief>\n' + RULES.join('\n') + '\n\n' + MISTAKES.join('\n') + '\n</windvane-brief>\n\n')).toBe(true)
  expect(sent.endsWith(prompt)).toBe(true)

  // A second call inside the window, same file: no second engine run.
  await $.tool.call({ tool: 'Agent', description: 'again', prompt: 'Review src/loader.py.' })
  expect(runs.length).toBe(1)
  expect(prompts[1]!).toContain(MISTAKES[1]!)

  // A fork inherits the conversation: untouched, no run.
  await $.tool.call({ tool: 'Agent', description: 'fork', prompt: 'look at src/loader.py', subagent_type: 'fork' })
  expect(prompts[2]!).toBe('look at src/loader.py')
  expect(runs.length).toBe(1)
})

test('an empty brief leaves the prompt untouched', async ($, on) => {
  const runs: Run[] = []
  const prompts: string[] = []
  engine(on, () => ({ rules: [], files: {}, checkpoint: [] }), runs, prompts)

  await $.tool.call({ tool: 'Agent', description: 'plain', prompt: 'Summarize the README.' })
  expect(runs.length).toBe(1)
  expect(runs[0]!.argv).not.toContain('--files') // no file named: rules only
  expect(prompts[0]!).toBe('Summarize the README.')
})

test('a failed engine run leaves the prompt untouched', async ($, on) => {
  const prompts: string[] = []
  on('env.get', () => ({ value: undefined }))
  on('session.id', () => ({ value: SID }))
  on('session.cwd', () => ({ value: 'E:/demo/proj' }))
  const logged: string[] = []
  on('ui.log', ($, e) => {
    logged.push(JSON.stringify(e))
    return { value: undefined }
  })
  let python = ''
  on('process.run', ($, e) => {
    python = e.argv[0] ?? ''
    return { value: { exitCode: 1, stdout: '', stderr: 'No module named windvane', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('tool.call', ($, e) => {
    prompts.push(String((e as unknown as { prompt?: string }).prompt))
    return { result: { content: [{ type: 'text', text: 'ok' }] } }
  })

  await $.tool.call({ tool: 'Agent', description: 'plain', prompt: 'Do the thing.' })
  expect(python).toBe('python') // no WINDVANE_PYTHON, no python option: PATH's python
  expect(prompts[0]!).toBe('Do the thing.')
  expect(logged.join('\n')).toContain('No module named windvane')
})

test('the python option names the interpreter; WINDVANE_PYTHON wins over it', { options: { python: 'D:/py/python.exe' } }, async ($, on) => {
  const runs: Run[] = []
  const prompts: string[] = []
  let envPython: string | undefined
  on('env.get', ($, e) => ({ value: e.name === 'WINDVANE_PYTHON' ? envPython : undefined }))
  on('session.id', () => ({ value: SID }))
  on('session.cwd', () => ({ value: 'E:/demo/proj' }))
  on('ui.log', () => ({ value: undefined }))
  on('process.run', ($, e) => {
    runs.push({ argv: e.argv })
    return { value: { exitCode: 0, stdout: JSON.stringify({ rules: [], files: {}, checkpoint: [] }), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('tool.call', ($, e) => {
    prompts.push(String((e as unknown as { prompt?: string }).prompt))
    return { result: { content: [{ type: 'text', text: 'ok' }] } }
  })

  await $.tool.call({ tool: 'Agent', description: 'a', prompt: 'One.' })
  expect(runs[0]!.argv[0]).toBe('D:/py/python.exe')
  // A file the cache has not seen runs the engine again.
  envPython = 'C:/env/python.exe'
  await $.tool.call({ tool: 'Agent', description: 'b', prompt: 'Look at src/b.py.' })
  expect(runs[1]!.argv[0]).toBe('C:/env/python.exe')
})

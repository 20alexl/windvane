// windvane: the first-run offer of the semantic tier. A small embedding
// model (sentence-transformers, bge-base) lets memory and session search
// find paraphrases a keyword match cannot: on a store of 200k chunks,
// hybrid search found the right chunk about six times as often as keyword
// alone. The extra is not installed with the plugin (torch is large) and
// the `semantic` row is off by default, so the first interactive session
// asks once: install and turn on, turn on (the extra is already there),
// not now (asked again in a week) or never. The answer lives in $.store
// under `semanticOffer`. A -p run has no one to ask and is never asked.
//
// This file holds the decisions; register.ts does the $ work at
// session.start, as for the other pieces.

export const SEMANTIC_OFFER_KEY = 'semanticOffer'
export const SEMANTIC_ROW = 'windvane.semantic'
export const EXTRA_PACKAGES = ['sentence-transformers>=2.7.0', 'numpy>=1.24.0']
export const RETRY_AFTER_MS = 7 * 24 * 3600 * 1000
export const CHECK_TIMEOUT_MS = 60_000
// The host caps a run at ten minutes; a slower install ends with the toast
// that says to pip install by hand.
export const INSTALL_TIMEOUT_MS = 600_000

export type Offer = { answer: 'done' | 'never' | 'later'; at: number }

export function recordOf(answer: Offer['answer'], now = Date.now()): Offer {
  return { answer, at: now }
}

// Is the question due? Never after a yes or a never; a week after a not now.
export function offerDue(prior: unknown, now: number): boolean {
  const p = prior as Partial<Offer> | undefined
  if (!p || typeof p !== 'object') return true
  if (p.answer === 'done' || p.answer === 'never') return false
  if (p.answer === 'later') return now - (typeof p.at === 'number' ? p.at : 0) >= RETRY_AFTER_MS
  return true
}

// The semantic row as /config lists it; a userConfig value can arrive as a string.
export function rowOnFrom(rows: ReadonlyArray<{ key: string; value: unknown }>): boolean {
  const row = rows.find(r => r.key === SEMANTIC_ROW)
  return row?.value === true || row?.value === 'true'
}

// The engine's own check of the extra: the modules are looked up, torch is
// not imported.
export function checkArgv(python: string): string[] {
  return [python, '-c', 'from windvane import semantic; print(int(semantic.available()))']
}

export function extraInstalledFrom(run: { exitCode: number; stdout: string }): boolean {
  return run.exitCode === 0 && run.stdout.trim().endsWith('1')
}

export function installArgv(python: string): string[] {
  return [python, '-m', 'pip', 'install', ...EXTRA_PACKAGES]
}

// The question for the state found, or undefined when both are in place.
export function offerFor(rowOn: boolean, installed: boolean): { act: string; question: string } | undefined {
  if (rowOn && installed) return undefined
  if (rowOn) {
    return {
      act: 'Install',
      question:
        'windvane: the semantic row is on but the extra it needs is not installed. Install it (sentence-transformers and numpy, several hundred MB) so memory and session search use the embedding model?',
    }
  }
  if (installed) {
    return {
      act: 'Turn on',
      question:
        'windvane: the semantic extra is installed but the semantic row is off. Turn it on so memory and session search use the embedding model?',
    }
  }
  return {
    act: 'Install and turn on',
    question:
      'windvane can search memory and past sessions with a small embedding model, which finds paraphrases a keyword match misses. Install the extra (sentence-transformers and numpy, several hundred MB) and turn the semantic row on?',
  }
}

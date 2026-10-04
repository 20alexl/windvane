// windvane: the band above the prompt says what the model last read from
// windvane, in one line:
//
//   windvane · 2 rules · 1 mistake · ckpt 12m · ctx 59%
//
// windvane's hooks inject their text as hook context (through the bridge or
// the plugin's command hooks); every such row passes session.append with
// door `hook-context`. The rows carrying windvane's markers are counted
// here, deterministically, from the text the model read; nothing is
// inferred. The checkpoint age and the context fill come from the status
// line's tick (register.ts). Hide is kept in $.store.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, On } from 'claude-code'

import type { WindvaneRead } from '../types'
import { ageText } from './ring'

// $.state values (../types/index.d.ts). The engine reads a reference only
// from a const of the file that uses it, so each module declares its own.
const lastRead = atom({ plugin: 'windvane', key: 'read' } as const, null)
const pressure = atom({ plugin: 'windvane', key: 'pressure' } as const, null)
const bandHidden = atom({ plugin: 'windvane', key: 'bandHidden' } as const, false)

// $.store key: the Hide press, kept across sessions.
export const BAND_HIDDEN_KEY = 'bandHidden'

const SESSION_BANNER = /windvane session started \(([a-z]+)\)/i
const BANNER_RULES = /^Rules \((\d+),/m
const RULE_BLOCK = /<windvane-rule>([\s\S]*?)(?:<\/windvane-rule>|$)/g
const RULE_LINE = /^\s*\[[^\]\s]+\] /
const MISTAKES_HEAD = 'AUTO-CHECK: Past mistakes with this file:'

// The row's text: its text blocks, joined.
function textOf(content: readonly { type: string; [field: string]: unknown }[]): string {
  return content
    .filter(b => b.type === 'text' && typeof b.text === 'string')
    .map(b => b.text as string)
    .join('\n')
}

// The windvane reading in one row's text, or null when the row is not
// windvane's.
export function parseWindvane(text: string, at: number): WindvaneRead | null {
  const banner = SESSION_BANNER.exec(text)
  if (!text.includes('<windvane-') && !banner) return null

  const tags = [...new Set([...text.matchAll(/<windvane-([a-z-]+)/g)].map(m => m[1] ?? ''))].filter(Boolean)

  let rules = 0
  const head = banner ? BANNER_RULES.exec(text) : null
  if (head) rules += Number(head[1])
  for (const block of text.matchAll(RULE_BLOCK)) {
    rules += (block[1] ?? '').split('\n').filter(l => RULE_LINE.test(l)).length
  }

  let mistakes = 0
  const lines = text.split('\n')
  for (let i = 0; i < lines.length; i++) {
    if (lines[i]?.trim() !== MISTAKES_HEAD) continue
    for (let j = i + 1; j < lines.length && (lines[j] ?? '').startsWith('  - '); j++) mistakes++
  }

  return {
    at,
    tags,
    started: banner?.[1],
    rules,
    mistakes,
    checkpointNow: text.includes('<windvane-context>CHECKPOINT NOW'),
    headsUp: text.includes('<windvane-context>Context pressure:'),
    stall: tags.includes('stall'),
  }
}

function plural(n: number, word: string): string {
  return `${n} ${word}${n === 1 ? '' : 's'}`
}

// The band's line, from the reading and the status line's figures.
export function bandLine(r: WindvaneRead, p: { percent?: number; checkpointCreated?: number } | null, nowMs: number): string {
  const parts: string[] = ['windvane']
  if (r.started) parts.push(`session ${r.started}`)
  if (r.rules) parts.push(plural(r.rules, 'rule'))
  if (r.mistakes) parts.push(plural(r.mistakes, 'mistake'))
  if (r.checkpointNow) parts.push('CHECKPOINT NOW')
  else if (r.headsUp) parts.push('heads-up')
  if (r.stall) parts.push('stall')
  if (parts.length === 1) parts.push(...r.tags)
  if (p) {
    parts.push(ageText(p.checkpointCreated, nowMs))
    if (p.percent !== undefined) parts.push(`ctx ${p.percent}%`)
  }
  return parts.join(' · ')
}

async function hideBand($: EngineInterface): Promise<void> {
  await update($, bandHidden, () => true)
  await $.store.set(BAND_HIDDEN_KEY, true)
}

// register.ts's session.start reads the Hide press back from $.store.
export function registerBand(on: On): void {
  // Every row windvane's hooks hand the model. A subagent's rows are its own.
  on('session.append', { door: 'hook-context' }, async ($, e, next) => {
    if (e.agentId === undefined) {
      const reading = parseWindvane(textOf(e.message.content), Date.now())
      if (reading) await update($, lastRead, () => reading)
    }
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey || e.props.view.agentId !== undefined) return next(e)
    const reading = await read($, lastRead)
    if (reading === null || (await read($, bandHidden))) return next(e)
    const figures = await read($, pressure)

    const { Box, Button, Text } = $.ui.resolve(e)
    return (
      <Box key="windvane-band" flexDirection="row">
        <Text dimColor wrap="truncate-end">
          {bandLine(reading, figures, Date.now())}{' '}
        </Text>
        <Button key="windvane-hide" label="Hide" onPress={() => hideBand($)} />
      </Box>
    )
  })
}

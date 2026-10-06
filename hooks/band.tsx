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
//
// register.ts hooks session.append and ui.render, keeps the reading in
// $.state and hands it here to draw; this module never holds `$`.
import type { Elements, RenderElement } from 'claude-code'

import type { Pressure, WindvaneRead } from '../types'
import { ageText } from './ring'

// $.store key: the Hide press, kept across sessions.
export const BAND_HIDDEN_KEY = 'bandHidden'

// The elements a drawing takes: the surface's table, as $.ui.resolve(e)
// hands it out.
export type Draw = Pick<Elements['terminal'], 'Box' | 'Text' | 'Button'>

const SESSION_BANNER = /windvane session started \(([a-z]+)\)/i
const BANNER_RULES = /^Rules \((\d+),/m
const RULE_BLOCK = /<windvane-rule>([\s\S]*?)(?:<\/windvane-rule>|$)/g
const RULE_LINE = /^\s*\[[^\]\s]+\] /
const MISTAKES_HEAD = 'AUTO-CHECK: Past mistakes with this file:'

// The row's text: its text blocks, joined.
export function textOf(content: readonly { type: string; [field: string]: unknown }[]): string {
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
    checkpointAtStepEnd: text.includes('<windvane-context>CHECKPOINT AT THE END OF THIS STEP'),
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
  else if (r.checkpointAtStepEnd) parts.push('checkpoint at step end')
  else if (r.headsUp) parts.push('heads-up')
  if (r.stall) parts.push('stall')
  if (parts.length === 1) parts.push(...r.tags)
  if (p) {
    parts.push(ageText(p.checkpointCreated, nowMs))
    if (p.percent !== undefined) parts.push(`ctx ${p.percent}%`)
  }
  return parts.join(' · ')
}

// The band: the line and the Hide button, which register.ts answers.
export function drawBand(ui: Draw, reading: WindvaneRead, figures: Pressure | null, nowMs: number, onHide: () => void): RenderElement {
  const { Box, Button, Text } = ui
  return (
    <Box key="windvane-band" flexDirection="row">
      <Text dimColor wrap="truncate-end">
        {bandLine(reading, figures, nowMs)}{' '}
      </Text>
      <Button key="windvane-hide" label="Hide" onPress={onHide} />
    </Box>
  )
}

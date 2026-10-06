// windvane's $.state contract: the values its band and pane draw from.

// What the model last read from windvane: one hook-context row carrying
// windvane's tags, counted deterministically from its text.
export type WindvaneRead = {
  // ms since the epoch when the row was appended
  at: number
  // the distinct <windvane-*> tags in the row, without the prefix
  tags: string[]
  // the session banner's source (startup, resume, compact, clear) when read
  started?: string
  // rules the row named: the banner's "Rules (N, ...)" plus each rule line
  // of a <windvane-rule> block
  rules: number
  // mistakes the row showed: the AUTO-CHECK past-mistakes lines
  mistakes: number
  // the engine's margin-band nudge: the trigger is near, save now
  checkpointNow: boolean
  // the early_compaction band's nudge: save at the end of the step in hand
  checkpointAtStepEnd: boolean
  headsUp: boolean
  stall: boolean
}

// The status line's figures, written by its 10 s tick.
export type Pressure = {
  percent?: number
  checkpointCreated?: number
  inBand: boolean
}

export type Line = { id: string; content: string }

// The pane's body, read from the store when /windvane opens it or Refresh
// is pressed.
export type PaneView = {
  loadedAt: number
  store: string
  project?: string
  checkpoint?: {
    created?: number
    kind?: string
    task_id?: string
    project_path?: string
    task_description?: string
    current_step?: string
    completed: string[]
    pending: string[]
    files: string[]
    warnings: string[]
    context_needed: string[]
    handoff?: string
    goal?: string
  }
  rules: Line[]
  file?: string
  mistakes: Line[]
}

declare module 'claude-code' {
  interface PluginState {
    windvane: {
      read: WindvaneRead | null
      pressure: Pressure | null
      bandHidden: boolean
      lastFile: string | null
      pane: PaneView | null
    }
  }
}

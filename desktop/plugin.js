// Baro run board: the Desktop half of the `baro` Hermes plugin (ADR-012).
//
// What this file follows:
// - Contract: website/docs/developer-guide/desktop-plugin-sdk.md:157-176 says a
//   desktop plugin is one ESM file that default-exports
//   { id, name, register(ctx) }. Lines 150-155 and 2451-2455 say it is loaded
//   uncompiled, so there is no JSX and the only imports allowed are
//   '@hermes/plugin-sdk' and 'react'. This file uses React.createElement only.
// - Unified package: desktop-plugin-sdk.md:2130-2157 says `desktop/plugin.js`
//   inside the agent package is copied into desktop-plugins/<id>/. The id must
//   match the package name, so it is `baro`, the plugin.yaml name.
// - Registration: plugins/kanban/dashboard/manifest.json:7-10 mounts the kanban
//   board as its own page (`tab.path: "/kanban"`). The Desktop equivalent of that
//   tab is a route plus a sidebar nav row (desktop-plugin-sdk.md:297-321), so the
//   board here is a full page at `/baro` with a nav entry.
// - Data: the Desktop SDK has no way to read the Python side's `ctx.state`.
//   `ctx.storage` is the renderer's own `hermes.plugin.<id>.*` namespace
//   (desktop-plugin-sdk.md:213 and 2390-2397). Agent-side data reaches the
//   desktop half only through the package's own backend namespace with
//   `ctx.rest` (desktop-plugin-sdk.md:2124-2152 and 2323-2340). That is exactly
//   how plugins/kanban reads its board:
//   plugins/kanban/dashboard/dist/index.js:5 and :264 call
//   `/api/plugins/kanban/`, served by
//   plugins/kanban/dashboard/plugin_api.py:359 (`@router.get("/board")`). So the
//   panel calls, under /api/plugins/baro/:
//     GET /runs        -> { runs: [run_id, ...] }  (storage key `runs`, oldest first)
//     GET /runs/<id>   -> { board: Board.to_dict() }  (storage key `run:<id>`)
//     GET /runs/<id>/activity -> { activity }  (core/activity.py, never stored)
//   It also adds a status-bar chip (STATUSBAR_AREAS.right) for the newest live
//   run and a ⌘K command (PALETTE_AREA); both open the board with
//   host.navigate. No pane: closing a plugin's only pane disables the plugin.
//   Both are the RunRepository keys from core/store.py (ADR-006). The panel also
//   accepts a bare list or a bare board dict. Per desktop-plugin-sdk.md:2200-2206,
//   when the backend half is off `ctx.rest` returns errors and the panel shows
//   them instead of crashing.
// - Polling: plugins/kanban/dashboard/dist/index.js:737-745 loads on mount and
//   clears its timer on unmount. Here the board is re-read every 2000 ms with
//   `ctx.setInterval`, a scoped timer that is also cleared when the plugin is
//   disabled or reloaded (desktop-plugin-sdk.md:202-204 and 2468-2472). Polling
//   runs only while the board status is 'planning' or 'running', and the
//   interval is disposed as soon as the status is anything else or the
//   component unmounts.
// - Board shape: only what core/board.py Board.to_dict() returns (ADR-005).
// - Styling uses theme variables only (desktop-plugin-sdk.md:2116-2122).

import * as React from 'react'
import { PALETTE_AREA, ROUTES_AREA, SIDEBAR_NAV_AREA, STATUSBAR_AREAS, host } from '@hermes/plugin-sdk'

const h = React.createElement
const { useState, useEffect, useCallback, useRef } = React

const PLUGIN_ID = 'baro'
const ROUTE_PATH = '/baro'
const POLL_MS = 2000
// PluginRestOptions.timeoutMs (desktop-plugin-sdk.md:2338-2340) bounds each read, so
// a hung backend request settles as an error instead of staying pending forever.
const REQUEST_TIMEOUT_MS = 10000
const LIVE_STATUSES = ['planning', 'running']

// Column order and the story.state each column holds (ADR-005 states).
const COLUMNS = [
  { state: 'blocked', title: 'Blocked' },
  { state: 'ready', title: 'Ready' },
  { state: 'running', title: 'Running' },
  { state: 'done', title: 'Done' },
  { state: 'failed', title: 'Failed' }
]

const EMPTY = '—'

function isLiveStatus(status) {
  return LIVE_STATUSES.indexOf(status) !== -1
}

function joinIds(ids) {
  return Array.isArray(ids) && ids.length > 0 ? ids.join(', ') : EMPTY
}

function errorText(err) {
  if (!err) return 'Unknown error'
  return typeof err.message === 'string' && err.message ? err.message : String(err)
}

// GET /runs -> { runs: [...] } (or a bare list): run ids, oldest first.
function normaliseRuns(payload) {
  const list = Array.isArray(payload) ? payload : payload && Array.isArray(payload.runs) ? payload.runs : []
  return list.filter(id => typeof id === 'string' && id)
}

// GET /runs/<id> -> { board } (or the bare board dict).
function normaliseBoard(payload) {
  if (!payload || typeof payload !== 'object') return null
  const board = payload.board && typeof payload.board === 'object' ? payload.board : payload
  return Array.isArray(board.stories) ? board : null
}

function groupByState(stories) {
  const groups = {}
  COLUMNS.forEach(col => {
    groups[col.state] = []
  })
  stories.forEach(story => {
    if (story && groups[story.state]) groups[story.state].push(story)
  })
  return groups
}

const styles = {
  page: { display: 'flex', flexDirection: 'column', height: '100%', gap: '12px', padding: '16px', minHeight: 0 },
  header: { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '12px' },
  title: { fontWeight: 600, fontSize: '1rem', color: 'var(--ui-text-primary)' },
  meta: { fontSize: '0.8125rem', color: 'var(--ui-text-secondary)' },
  muted: { fontSize: '0.8125rem', color: 'var(--ui-text-tertiary)' },
  error: { fontSize: '0.8125rem', color: 'var(--ui-text-danger, var(--ui-text-secondary))' },
  clamp: { display: '-webkit-box', WebkitBoxOrient: 'vertical', WebkitLineClamp: 2, overflow: 'hidden' },
  select: {
    background: 'transparent',
    color: 'var(--ui-text-primary)',
    border: '1px solid var(--ui-stroke-secondary)',
    borderRadius: '6px',
    padding: '2px 6px',
    fontSize: '0.8125rem'
  },
  button: {
    background: 'transparent',
    color: 'var(--ui-text-secondary)',
    border: '1px solid var(--ui-stroke-secondary)',
    borderRadius: '6px',
    padding: '2px 8px',
    fontSize: '0.8125rem',
    cursor: 'pointer'
  },
  board: { display: 'flex', gap: '12px', flex: 1, minHeight: 0, minWidth: 0, overflowX: 'auto' },
  column: {
    display: 'flex',
    flexDirection: 'column',
    gap: '8px',
    flex: '1 0 200px',
    minWidth: '200px',
    border: '1px solid var(--ui-stroke-secondary)',
    borderRadius: '8px',
    padding: '8px',
    overflowY: 'auto'
  },
  columnTitle: { fontWeight: 600, fontSize: '0.8125rem', color: 'var(--ui-text-secondary)' },
  card: {
    display: 'flex',
    flexDirection: 'column',
    gap: '4px',
    border: '1px solid var(--ui-stroke-secondary)',
    borderRadius: '6px',
    padding: '8px',
    fontSize: '0.8125rem'
  },
  cardTitle: { fontWeight: 500, color: 'var(--ui-text-primary)' },
  bar: { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '8px', fontSize: '0.8125rem' },
  chip: {
    border: '1px solid var(--ui-stroke-secondary)',
    borderRadius: '999px',
    padding: '1px 8px',
    color: 'var(--ui-text-secondary)'
  },
  chipLive: { border: '1px solid var(--ui-accent)', color: 'var(--ui-accent)' },
  layout: { display: 'flex', gap: '12px', flex: 1, minHeight: 0, minWidth: 0 },
  panel: {
    display: 'flex',
    flexDirection: 'column',
    gap: '6px',
    flex: '0 0 380px',
    border: '1px solid var(--ui-stroke-secondary)',
    borderRadius: '8px',
    padding: '10px',
    overflowY: 'auto',
    fontSize: '0.8125rem'
  },
  step: { display: 'flex', gap: '6px', alignItems: 'baseline' },
  stepKind: { flex: '0 0 44px', color: 'var(--ui-text-tertiary)', fontSize: '0.6875rem', textTransform: 'uppercase' },
  stepText: { color: 'var(--ui-text-primary)', wordBreak: 'break-word', whiteSpace: 'pre-wrap' },
  badge: {
    alignSelf: 'flex-start',
    border: '1px solid var(--ui-accent)',
    color: 'var(--ui-accent)',
    borderRadius: '999px',
    padding: '0 6px',
    fontSize: '0.6875rem'
  }
}

const CARD_NOTE_CHARS = 90

// The card shows a glance; the full text stays in baro_status and on hover.
function cardNote(text) {
  const verdict = /^\[critic\/(pass|fail)\]/.exec(text)
  if (verdict) return verdict[1] === 'pass' ? 'Critic: passed' : 'Critic: sent back for changes'
  const line = text.split('\n')[0].replace(/\s+/g, ' ').trim()
  const sentence = line.split(/(?<=[.!?])\s/)[0]
  return sentence.length > CARD_NOTE_CHARS ? sentence.slice(0, CARD_NOTE_CHARS - 1).trimEnd() + '…' : sentence
}

const STATE_STRIPE = {
  running: 'var(--ui-accent)',
  done: 'var(--ui-text-tertiary)',
  failed: 'var(--ui-text-danger, var(--ui-accent))',
  ready: 'var(--ui-stroke-primary, var(--ui-stroke-secondary))',
  blocked: 'var(--ui-stroke-secondary)'
}

const STEP_LABEL = { team: 'team', start: 'start', run: 'runs', say: 'says', file: 'file', test: 'test', verdict: 'critic', done: 'done', pause: 'waits', error: 'error' }

function StoryCard({ story, agent, selected, onOpen }) {
  const model = agent && agent.model ? agent.model.replace(/^[a-z]+:/, '') : null
  const children = [
    h('div', { key: 'title', style: styles.cardTitle }, story.title || story.id),
    h('div', { key: 'id', style: styles.muted }, [story.id, model].filter(Boolean).join(' · '))
  ]
  if (story.waits_on && story.waits_on.length) {
    children.push(h('div', { key: 'waits', style: styles.meta }, 'Waits on ' + joinIds(story.waits_on)))
  }
  if (story.blocks && story.blocks.length && story.state !== 'done') {
    children.push(h('div', { key: 'blocks', style: styles.muted }, 'Blocks ' + joinIds(story.blocks)))
  }
  if (story.blocked_by_failure) {
    children.push(h('span', { key: 'bbf', style: styles.badge }, 'blocked by failure'))
  }
  if (story.error) {
    children.push(h('div', { key: 'error', style: Object.assign({}, styles.error, styles.clamp), title: story.error }, story.error))
  }
  const sent = (agent && agent.notes_sent) || 0
  const received = (agent && agent.notes_received) || 0
  if (sent || received) {
    children.push(h('div', { key: 'team', style: styles.muted }, '✉ ' + sent + ' sent · ' + received + ' received'))
  }
  const step = agent && agent.last_step
  if (step) {
    const text = (STEP_LABEL[step.kind] || step.kind) + ': ' + step.text
    children.push(h('div', { key: 'step', style: Object.assign({}, styles.meta, styles.clamp), title: text }, cardNote(text)))
  } else if (story.last_progress) {
    children.push(h('div', { key: 'progress', style: styles.muted, title: story.last_progress }, cardNote(story.last_progress)))
  }
  const style = Object.assign(
    {},
    styles.card,
    { cursor: 'pointer', borderLeft: '3px solid ' + (STATE_STRIPE[story.state] || 'var(--ui-stroke-secondary)') },
    selected ? { borderColor: 'var(--ui-accent)', borderLeftColor: 'var(--ui-accent)' } : null
  )
  return h(
    'div',
    {
      style,
      role: 'button',
      tabIndex: 0,
      onClick: () => onOpen(story.id),
      onKeyDown: e => (e.key === 'Enter' ? onOpen(story.id) : null),
      'data-story-id': story.id,
      'data-story-state': story.state
    },
    children
  )
}

// Agents at work, and what the planner is on before any story exists.
function ActivityBar({ board, activity }) {
  if (!activity) return null
  const agents = activity.agents || {}
  const live = board && isLiveStatus(board.status)
  const chips = []
  const working = agents.working || 0
  chips.push(
    h('span', { key: 'working', style: Object.assign({}, styles.chip, working > 0 ? styles.chipLive : null) },
      working + (working === 1 ? ' agent working' : ' agents working'))
  )
  chips.push(h('span', { key: 'done', style: styles.chip }, (agents.done || 0) + ' of ' + (agents.total || 0) + ' stories done'))
  if ((agents.total || 0) === 0) return h('div', { style: styles.bar }, chips.slice(0, 0))
  if (activity.elapsed_secs) {
    const m = Math.floor(activity.elapsed_secs / 60)
    chips.push(h('span', { key: 'time', style: styles.chip }, m + ' min ' + (activity.elapsed_secs % 60) + ' s'))
  }
  if (live && (agents.total || 0) === 0 && activity.plan_note) {
    chips.push(h('span', { key: 'plan', style: styles.meta, title: activity.plan_note }, 'Planning: ' + cardNote(activity.plan_note)))
  }
  return h('div', { style: styles.bar }, chips)
}

function elapsedSince(iso) {
  const start = Date.parse(iso || '')
  if (!start) return null
  const secs = Math.max(0, Math.floor((Date.now() - start) / 1000))
  return Math.floor(secs / 60) + ' min ' + (secs % 60) + ' s'
}

// Before any story exists: the phases and what the planner is doing, live.
function PlanningView({ board, activity }) {
  const phase = (activity && activity.phase) || { index: 0, names: ['Understanding the goal', 'Architect', 'Acceptance checks', 'Planning stories'] }
  const steps = (activity && activity.plan_steps) || []
  const since = elapsedSince(board && board.started_at)
  const stepper = phase.names.map((name, i) => {
    const current = i === phase.index
    const past = i < phase.index
    return h(
      'span',
      { key: name, style: Object.assign({}, styles.chip, current ? styles.chipLive : null, past ? { opacity: 0.6 } : null) },
      (past ? '✓ ' : current ? '● ' : '') + name
    )
  })
  const rows = [
    h('div', { key: 'title', style: styles.title }, 'Planning' + (since ? ' · ' + since : '')),
    h('div', { key: 'stepper', style: styles.bar }, stepper)
  ]
  if (steps.length === 0) {
    rows.push(h('div', { key: 'wait', style: styles.muted }, 'Baro is reading the goal and the repository…'))
  } else {
    steps
      .slice()
      .reverse()
      .forEach((step, i) =>
        rows.push(
          h('div', { key: 'p' + i, style: Object.assign({}, styles.step, i === 0 ? null : { opacity: 0.7 }) },
            h('span', { style: styles.stepText }, step.text))
        )
      )
  }
  return h('div', { style: Object.assign({}, styles.panel, { flex: 1 }) }, rows)
}

// What the agents told each other while they worked (Mozaik coordination),
// plus the plan changing under them. Newest first.
const TEAM_PREVIEW = 4

function TeamFeed({ activity }) {
  const [open, setOpen] = useState(false)
  const team = (activity && activity.team) || []
  if (team.length === 0) return null
  const shown = team.slice().reverse().slice(0, open ? team.length : TEAM_PREVIEW)
  const rows = shown.map((m, i) => {
    const who = m.from === 'plan' ? 'Plan' : m.from
    const to = m.to && m.to.length ? ' → ' + m.to.join(', ') : ''
    const text = m.label + (m.text ? ': ' + m.text : '')
    return h(
      'div',
      { key: 'm' + i, style: styles.step, title: text },
      h('span', { style: Object.assign({}, styles.stepKind, { flex: '0 0 92px', textTransform: 'none' }) }, who + to),
      h('span', { style: Object.assign({}, styles.stepText, styles.clamp) }, text)
    )
  })
  return h(
    'div',
    { style: Object.assign({}, styles.panel, { flex: '0 0 auto', maxHeight: open ? '40%' : 'none' }) },
    h('div', { style: styles.header },
      h('span', { style: styles.columnTitle }, 'Team · agents talking while they work (' + team.length + ')'),
      team.length > TEAM_PREVIEW
        ? h('button', { type: 'button', style: styles.button, onClick: () => setOpen(!open) }, open ? 'Less' : 'All')
        : null),
    rows
  )
}

// Everything one agent did, newest last.
function AgentPanel({ story, agent, onClose }) {
  const rows = [
    h('div', { key: 'head', style: styles.header },
      h('span', { style: styles.title }, story ? story.title || story.id : ''),
      h('button', { type: 'button', style: styles.button, onClick: onClose }, 'Close'))
  ]
  if (!agent) {
    rows.push(h('div', { key: 'none', style: styles.muted }, 'No agent has worked on this story yet.'))
    return h('div', { style: styles.panel }, rows)
  }
  const facts = [agent.model, agent.attempt ? 'attempt ' + agent.attempt : null, agent.status].filter(Boolean)
  rows.push(h('div', { key: 'facts', style: styles.meta }, facts.join(' · ')))
  if (agent.verdict) rows.push(h('div', { key: 'verdict', style: styles.meta, title: agent.verdict }, 'Critic: ' + cardNote(agent.verdict)))
  if (agent.files && agent.files.length) rows.push(h('div', { key: 'files', style: styles.muted }, 'Files: ' + agent.files.join(', ')))
  ;(agent.steps || []).forEach((step, i) => {
    rows.push(
      h('div', { key: 'step' + i, style: styles.step },
        h('span', { style: styles.stepKind }, STEP_LABEL[step.kind] || step.kind),
        h('span', { style: styles.stepText }, step.text))
    )
  })
  return h('div', { style: styles.panel }, rows)
}

function Column({ column, stories, agents, openId, onOpen }) {
  // An empty column keeps its place but not its width, so a run's cards and
  // the agent panel fit side by side.
  const style = stories.length === 0 ? Object.assign({}, styles.column, { flex: '0 0 120px', minWidth: '120px' }) : styles.column
  return h(
    'div',
    { style, 'data-column': column.state },
    h('div', { style: styles.columnTitle }, column.title + ' (' + stories.length + ')'),
    stories.length === 0
      ? h('div', { style: styles.muted }, EMPTY)
      : stories.map(story =>
          h(StoryCard, { key: story.id, story, agent: agents[story.id] || null, selected: story.id === openId, onOpen })
        )
  )
}

function BoardHeader({ board, runs, selected, onSelect, onRefresh, error }) {
  const replans = Array.isArray(board && board.replans) ? board.replans.length : 0
  const items = [
    h('span', { key: 'title', style: styles.title }, 'Baro'),
    h(
      'select',
      {
        key: 'picker',
        style: styles.select,
        value: selected || '',
        disabled: runs.length === 0,
        onChange: e => onSelect(e.target.value || null),
        'aria-label': 'Run'
      },
      runs.length === 0
        ? h('option', { value: '' }, 'No runs yet')
        : runs
            .slice()
            .reverse()
            .map(id => h('option', { key: id, value: id }, id))
    ),
    h('button', { key: 'refresh', type: 'button', style: styles.button, onClick: onRefresh }, 'Refresh')
  ]
  if (board) {
    items.push(
      h('span', { key: 'status', style: styles.meta }, 'Status: ' + (board.status || EMPTY)),
      h('span', { key: 'replans', style: styles.meta }, 'Replans: ' + replans)
    )
  }
  const rows = [h('div', { key: 'row', style: styles.header }, items)]
  if (board && board.goal) {
    rows.push(h('div', { key: 'goal', style: styles.meta }, 'Goal: ' + board.goal))
  }
  if (error) {
    rows.push(h('div', { key: 'error', style: styles.error }, error))
  }
  return h('div', null, rows)
}

function BaroBoardPage({ ctx }) {
  const [runs, setRuns] = useState([])
  const [selected, setSelected] = useState(null)
  const [board, setBoard] = useState(null)
  const [activity, setActivity] = useState(null)
  const [openId, setOpenId] = useState(null)
  const [error, setError] = useState(null)
  // Becomes false once the user picks a run, so a refresh no longer moves the
  // picker to the newest run.
  const followNewest = useRef(true)
  // Every poll tick issues a fresh request, and a pending, slow or hung one never
  // suppresses later ticks. Each request takes a sequence number. A response
  // publishes only if it belongs to the run selected now and is newer than the
  // last response published, so an older reply that arrives late cannot overwrite
  // newer cards and a reply for a run that is no longer selected is dropped.
  const boardSeq = useRef(0)
  const publishedSeq = useRef(0)
  const selectedRef = useRef(null)
  selectedRef.current = selected

  const loadRuns = useCallback(() => {
    return ctx
      .rest('/runs')
      .then(payload => {
        const ids = normaliseRuns(payload)
        setRuns(ids)
        setSelected(current => {
          if (ids.length === 0) return null
          if (followNewest.current || !current || ids.indexOf(current) === -1) return ids[ids.length - 1]
          return current
        })
        setError(null)
      })
      .catch(err => setError('Could not load runs: ' + errorText(err)))
  }, [ctx])

  const loadBoard = useCallback(
    runId => {
      if (!runId) return Promise.resolve()
      const seq = ++boardSeq.current
      const mayPublish = () => runId === selectedRef.current && seq > publishedSeq.current
      return ctx
        .rest('/runs/' + encodeURIComponent(runId), { timeoutMs: REQUEST_TIMEOUT_MS })
        .then(payload => {
          if (!mayPublish()) return
          publishedSeq.current = seq
          setBoard(normaliseBoard(payload))
          setError(null)
          // Best effort: the board stands on its own if this read fails.
          return ctx
            .rest('/runs/' + encodeURIComponent(runId) + '/activity', { timeoutMs: REQUEST_TIMEOUT_MS })
            .then(more => {
              if (runId === selectedRef.current) setActivity(more && more.activity ? more.activity : null)
            })
            .catch(() => {})
        })
        .catch(err => {
          if (!mayPublish()) return
          publishedSeq.current = seq
          setError('Could not load run ' + runId + ': ' + errorText(err))
        })
    },
    [ctx]
  )

  // Load the run list when the page opens.
  useEffect(() => {
    loadRuns()
  }, [loadRuns])

  // Load the board whenever the selected run changes.
  useEffect(() => {
    setBoard(null)
    setActivity(null)
    setOpenId(null)
    if (selected) loadBoard(selected)
  }, [selected, loadBoard])

  // Poll while the shown board belongs to the selected run and is live. Any
  // other status, a change of run, or an unmount runs the cleanup and disposes
  // the interval.
  const status = board ? board.status : null
  const boardRunId = board ? board.run_id : null
  const live = Boolean(selected) && boardRunId === selected && isLiveStatus(status)
  useEffect(() => {
    if (!live) return undefined
    // Unconditional: every tick re-reads, even while an earlier request is pending.
    const dispose = ctx.setInterval(() => {
      loadBoard(selected)
    }, POLL_MS)
    return dispose
  }, [live, selected, ctx, loadBoard])

  const onSelect = useCallback(runId => {
    followNewest.current = false
    setSelected(runId)
  }, [])

  const onRefresh = useCallback(() => {
    loadRuns()
    if (selected) loadBoard(selected)
  }, [loadRuns, loadBoard, selected])

  const stories = board && Array.isArray(board.stories) ? board.stories : []
  const groups = groupByState(stories)

  let body
  if (!selected) {
    body = h('div', { style: styles.muted }, 'No Baro runs yet. Start one with the baro_run tool.')
  } else if (!board) {
    body = h('div', { style: styles.muted }, 'Loading ' + selected + '…')
  } else if (stories.length === 0 && isLiveStatus(board.status)) {
    body = h(PlanningView, { board, activity })
  } else {
    const agents = (activity && activity.stories) || {}
    const columns = h(
      'div',
      { style: styles.board },
      COLUMNS.map(column =>
        h(Column, { key: column.state, column, stories: groups[column.state], agents, openId, onOpen: setOpenId })
      )
    )
    const open = openId ? stories.find(s => s.id === openId) : null
    body = open
      ? h('div', { style: styles.layout }, columns, h(AgentPanel, { story: open, agent: agents[openId] || null, onClose: () => setOpenId(null) }))
      : columns
  }

  return h(
    'div',
    { style: styles.page },
    h(BoardHeader, { board, runs, selected, onSelect, onRefresh, error }),
    h(ActivityBar, { board, activity }),
    h(TeamFeed, { activity }),
    body
  )
}

// Bottom-bar chip: the newest run's agents at work, from any page. Hidden when
// no run is live; a click opens the board.
const STATUS_LIVE_MS = 4000
const STATUS_IDLE_MS = 15000

function BaroStatus({ ctx }) {
  const [summary, setSummary] = useState(null)
  const [live, setLive] = useState(false)
  const read = useCallback(() => {
    return ctx
      .rest('/runs', { timeoutMs: REQUEST_TIMEOUT_MS })
      .then(payload => {
        const ids = normaliseRuns(payload)
        if (ids.length === 0) return null
        const id = encodeURIComponent(ids[ids.length - 1])
        return Promise.all([
          ctx.rest('/runs/' + id, { timeoutMs: REQUEST_TIMEOUT_MS }),
          ctx.rest('/runs/' + id + '/activity', { timeoutMs: REQUEST_TIMEOUT_MS }).catch(() => null)
        ])
      })
      .then(pair => {
        const board = pair ? normaliseBoard(pair[0]) : null
        const isLive = Boolean(board) && isLiveStatus(board.status)
        setLive(isLive)
        setSummary(isLive ? { board, activity: pair[1] && pair[1].activity } : null)
      })
      .catch(() => {
        setLive(false)
        setSummary(null)
      })
  }, [ctx])
  useEffect(() => {
    read()
    return ctx.setInterval(read, live ? STATUS_LIVE_MS : STATUS_IDLE_MS)
  }, [ctx, read, live])
  if (!summary) return null
  const agents = (summary.activity && summary.activity.agents) || {}
  const stories = summary.board.stories || []
  const done = stories.filter(s => s.state === 'done').length
  const label =
    stories.length === 0
      ? 'Baro · planning'
      : 'Baro · ' + (agents.working || 0) + ' working · ' + done + '/' + stories.length + ' done'
  return h(
    'button',
    {
      type: 'button',
      onClick: () => host.navigate(ROUTE_PATH),
      title: 'Open the Baro board',
      style: {
        background: 'transparent',
        border: 'none',
        cursor: 'pointer',
        padding: '0 6px',
        fontSize: '0.6875rem',
        color: 'var(--ui-accent)'
      }
    },
    label
  )
}

export default {
  id: PLUGIN_ID,
  name: 'Baro',
  register(ctx) {
    ctx.registerMany([
      {
        id: 'board',
        area: ROUTES_AREA,
        data: { path: ROUTE_PATH },
        render: () => h(BaroBoardPage, { ctx })
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: ROUTE_PATH, label: 'Baro', codicon: 'project' }
      },
      {
        id: 'status',
        area: STATUSBAR_AREAS.right,
        order: 140,
        render: () => h(BaroStatus, { ctx })
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'baro.open',
          label: 'Baro: Open board',
          keywords: ['baro', 'board', 'agents', 'kanban'],
          run: () => host.navigate(ROUTE_PATH)
        }
      }
    ])
  }
}

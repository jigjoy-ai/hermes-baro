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
import { ROUTES_AREA, SIDEBAR_NAV_AREA } from '@hermes/plugin-sdk'

const h = React.createElement
const { useState, useEffect, useCallback, useRef } = React

const PLUGIN_ID = 'baro'
const ROUTE_PATH = '/baro'
const POLL_MS = 2000
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
  board: { display: 'flex', gap: '12px', flex: 1, minHeight: 0, overflowX: 'auto' },
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
  badge: {
    alignSelf: 'flex-start',
    border: '1px solid var(--ui-accent)',
    color: 'var(--ui-accent)',
    borderRadius: '999px',
    padding: '0 6px',
    fontSize: '0.6875rem'
  }
}

function StoryCard({ story }) {
  const children = [
    h('div', { key: 'title', style: styles.cardTitle }, story.title || story.id),
    h('div', { key: 'id', style: styles.muted }, story.id),
    h('div', { key: 'waits', style: styles.meta }, 'Waits on: ' + joinIds(story.waits_on)),
    h('div', { key: 'blocks', style: styles.meta }, 'Blocks: ' + joinIds(story.blocks))
  ]
  if (story.blocked_by_failure) {
    children.push(h('span', { key: 'bbf', style: styles.badge }, 'blocked by failure'))
  }
  if (story.error) {
    children.push(h('div', { key: 'error', style: styles.error }, story.error))
  }
  if (story.last_progress) {
    children.push(h('div', { key: 'progress', style: styles.muted }, story.last_progress))
  }
  return h('div', { style: styles.card, 'data-story-id': story.id, 'data-story-state': story.state }, children)
}

function Column({ column, stories }) {
  return h(
    'div',
    { style: styles.column, 'data-column': column.state },
    h('div', { style: styles.columnTitle }, column.title + ' (' + stories.length + ')'),
    stories.length === 0
      ? h('div', { style: styles.muted }, EMPTY)
      : stories.map(story => h(StoryCard, { key: story.id, story }))
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
  const [error, setError] = useState(null)
  // Becomes false once the user picks a run, so a refresh no longer moves the
  // picker to the newest run.
  const followNewest = useRef(true)
  // Only the latest board request may publish (selection changes and overlapping
  // ticks must not let an older response overwrite a newer one).
  const boardSeq = useRef(0)
  const inFlight = useRef(false)

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
      inFlight.current = true
      return ctx
        .rest('/runs/' + encodeURIComponent(runId))
        .then(payload => {
          if (seq !== boardSeq.current) return
          setBoard(normaliseBoard(payload))
          setError(null)
        })
        .catch(err => {
          if (seq !== boardSeq.current) return
          setError('Could not load run ' + runId + ': ' + errorText(err))
        })
        .then(() => {
          if (seq === boardSeq.current) inFlight.current = false
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
    const dispose = ctx.setInterval(() => {
      if (!inFlight.current) loadBoard(selected)
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
  } else {
    body = h(
      'div',
      { style: styles.board },
      COLUMNS.map(column => h(Column, { key: column.state, column, stories: groups[column.state] }))
    )
  }

  return h(
    'div',
    { style: styles.page },
    h(BoardHeader, { board, runs, selected, onSelect, onRefresh, error }),
    body
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
      }
    ])
  }
}

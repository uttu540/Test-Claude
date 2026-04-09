import { useState, useEffect, useRef, useCallback } from 'react'
import { useWebSocket } from '../ws'

const MAX_ENTRIES = 500

const LEVEL_STYLES = {
  error:    { text: 'text-red-trade',    label: 'ERR',  dot: 'bg-red-trade'    },
  warning:  { text: 'text-yellow-trade', label: 'WARN', dot: 'bg-yellow-trade' },
  info:     { text: 'text-blue-trade',   label: 'INFO', dot: 'bg-blue-trade'   },
  debug:    { text: 'text-text-muted',   label: 'DBG',  dot: 'bg-text-muted'   },
}

const LEVELS = ['error', 'warning', 'info', 'debug']

function levelStyle(level) {
  return LEVEL_STYLES[level?.toLowerCase()] ?? LEVEL_STYLES.debug
}

function LogRow({ entry }) {
  const style = levelStyle(entry.level)
  // Build extra fields string (everything except well-known keys)
  const SKIP = new Set(['event', 'level', 'logger', 'timestamp', '_record'])
  const extras = Object.entries(entry)
    .filter(([k]) => !SKIP.has(k))
    .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`)
    .join('  ')

  return (
    <div className="flex items-start gap-2 px-3 py-0.5 font-mono text-xs hover:bg-bg-hover group">
      <span className="text-text-muted shrink-0 tabular-nums w-16">{entry.timestamp ?? ''}</span>
      <span className={`shrink-0 w-8 font-semibold ${style.text}`}>{style.label}</span>
      <span className="text-text-muted shrink-0 hidden lg:block w-40 truncate" title={entry.logger ?? ''}>
        {entry.logger ?? ''}
      </span>
      <span className={`shrink-0 ${style.text} font-medium`}>{entry.event ?? ''}</span>
      {extras && (
        <span className="text-text-muted break-all">{extras}</span>
      )}
    </div>
  )
}

export default function Logs() {
  const [entries, setEntries]         = useState([])
  const [filter, setFilter]           = useState('all')   // 'all' | level name
  const [paused, setPaused]           = useState(false)
  const [search, setSearch]           = useState('')
  const bottomRef                     = useRef(null)
  const pausedRef                     = useRef(false)

  // Keep ref in sync so the WS callback closure always reads the latest value
  useEffect(() => { pausedRef.current = paused }, [paused])

  const handleMessage = useCallback((msg) => {
    if (msg.type !== 'log') return
    if (pausedRef.current) return
    setEntries(prev => {
      const next = [...prev, msg.data]
      return next.length > MAX_ENTRIES ? next.slice(next.length - MAX_ENTRIES) : next
    })
  }, [])

  useWebSocket(handleMessage)

  // Auto-scroll to bottom when new entries arrive (unless paused)
  useEffect(() => {
    if (!paused) bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [entries, paused])

  const visible = entries.filter(e => {
    if (filter !== 'all' && e.level?.toLowerCase() !== filter) return false
    if (search) {
      const q = search.toLowerCase()
      return (
        e.event?.toLowerCase().includes(q) ||
        e.logger?.toLowerCase().includes(q) ||
        JSON.stringify(e).toLowerCase().includes(q)
      )
    }
    return true
  })

  return (
    <div className="flex flex-col h-[calc(100vh-52px)] bg-bg-primary">
      {/* Toolbar */}
      <div className="flex items-center gap-2 px-4 py-2 border-b border-border bg-bg-card shrink-0 flex-wrap">
        <span className="text-xs font-medium text-text-secondary">Logs</span>
        <span className="text-text-muted text-xs">({visible.length})</span>

        <div className="w-px h-4 bg-border" />

        {/* Level filters */}
        <div className="flex items-center gap-1">
          {['all', ...LEVELS].map(lvl => (
            <button
              key={lvl}
              onClick={() => setFilter(lvl)}
              className={`px-2 py-0.5 rounded text-xs font-medium transition-colors ${
                filter === lvl
                  ? 'bg-blue-trade/20 text-blue-trade border border-blue-trade/30'
                  : 'text-text-muted hover:text-text-primary hover:bg-bg-hover border border-transparent'
              }`}
            >
              {lvl === 'all' ? 'All' : levelStyle(lvl).label}
            </button>
          ))}
        </div>

        <div className="w-px h-4 bg-border" />

        {/* Search */}
        <input
          type="text"
          placeholder="Search…"
          value={search}
          onChange={e => setSearch(e.target.value)}
          className="bg-bg-primary border border-border rounded px-2 py-0.5 text-xs text-text-primary placeholder:text-text-muted focus:outline-none focus:border-blue-trade w-40"
        />

        <div className="flex-1" />

        {/* Pause / Resume */}
        <button
          onClick={() => setPaused(p => !p)}
          className={`px-2.5 py-1 rounded text-xs font-medium border transition-colors ${
            paused
              ? 'bg-yellow-trade/10 text-yellow-trade border-yellow-trade/30'
              : 'bg-bg-hover text-text-secondary border-border hover:text-text-primary'
          }`}
        >
          {paused ? '▶ Resume' : '⏸ Pause'}
        </button>

        <button
          onClick={() => setEntries([])}
          className="px-2.5 py-1 rounded text-xs font-medium border border-border text-text-muted hover:text-text-primary hover:bg-bg-hover transition-colors"
        >
          Clear
        </button>
      </div>

      {/* Log output */}
      <div className="flex-1 overflow-y-auto bg-bg-primary py-1">
        {visible.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full gap-2 text-text-muted">
            <span className="font-mono text-3xl">_</span>
            <p className="text-xs">Waiting for log events…</p>
          </div>
        ) : (
          visible.map((e, i) => <LogRow key={i} entry={e} />)
        )}
        <div ref={bottomRef} />
      </div>

      {/* Footer status */}
      {paused && (
        <div className="shrink-0 px-4 py-1 bg-yellow-trade/10 border-t border-yellow-trade/20 text-xs text-yellow-trade font-mono">
          Stream paused — new logs are being dropped
        </div>
      )}
    </div>
  )
}

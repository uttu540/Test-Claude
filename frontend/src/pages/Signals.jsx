import { useState, useEffect, useCallback } from 'react'
import { fetchRecentSignals } from '../api'
import { useWebSocket } from '../ws'

function DirectionBadge({ direction }) {
  if (direction === 'LONG') {
    return (
      <span className="badge bg-green-muted text-green-trade border border-green-trade/20">
        <svg className="w-2.5 h-2.5" fill="currentColor" viewBox="0 0 24 24">
          <path d="M4 12l1.41 1.41L11 7.83V20h2V7.83l5.58 5.59L20 12l-8-8-8 8z" />
        </svg>
        LONG
      </span>
    )
  }
  return (
    <span className="badge bg-red-muted text-red-trade border border-red-trade/20">
      <svg className="w-2.5 h-2.5" fill="currentColor" viewBox="0 0 24 24">
        <path d="M20 12l-1.41-1.41L13 16.17V4h-2v12.17l-5.58-5.59L4 12l8 8 8-8z" />
      </svg>
      SHORT
    </span>
  )
}

function ConfidenceBar({ value = 0 }) {
  const pct   = Math.min(Math.max(value, 0), 100)
  const color = pct >= 80 ? 'bg-green-trade' : pct >= 60 ? 'bg-yellow-trade' : 'bg-text-muted'
  const text  = pct >= 80 ? 'text-green-trade' : pct >= 60 ? 'text-yellow-trade' : 'text-text-muted'
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 bg-bg-hover rounded-full overflow-hidden max-w-[80px]">
        <div className={`h-full ${color} rounded-full transition-all`} style={{ width: `${pct}%` }} />
      </div>
      <span className={`font-mono text-xs ${text}`}>{pct}%</span>
    </div>
  )
}

function formatTime(isoString) {
  if (!isoString) return '—'
  try {
    return new Date(isoString).toLocaleTimeString('en-IN', {
      timeZone: 'Asia/Kolkata',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    })
  } catch {
    return '—'
  }
}

function IndicatorsPopover({ indicators }) {
  const [open, setOpen] = useState(false)
  if (!indicators || Object.keys(indicators).length === 0) return null

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="text-2xs text-text-muted hover:text-text-secondary font-mono border border-border rounded px-1.5 py-0.5 transition-colors"
      >
        {Object.keys(indicators).length} indicators
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-7 z-20 card p-3 w-56 shadow-xl text-xs font-mono space-y-1">
            {Object.entries(indicators).map(([k, v]) => (
              <div key={k} className="flex justify-between gap-2">
                <span className="text-text-muted truncate">{k}</span>
                <span className="text-text-secondary shrink-0">
                  {typeof v === 'number' ? v.toFixed(2) : String(v)}
                </span>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

function SkeletonRow() {
  return (
    <tr>
      {[1, 2, 3, 4, 5, 6, 7].map((i) => (
        <td key={i} className="td"><div className="skeleton h-4 w-16" /></td>
      ))}
    </tr>
  )
}

const DIRECTIONS = ['ALL', 'LONG', 'SHORT']

export default function Signals() {
  const [signals, setSignals]           = useState([])
  const [loading, setLoading]           = useState(true)
  const [error, setError]               = useState(null)
  const [dirFilter, setDirFilter]       = useState('ALL')
  const [search, setSearch]             = useState('')
  const [lastUpdated, setLastUpdated]   = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await fetchRecentSignals()
      setSignals(data)
      setLastUpdated(new Date())
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  // Live WS updates — prepend new signals
  const handleWsMessage = useCallback((msg) => {
    if (msg?.type === 'signal' && msg.data) {
      setSignals((prev) => {
        const next = [msg.data, ...prev.filter((s) => s.symbol !== msg.data.symbol)]
        setLastUpdated(new Date())
        return next.slice(0, 50)
      })
    }
  }, [])
  useWebSocket(handleWsMessage)

  // Filter
  const filtered = signals.filter((s) => {
    const matchDir    = dirFilter === 'ALL' || s.direction === dirFilter
    const matchSearch = !search || s.symbol?.toLowerCase().includes(search.toLowerCase())
    return matchDir && matchSearch
  })

  const longCount  = signals.filter((s) => s.direction === 'LONG').length
  const shortCount = signals.filter((s) => s.direction === 'SHORT').length

  const updatedStr = lastUpdated?.toLocaleTimeString('en-IN', {
    timeZone: 'Asia/Kolkata',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })

  return (
    <div className="p-5 space-y-5 max-w-screen-xl mx-auto animate-fade-in">

      {/* Header */}
      <div className="page-header">
        <div className="flex items-center gap-3">
          <h1 className="page-title">Signals</h1>
          <span className="badge bg-blue-muted text-blue-trade border border-blue-trade/20">
            {signals.length} active
          </span>
        </div>
        <div className="flex items-center gap-2">
          {updatedStr && (
            <span className="text-2xs text-text-muted font-mono hidden sm:block">
              Updated {updatedStr} IST
            </span>
          )}
          <button
            onClick={load}
            disabled={loading}
            className="btn bg-bg-card border border-border text-text-muted hover:text-text-primary text-xs"
          >
            <svg className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
            Refresh
          </button>
        </div>
      </div>

      {/* Stats strip */}
      <div className="grid grid-cols-3 gap-3">
        <div className="card p-3 text-center">
          <div className="section-label mb-1">Total</div>
          <div className="font-mono text-2xl font-bold text-text-primary">{signals.length}</div>
        </div>
        <div className="card p-3 text-center">
          <div className="section-label mb-1">Long</div>
          <div className="font-mono text-2xl font-bold text-green-trade">{longCount}</div>
        </div>
        <div className="card p-3 text-center">
          <div className="section-label mb-1">Short</div>
          <div className="font-mono text-2xl font-bold text-red-trade">{shortCount}</div>
        </div>
      </div>

      {/* Filters */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="flex items-center rounded-lg border border-border bg-bg-card overflow-hidden">
          {DIRECTIONS.map((d) => (
            <button
              key={d}
              onClick={() => setDirFilter(d)}
              className={`px-3 py-1.5 text-xs font-medium transition-colors ${
                dirFilter === d
                  ? 'bg-blue-trade/15 text-blue-trade'
                  : 'text-text-muted hover:text-text-secondary'
              }`}
            >
              {d}
            </button>
          ))}
        </div>
        <input
          type="text"
          placeholder="Search symbol…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="h-8 px-3 text-sm bg-bg-card border border-border rounded-lg text-text-primary placeholder-text-muted focus:outline-none focus:border-blue-trade/50 font-mono w-40"
        />
        {(dirFilter !== 'ALL' || search) && (
          <button
            onClick={() => { setDirFilter('ALL'); setSearch('') }}
            className="text-xs text-text-muted hover:text-text-secondary transition-colors"
          >
            Clear filters
          </button>
        )}
      </div>

      {error && (
        <div className="card px-4 py-3 bg-red-trade/5 border-red-trade/30 text-sm text-red-trade font-mono">
          {error}
        </div>
      )}

      {/* Table */}
      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[700px]">
            <thead>
              <tr>
                <th className="th">Symbol</th>
                <th className="th">Direction</th>
                <th className="th">Signal</th>
                <th className="th">Timeframe</th>
                <th className="th">Confidence</th>
                <th className="th text-right">Price</th>
                <th className="th text-right">Time (IST)</th>
                <th className="th text-right">Details</th>
              </tr>
            </thead>
            <tbody>
              {loading
                ? Array.from({ length: 8 }).map((_, i) => <SkeletonRow key={i} />)
                : filtered.length === 0
                ? (
                  <tr>
                    <td colSpan={8} className="td text-center py-12">
                      <div className="flex flex-col items-center gap-2 text-text-muted">
                        <svg className="w-8 h-8 opacity-40" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1}>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
                        </svg>
                        <span className="text-sm">
                          {signals.length === 0 ? 'No signals yet — bot is analyzing market data' : 'No signals match your filters'}
                        </span>
                      </div>
                    </td>
                  </tr>
                )
                : filtered.map((s, i) => (
                  <tr key={`${s.symbol}-${i}`} className="table-row-hover animate-fade-in">
                    <td className="td">
                      <span className="font-mono font-semibold text-text-primary">{s.symbol}</span>
                    </td>
                    <td className="td">
                      <DirectionBadge direction={s.direction} />
                    </td>
                    <td className="td">
                      <span className="text-xs text-text-secondary font-medium uppercase tracking-wide">
                        {s.signal ?? s.signal_type ?? '—'}
                      </span>
                    </td>
                    <td className="td">
                      <span className="badge bg-bg-hover text-text-muted border border-border text-2xs font-mono">
                        {s.timeframe ?? '—'}
                      </span>
                    </td>
                    <td className="td">
                      <ConfidenceBar value={s.confidence} />
                    </td>
                    <td className="td text-right">
                      <span className="font-mono text-sm text-text-primary">
                        {s.price != null
                          ? `₹${Number(s.price).toLocaleString('en-IN', { minimumFractionDigits: 2 })}`
                          : '—'}
                      </span>
                    </td>
                    <td className="td text-right">
                      <span className="font-mono text-xs text-text-muted">
                        {formatTime(s.timestamp ?? s.time)}
                      </span>
                    </td>
                    <td className="td text-right">
                      <IndicatorsPopover indicators={s.indicators} />
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      </div>

      <p className="text-xs text-text-muted">Signals expire after 15 min. Live updates via WebSocket.</p>
    </div>
  )
}

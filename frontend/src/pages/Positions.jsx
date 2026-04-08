import { useState, useEffect, useCallback } from 'react'
import { fetchPositions } from '../api'
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

function PriceMono({ price }) {
  if (price == null) return <span className="font-mono text-text-muted">—</span>
  return (
    <span className="font-mono text-sm text-text-primary">
      ₹{Number(price).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
    </span>
  )
}

function formatDateTime(isoString) {
  if (!isoString) return '—'
  try {
    return new Date(isoString).toLocaleString('en-IN', {
      timeZone: 'Asia/Kolkata',
      day: '2-digit',
      month: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
  } catch {
    return '—'
  }
}

function RiskRewardBar({ entry, stop, target }) {
  if (entry == null || stop == null || target == null) return null
  const risk   = Math.abs(entry - stop)
  const reward = Math.abs(target - entry)
  const rr     = risk > 0 ? (reward / risk).toFixed(1) : '—'
  return (
    <span className="font-mono text-xs text-text-muted">
      1:{rr}
    </span>
  )
}

function SkeletonCard() {
  return (
    <div className="card p-4 space-y-3 animate-pulse">
      <div className="flex items-center justify-between">
        <div className="skeleton h-5 w-24" />
        <div className="skeleton h-5 w-16" />
      </div>
      <div className="grid grid-cols-3 gap-3">
        {[1, 2, 3].map((i) => (
          <div key={i} className="space-y-1">
            <div className="skeleton h-3 w-12" />
            <div className="skeleton h-4 w-16" />
          </div>
        ))}
      </div>
    </div>
  )
}

function PositionCard({ position: p }) {
  const isLong = p.direction === 'LONG'
  const borderColor = isLong ? 'border-l-green-trade' : 'border-l-red-trade'
  const glowClass   = isLong ? '' : ''

  return (
    <div className={`card overflow-hidden border-l-2 ${borderColor} ${glowClass}`}>
      <div className="p-4">
        {/* Header */}
        <div className="flex items-start justify-between mb-3">
          <div>
            <span className="font-mono font-bold text-text-primary text-base">
              {p.trading_symbol}
            </span>
            {p.exchange && (
              <span className="ml-2 text-2xs text-text-muted font-mono">{p.exchange}</span>
            )}
            {p.strategy_name && (
              <div className="text-2xs text-text-muted mt-0.5">{p.strategy_name}</div>
            )}
          </div>
          <div className="flex flex-col items-end gap-1">
            <DirectionBadge direction={p.direction} />
            {p.broker && (
              <span className="text-2xs text-text-muted font-mono">{p.broker}</span>
            )}
          </div>
        </div>

        {/* Prices grid */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-3">
          <div>
            <div className="section-label mb-1">Entry Price</div>
            <PriceMono price={p.entry_price} />
          </div>
          <div>
            <div className="section-label mb-1">Quantity</div>
            <span className="font-mono text-sm text-text-primary">
              {p.entry_quantity ?? '—'}
            </span>
          </div>
          <div>
            <div className="section-label mb-1">Stop Loss</div>
            <span className="font-mono text-sm text-red-trade">
              {p.planned_stop_loss != null
                ? `₹${Number(p.planned_stop_loss).toLocaleString('en-IN', { minimumFractionDigits: 2 })}`
                : '—'}
            </span>
          </div>
          <div>
            <div className="section-label mb-1">Target</div>
            <span className="font-mono text-sm text-green-trade">
              {p.planned_target_1 != null
                ? `₹${Number(p.planned_target_1).toLocaleString('en-IN', { minimumFractionDigits: 2 })}`
                : '—'}
            </span>
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between pt-2 border-t border-border/50">
          <div className="flex items-center gap-3">
            {p.initial_risk_amount != null && (
              <span className="text-xs text-text-muted">
                Risk: <span className="font-mono text-red-trade">
                  ₹{Math.abs(p.initial_risk_amount).toLocaleString('en-IN', { minimumFractionDigits: 0 })}
                </span>
              </span>
            )}
            <RiskRewardBar
              entry={p.entry_price}
              stop={p.planned_stop_loss}
              target={p.planned_target_1}
            />
          </div>
          <span className="text-2xs text-text-muted font-mono">
            {formatDateTime(p.entry_time)}
          </span>
        </div>
      </div>
    </div>
  )
}

export default function Positions() {
  const [positions, setPositions] = useState([])
  const [loading, setLoading]     = useState(true)
  const [error, setError]         = useState(null)
  const [lastUpdated, setLastUpdated] = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await fetchPositions()
      setPositions(data)
      setLastUpdated(new Date())
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  // Real-time WS updates
  const handleWsMessage = useCallback((msg) => {
    if (msg?.type === 'positions_update') {
      setPositions(msg.data ?? [])
      setLastUpdated(new Date())
    }
  }, [])
  useWebSocket(handleWsMessage)

  const longCount  = positions.filter((p) => p.direction === 'LONG').length
  const shortCount = positions.filter((p) => p.direction === 'SHORT').length

  const updatedStr = lastUpdated
    ? lastUpdated.toLocaleTimeString('en-IN', {
        timeZone: 'Asia/Kolkata',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
      })
    : null

  return (
    <div className="p-5 space-y-5 max-w-screen-xl mx-auto animate-fade-in">

      {/* Header */}
      <div className="page-header">
        <div className="flex items-center gap-3">
          <h1 className="page-title">Live Positions</h1>
          {positions.length > 0 && (
            <div className="flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-full bg-green-trade animate-pulse" />
              <span className="text-xs text-text-muted">{positions.length} open</span>
            </div>
          )}
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

      {/* Summary stats */}
      {!loading && (
        <div className="grid grid-cols-3 gap-3">
          <div className="card p-3 text-center">
            <div className="section-label mb-1">Total Open</div>
            <div className="font-mono text-2xl font-bold text-text-primary">{positions.length}</div>
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
      )}

      {/* Error */}
      {error && (
        <div className="card px-4 py-3 bg-red-trade/5 border-red-trade/30 text-sm text-red-trade font-mono">
          {error}
        </div>
      )}

      {/* Position cards */}
      <div className="space-y-3">
        {loading ? (
          Array.from({ length: 3 }).map((_, i) => <SkeletonCard key={i} />)
        ) : positions.length === 0 ? (
          <div className="card p-12 flex flex-col items-center gap-3 text-text-muted">
            <svg className="w-12 h-12 opacity-20" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 10V7" />
            </svg>
            <span className="text-base">No open positions</span>
            <span className="text-sm text-text-muted">Bot is idle or market is closed</span>
          </div>
        ) : (
          positions.map((p, i) => (
            <PositionCard key={p.id ?? i} position={p} />
          ))
        )}
      </div>
    </div>
  )
}

import { useState } from 'react'

function ConfidencePip({ value }) {
  const color =
    value >= 80 ? 'text-green-trade' : value >= 60 ? 'text-yellow-trade' : 'text-text-muted'
  const bars = Math.round(value / 20) // 1-5 bars

  return (
    <span className={`font-mono text-xs ${color} flex items-center gap-1`}>
      <span className="flex gap-0.5">
        {[1, 2, 3, 4, 5].map((b) => (
          <span
            key={b}
            className={`w-1 h-3 rounded-sm ${b <= bars ? color.replace('text-', 'bg-') : 'bg-bg-hover'}`}
          />
        ))}
      </span>
      {value}%
    </span>
  )
}

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

function SkeletonRow() {
  return (
    <tr>
      {[60, 50, 80, 40, 70, 55].map((w, i) => (
        <td key={i} className="td">
          <div className={`skeleton h-4 w-${w === 60 ? '16' : w === 50 ? '14' : w === 80 ? '20' : w === 40 ? '10' : w === 70 ? '16' : '12'}`} />
        </td>
      ))}
    </tr>
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
    return isoString
  }
}

function EmptyState() {
  return (
    <tr>
      <td colSpan={6} className="td text-center py-12">
        <div className="flex flex-col items-center gap-2 text-text-muted">
          <svg className="w-8 h-8 opacity-40" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
          </svg>
          <span className="text-sm">No recent signals</span>
        </div>
      </td>
    </tr>
  )
}

export default function SignalsTable({ signals = [], loading = false, error = null }) {
  return (
    <div className="card overflow-hidden">
      <div className="flex items-center justify-between px-4 py-3 border-b border-border">
        <h2 className="text-sm font-semibold text-text-primary flex items-center gap-2">
          <span className="w-1.5 h-1.5 rounded-full bg-blue-trade" />
          Recent Signals
        </h2>
        <span className="badge bg-blue-muted text-blue-trade border border-blue-trade/20">
          {signals.length} signals
        </span>
      </div>

      {error && (
        <div className="px-4 py-2 bg-red-trade/5 border-b border-red-trade/20 text-xs text-red-trade font-mono">
          {error}
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full min-w-[600px]">
          <thead>
            <tr>
              <th className="th">Symbol</th>
              <th className="th">Direction</th>
              <th className="th">Signal Type</th>
              <th className="th">Confidence</th>
              <th className="th text-right">Price</th>
              <th className="th text-right">Time (IST)</th>
            </tr>
          </thead>
          <tbody>
            {loading
              ? Array.from({ length: 5 }).map((_, i) => <SkeletonRow key={i} />)
              : signals.length === 0
              ? <EmptyState />
              : signals.map((s, i) => (
                  <tr key={i} className="table-row-hover animate-fade-in">
                    <td className="td">
                      <span className="font-mono font-semibold text-text-primary text-sm">
                        {s.symbol}
                      </span>
                    </td>
                    <td className="td">
                      <DirectionBadge direction={s.direction} />
                    </td>
                    <td className="td">
                      <span className="text-xs text-text-secondary font-medium uppercase tracking-wide">
                        {s.signal_type}
                      </span>
                    </td>
                    <td className="td">
                      <ConfidencePip value={s.confidence} />
                    </td>
                    <td className="td text-right">
                      <span className="font-mono text-sm text-text-primary">
                        ₹{Number(s.price).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                      </span>
                    </td>
                    <td className="td text-right">
                      <span className="font-mono text-xs text-text-muted">
                        {formatTime(s.time)}
                      </span>
                    </td>
                  </tr>
                ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

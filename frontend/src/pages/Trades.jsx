import { useState, useEffect, useCallback } from 'react'
import { fetchTrades } from '../api'
import TradesTable from '../components/TradesTable'

const PER_PAGE = 50

function Pagination({ page, total, perPage, onPage }) {
  const totalPages = Math.max(1, Math.ceil(total / perPage))
  if (totalPages <= 1) return null

  const pages = []
  const delta = 2
  for (let i = 1; i <= totalPages; i++) {
    if (i === 1 || i === totalPages || (i >= page - delta && i <= page + delta)) {
      pages.push(i)
    } else if (pages[pages.length - 1] !== '…') {
      pages.push('…')
    }
  }

  return (
    <div className="flex items-center justify-between mt-4">
      <span className="text-xs text-text-muted font-mono">
        {((page - 1) * perPage) + 1}–{Math.min(page * perPage, total)} of {total.toLocaleString('en-IN')} trades
      </span>
      <div className="flex items-center gap-1">
        <button
          onClick={() => onPage(page - 1)}
          disabled={page <= 1}
          className="btn bg-bg-card border border-border text-text-muted hover:text-text-primary disabled:opacity-30 text-xs px-2"
        >
          ← Prev
        </button>
        {pages.map((p, i) =>
          p === '…' ? (
            <span key={`e-${i}`} className="px-2 text-text-muted text-sm">…</span>
          ) : (
            <button
              key={p}
              onClick={() => onPage(p)}
              className={`btn text-xs px-2.5 ${
                p === page
                  ? 'bg-blue-trade text-white border border-blue-trade'
                  : 'bg-bg-card border border-border text-text-muted hover:text-text-primary'
              }`}
            >
              {p}
            </button>
          )
        )}
        <button
          onClick={() => onPage(page + 1)}
          disabled={page >= totalPages}
          className="btn bg-bg-card border border-border text-text-muted hover:text-text-primary disabled:opacity-30 text-xs px-2"
        >
          Next →
        </button>
      </div>
    </div>
  )
}

function SummaryBar({ trades, loading }) {
  if (loading) {
    return (
      <div className="flex gap-4">
        {[1, 2, 3, 4].map(i => (
          <div key={i} className="skeleton h-8 w-28 rounded" />
        ))}
      </div>
    )
  }

  const closed   = trades.filter(t => t.status !== 'OPEN')
  const winners  = closed.filter(t => (t.pnl ?? 0) > 0)
  const losers   = closed.filter(t => (t.pnl ?? 0) < 0)
  const totalPnl = closed.reduce((s, t) => s + (t.pnl ?? 0), 0)
  const winRate  = closed.length ? (winners.length / closed.length) * 100 : 0

  const pnlIsPos = totalPnl >= 0
  const sign = pnlIsPos ? '+' : ''

  return (
    <div className="flex flex-wrap gap-3">
      <div className="card px-3 py-2 flex flex-col gap-0.5">
        <span className="text-2xs text-text-muted uppercase tracking-wide">Net P&L</span>
        <span className={`font-mono text-sm font-semibold ${pnlIsPos ? 'text-green-trade' : 'text-red-trade'}`}>
          {sign}₹{Math.abs(totalPnl).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
        </span>
      </div>
      <div className="card px-3 py-2 flex flex-col gap-0.5">
        <span className="text-2xs text-text-muted uppercase tracking-wide">Win Rate</span>
        <span className={`font-mono text-sm font-semibold ${winRate >= 50 ? 'text-green-trade' : 'text-red-trade'}`}>
          {winRate.toFixed(1)}%
        </span>
      </div>
      <div className="card px-3 py-2 flex flex-col gap-0.5">
        <span className="text-2xs text-text-muted uppercase tracking-wide">Winners</span>
        <span className="font-mono text-sm font-semibold text-green-trade">{winners.length}</span>
      </div>
      <div className="card px-3 py-2 flex flex-col gap-0.5">
        <span className="text-2xs text-text-muted uppercase tracking-wide">Losers</span>
        <span className="font-mono text-sm font-semibold text-red-trade">{losers.length}</span>
      </div>
      <div className="card px-3 py-2 flex flex-col gap-0.5">
        <span className="text-2xs text-text-muted uppercase tracking-wide">Open</span>
        <span className="font-mono text-sm font-semibold text-yellow-trade">
          {trades.filter(t => t.status === 'OPEN').length}
        </span>
      </div>
    </div>
  )
}

export default function Trades() {
  const [trades, setTrades]     = useState([])
  const [total, setTotal]       = useState(0)
  const [page, setPage]         = useState(1)
  const [loading, setLoading]   = useState(true)
  const [error, setError]       = useState(null)

  const load = useCallback(async (p = 1) => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetchTrades({ page: p, per_page: PER_PAGE })
      setTrades(res?.trades ?? [])
      setTotal(res?.total ?? 0)
      setPage(p)
    } catch (err) {
      setError(err.message)
      setTrades([])
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load(1) }, [load])

  const handlePage = (p) => {
    window.scrollTo({ top: 0, behavior: 'smooth' })
    load(p)
  }

  return (
    <div className="p-5 space-y-4 animate-fade-in max-w-screen-2xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h1 className="text-base font-semibold text-text-primary">Trade History</h1>
          {!loading && (
            <span className="badge bg-bg-card border border-border text-text-muted font-mono">
              {total.toLocaleString('en-IN')} total
            </span>
          )}
        </div>
        <button
          onClick={() => load(page)}
          className="btn bg-bg-card border border-border text-text-muted hover:text-text-primary text-xs"
        >
          <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          Refresh
        </button>
      </div>

      {/* Summary bar */}
      <SummaryBar trades={trades} loading={loading} />

      {/* Color legend */}
      <div className="flex items-center gap-4 text-xs text-text-muted">
        <span className="flex items-center gap-1.5">
          <span className="w-3 h-3 rounded-sm bg-green-trade/20 border-l-2 border-green-trade" />
          Profit
        </span>
        <span className="flex items-center gap-1.5">
          <span className="w-3 h-3 rounded-sm bg-red-trade/20 border-l-2 border-red-trade" />
          Loss
        </span>
        <span className="flex items-center gap-1.5">
          <span className="w-3 h-3 rounded-sm bg-yellow-trade/20 border-l-2 border-yellow-trade" />
          Open
        </span>
      </div>

      {/* Table */}
      <TradesTable trades={trades} loading={loading} error={error} />

      {/* Pagination */}
      <Pagination
        page={page}
        total={total}
        perPage={PER_PAGE}
        onPage={handlePage}
      />
    </div>
  )
}

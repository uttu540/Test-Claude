import { useState, useEffect, useCallback } from 'react'
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, ReferenceLine, Cell,
} from 'recharts'
import { fetchPnLHistory } from '../api'

// ─── Custom Tooltip ────────────────────────────────────────────────────────────

function BarTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null
  const val  = payload[0].value
  const isPos = val >= 0
  const d = payload[0].payload
  return (
    <div className="card px-3 py-2 text-xs font-mono space-y-1 min-w-[140px]">
      <div className="text-text-muted">{label}</div>
      <div className={`font-semibold ${isPos ? 'text-green-trade' : 'text-red-trade'}`}>
        {isPos ? '+' : ''}₹{Math.abs(val).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
      </div>
      {d.total_trades != null && (
        <div className="text-text-muted">
          {d.total_trades} trade{d.total_trades !== 1 ? 's' : ''}
          {d.wins != null ? ` · ${d.wins}W` : ''}
        </div>
      )}
    </div>
  )
}

// ─── Summary Card ─────────────────────────────────────────────────────────────

function SummaryCard({ label, value, sub, accent }) {
  return (
    <div className={`card px-4 py-3 border-l-2 ${accent}`}>
      <div className="text-2xs text-text-muted uppercase tracking-widest mb-1">{label}</div>
      <div className="font-mono text-base font-semibold text-text-primary">{value}</div>
      {sub && <div className="text-2xs text-text-muted mt-0.5">{sub}</div>}
    </div>
  )
}

// ─── Day Range Selector ───────────────────────────────────────────────────────

function RangeButton({ label, value, current, onClick }) {
  return (
    <button
      onClick={() => onClick(value)}
      className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
        current === value
          ? 'bg-blue-trade/10 text-blue-trade border border-blue-trade/30'
          : 'text-text-muted hover:text-text-primary border border-transparent'
      }`}
    >
      {label}
    </button>
  )
}

// ─── Main Page ────────────────────────────────────────────────────────────────

export default function PnLHistory() {
  const [rawData, setRawData] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError]     = useState(null)
  const [days, setDays]       = useState(30)

  const load = useCallback(async (d) => {
    setLoading(true)
    setError(null)
    try {
      const result = await fetchPnLHistory(d)
      // API returns newest-first; reverse for chart (oldest left)
      setRawData([...result].reverse())
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load(days) }, [load, days])

  const handleRange = (d) => {
    setDays(d)
    load(d)
  }

  // Chart data: format date labels
  const chartData = rawData.map(d => ({
    ...d,
    date:      formatDate(d.trading_date),
    net_pnl:   Number(d.net_pnl || 0),
    wins:      d.wins,
    total_trades: d.total_trades,
  }))

  // Summary stats
  const tradedDays = chartData.length
  const winDays    = chartData.filter(d => d.net_pnl > 0).length
  const totalPnl   = chartData.reduce((s, d) => s + d.net_pnl, 0)
  const bestDay    = tradedDays ? Math.max(...chartData.map(d => d.net_pnl)) : 0
  const worstDay   = tradedDays ? Math.min(...chartData.map(d => d.net_pnl)) : 0
  const winDayRate = tradedDays ? (winDays / tradedDays * 100) : 0

  const totalIsPos = totalPnl >= 0

  return (
    <div className="p-5 space-y-5 animate-fade-in max-w-screen-2xl mx-auto">

      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-3">
          <h1 className="text-base font-semibold text-text-primary">P&amp;L History</h1>
          {!loading && (
            <span className="badge bg-bg-card border border-border text-text-muted font-mono">
              {tradedDays} days traded
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          {[
            { label: '7D',  value: 7  },
            { label: '30D', value: 30 },
            { label: '60D', value: 60 },
            { label: '90D', value: 90 },
          ].map(r => (
            <RangeButton key={r.value} {...r} current={days} onClick={handleRange} />
          ))}
          <button
            onClick={() => load(days)}
            className="btn bg-bg-card border border-border text-text-muted hover:text-text-primary text-xs ml-1"
          >
            <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
            </svg>
          </button>
        </div>
      </div>

      {/* Error */}
      {error && (
        <div className="px-4 py-2 bg-red-trade/10 border border-red-trade/30 rounded-lg text-sm text-red-trade font-mono">
          {error}
        </div>
      )}

      {/* Summary Cards */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3">
        <SummaryCard
          label="Total P&L"
          value={`${totalIsPos ? '+' : ''}₹${Math.abs(totalPnl).toLocaleString('en-IN', { minimumFractionDigits: 2 })}`}
          sub={`${days}-day window`}
          accent={totalIsPos ? 'border-l-green-trade' : 'border-l-red-trade'}
        />
        <SummaryCard
          label="Win Days"
          value={`${winDays} / ${tradedDays}`}
          sub={`${winDayRate.toFixed(0)}% day win rate`}
          accent={winDayRate >= 50 ? 'border-l-green-trade' : 'border-l-red-trade'}
        />
        <SummaryCard
          label="Best Day"
          value={`+₹${Math.max(bestDay, 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}`}
          accent="border-l-green-trade"
        />
        <SummaryCard
          label="Worst Day"
          value={`₹${Math.min(worstDay, 0).toLocaleString('en-IN', { minimumFractionDigits: 2 })}`}
          accent="border-l-red-trade"
        />
        <SummaryCard
          label="Avg / Day"
          value={`${(totalIsPos ? '+' : '')}₹${tradedDays ? Math.abs(totalPnl / tradedDays).toLocaleString('en-IN', { minimumFractionDigits: 2 }) : '0.00'}`}
          accent="border-l-border"
        />
      </div>

      {/* Bar Chart */}
      <div className="card p-4">
        <div className="flex items-center justify-between mb-4">
          <span className="text-xs text-text-muted uppercase tracking-widest font-medium">
            Daily Net P&amp;L
          </span>
          <span className="text-xs text-text-muted">
            <span className="inline-block w-2.5 h-2.5 rounded-sm bg-green-trade/60 mr-1" />Profit
            <span className="inline-block w-2.5 h-2.5 rounded-sm bg-red-trade/60 ml-3 mr-1" />Loss
          </span>
        </div>

        {loading ? (
          <div className="h-64 flex items-center justify-center">
            <div className="skeleton w-full h-full rounded" />
          </div>
        ) : chartData.length === 0 ? (
          <div className="h-64 flex flex-col items-center justify-center gap-3 text-text-muted">
            <svg className="w-10 h-10 opacity-30" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
            </svg>
            <span className="text-sm">No P&amp;L data for this window</span>
            <span className="text-xs text-text-muted">Trades will appear here once closed</span>
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={280}>
            <BarChart data={chartData} margin={{ top: 4, right: 4, bottom: 4, left: 0 }} barCategoryGap="30%">
              <CartesianGrid stroke="#1f1f1f" strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="date"
                tick={{ fontSize: 10, fill: '#475569', fontFamily: 'JetBrains Mono' }}
                axisLine={false}
                tickLine={false}
                interval="preserveStartEnd"
              />
              <YAxis
                tick={{ fontSize: 10, fill: '#475569', fontFamily: 'JetBrains Mono' }}
                axisLine={false}
                tickLine={false}
                tickFormatter={(v) => `₹${v >= 0 ? '' : '-'}${Math.abs(v).toLocaleString('en-IN')}`}
                width={72}
              />
              <Tooltip content={<BarTooltip />} cursor={{ fill: 'rgba(255,255,255,0.03)' }} />
              <ReferenceLine y={0} stroke="#2a2a2a" strokeWidth={1} />
              <Bar dataKey="net_pnl" radius={[3, 3, 0, 0]}>
                {chartData.map((entry, i) => (
                  <Cell
                    key={i}
                    fill={entry.net_pnl >= 0 ? 'rgba(34,197,94,0.7)' : 'rgba(239,68,68,0.7)'}
                  />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>

      {/* Daily Breakdown Table */}
      {!loading && chartData.length > 0 && (
        <div className="card overflow-hidden">
          <div className="px-4 py-3 border-b border-border">
            <span className="text-xs text-text-muted uppercase tracking-widest font-medium">Daily Breakdown</span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr>
                  <th className="th">Date</th>
                  <th className="th text-right">Trades</th>
                  <th className="th text-right">Wins</th>
                  <th className="th text-right">Win Rate</th>
                  <th className="th text-right">Net P&amp;L</th>
                </tr>
              </thead>
              <tbody>
                {[...chartData].reverse().map((row, i) => {
                  const isPos = row.net_pnl >= 0
                  const wr = row.total_trades ? Math.round(row.wins / row.total_trades * 100) : 0
                  return (
                    <tr key={i} className="table-row-hover">
                      <td className="td font-mono text-xs text-text-secondary">{row.date}</td>
                      <td className="td text-right font-mono text-sm">{row.total_trades ?? '—'}</td>
                      <td className="td text-right font-mono text-sm text-green-trade">{row.wins ?? '—'}</td>
                      <td className="td text-right font-mono text-sm">
                        <span className={wr >= 50 ? 'text-green-trade' : 'text-red-trade'}>
                          {row.total_trades ? `${wr}%` : '—'}
                        </span>
                      </td>
                      <td className="td text-right">
                        <span className={`font-mono text-sm font-semibold ${isPos ? 'text-green-trade' : 'text-red-trade'}`}>
                          {isPos ? '+' : ''}₹{Math.abs(row.net_pnl).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
                        </span>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}

function formatDate(iso) {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    return d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', timeZone: 'Asia/Kolkata' })
  } catch {
    return iso
  }
}

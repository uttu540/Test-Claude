import { useState, useEffect, useCallback } from 'react'
import {
  AreaChart, Area, XAxis, YAxis, Tooltip,
  ResponsiveContainer, ReferenceLine, CartesianGrid,
} from 'recharts'
import { fetchPnLToday, fetchPositions, fetchRecentSignals } from '../api'
import { useWebSocket } from '../ws'
import StatCard from '../components/StatCard'
import PnLBar from '../components/PnLBar'
import SignalsTable from '../components/SignalsTable'
import PositionsTable from '../components/PositionsTable'

// ─── Regime badge ─────────────────────────────────────────────────────────────

function RegimeBadge({ regime, loading }) {
  if (loading) return <div className="skeleton h-7 w-28 rounded-full" />

  const configs = {
    TRENDING: { bg: 'bg-blue-trade/10', text: 'text-blue-trade', border: 'border-blue-trade/30', icon: '⟳' },
    RANGING:  { bg: 'bg-yellow-trade/10', text: 'text-yellow-trade', border: 'border-yellow-trade/30', icon: '⟷' },
    UNKNOWN:  { bg: 'bg-text-muted/10', text: 'text-text-muted', border: 'border-border', icon: '?' },
  }
  const c = configs[regime] || configs.UNKNOWN

  return (
    <div className="flex items-center gap-2">
      <span className="text-xs text-text-muted">Market Regime</span>
      <span className={`badge ${c.bg} ${c.text} border ${c.border} text-xs`}>
        {regime || 'UNKNOWN'}
      </span>
    </div>
  )
}

// ─── Custom sparkline tooltip ─────────────────────────────────────────────────

function SparklineTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null
  const val = payload[0].value
  const isPos = val >= 0
  return (
    <div className="card px-2.5 py-1.5 text-xs font-mono">
      <div className="text-text-muted mb-0.5">{label}</div>
      <div className={isPos ? 'text-green-trade' : 'text-red-trade'}>
        {isPos ? '+' : ''}₹{Math.abs(val).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
      </div>
    </div>
  )
}

// ─── P&L Sparkline ───────────────────────────────────────────────────────────

function PnLSparkline({ data = [], loading }) {
  if (loading) {
    return (
      <div className="card p-4 h-[180px] flex items-center justify-center">
        <div className="skeleton w-full h-full rounded" />
      </div>
    )
  }

  if (!data || data.length < 2) {
    return (
      <div className="card p-4 h-[180px] flex flex-col items-center justify-center gap-2 text-text-muted">
        <svg className="w-8 h-8 opacity-30" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M7 12l3-3 3 3 4-4M8 21l4-4 4 4M3 4h18M4 4h16v12a1 1 0 01-1 1H5a1 1 0 01-1-1V4z" />
        </svg>
        <span className="text-sm">No chart data yet</span>
      </div>
    )
  }

  const lastVal = data[data.length - 1]?.pnl ?? 0
  const isPos = lastVal >= 0
  const strokeColor = isPos ? '#22c55e' : '#ef4444'
  const fillColor   = isPos ? 'url(#greenGradient)' : 'url(#redGradient)'

  return (
    <div className="card p-4">
      <div className="flex items-center justify-between mb-3">
        <span className="text-xs text-text-muted uppercase tracking-widest font-medium">Intraday P&L</span>
        <span className={`font-mono text-sm font-semibold ${isPos ? 'text-green-trade' : 'text-red-trade'}`}>
          {isPos ? '+' : ''}₹{Math.abs(lastVal).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
        </span>
      </div>
      <ResponsiveContainer width="100%" height={120}>
        <AreaChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id="greenGradient" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#22c55e" stopOpacity={0.25} />
              <stop offset="100%" stopColor="#22c55e" stopOpacity={0.02} />
            </linearGradient>
            <linearGradient id="redGradient" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#ef4444" stopOpacity={0.25} />
              <stop offset="100%" stopColor="#ef4444" stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke="#1f1f1f" strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="time"
            tick={{ fontSize: 10, fill: '#475569', fontFamily: 'JetBrains Mono' }}
            axisLine={false}
            tickLine={false}
            interval="preserveStartEnd"
          />
          <YAxis
            tick={{ fontSize: 10, fill: '#475569', fontFamily: 'JetBrains Mono' }}
            axisLine={false}
            tickLine={false}
            tickFormatter={(v) => `₹${v.toLocaleString('en-IN')}`}
            width={60}
          />
          <Tooltip content={<SparklineTooltip />} />
          <ReferenceLine y={0} stroke="#2a2a2a" strokeDasharray="4 4" />
          <Area
            type="monotone"
            dataKey="pnl"
            stroke={strokeColor}
            strokeWidth={2}
            fill={fillColor}
            dot={false}
            activeDot={{ r: 3, fill: strokeColor, stroke: '#0f0f0f', strokeWidth: 2 }}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

// ─── Main Dashboard ───────────────────────────────────────────────────────────

export default function Dashboard() {
  const [pnlData, setPnlData]         = useState(null)
  const [positions, setPositions]     = useState([])
  const [signals, setSignals]         = useState([])

  const [pnlLoading, setPnlLoading]           = useState(true)
  const [posLoading, setPosLoading]           = useState(true)
  const [sigLoading, setSigLoading]           = useState(true)

  const [pnlError, setPnlError]               = useState(null)
  const [posError, setPosError]               = useState(null)
  const [sigError, setSigError]               = useState(null)

  // Initial fetch
  const loadAll = useCallback(async () => {
    setPnlLoading(true)
    setPosLoading(true)
    setSigLoading(true)

    fetchPnLToday()
      .then(setPnlData)
      .catch((e) => setPnlError(e.message))
      .finally(() => setPnlLoading(false))

    fetchPositions()
      .then(setPositions)
      .catch((e) => setPosError(e.message))
      .finally(() => setPosLoading(false))

    fetchRecentSignals()
      .then(setSignals)
      .catch((e) => setSigError(e.message))
      .finally(() => setSigLoading(false))
  }, [])

  useEffect(() => { loadAll() }, [loadAll])

  // WebSocket live updates
  const handleWsMessage = useCallback((msg) => {
    if (!msg?.type) return
    switch (msg.type) {
      case 'positions_update':
        setPositions(msg.data ?? [])
        break
      case 'signal':
        setSignals((prev) => [msg.data, ...prev].slice(0, 20))
        break
      case 'pnl_update':
        setPnlData((prev) => prev ? { ...prev, ...msg.data } : msg.data)
        break
      default:
        break
    }
  }, [])

  useWebSocket(handleWsMessage) // wsStatus not needed on this page

  // Formatted stats
  const netPnl     = pnlData?.net_pnl ?? 0
  const winRate    = pnlData?.win_rate ?? 0
  const openCount  = positions.length
  const tradesQty  = pnlData?.trades_today ?? 0
  const lossUsed   = pnlData?.daily_loss_used ?? 0
  const lossLimit  = pnlData?.daily_loss_limit ?? 2000
  const regime     = pnlData?.market_regime ?? 'UNKNOWN'
  const pnlSeries  = pnlData?.pnl_series ?? []

  const pnlTrend   = netPnl > 0 ? 'up' : netPnl < 0 ? 'down' : 'neutral'
  const pnlSign    = netPnl >= 0 ? '+' : ''

  return (
    <div className="p-5 space-y-5 animate-fade-in max-w-screen-2xl mx-auto">

      {/* ── Top bar: regime + refresh ── */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <h1 className="text-base font-semibold text-text-primary">Dashboard</h1>
          <RegimeBadge regime={regime} loading={pnlLoading} />
        </div>
        <button
          onClick={loadAll}
          className="btn bg-bg-card border border-border text-text-muted hover:text-text-primary text-xs"
        >
          <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          Refresh
        </button>
      </div>

      {/* ── Stat cards ── */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <StatCard
          label="Today's Net P&L"
          value={`${pnlSign}₹${Math.abs(netPnl).toLocaleString('en-IN', { minimumFractionDigits: 2 })}`}
          trend={pnlTrend}
          loading={pnlLoading}
          sub={pnlError}
          accent={pnlTrend === 'up' ? 'border-l-green-trade' : pnlTrend === 'down' ? 'border-l-red-trade' : 'border-l-border'}
          icon={
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v12m-3-2.818l.879.659c1.171.879 3.07.879 4.242 0 1.172-.879 1.172-2.303 0-3.182C13.536 12.219 12.768 12 12 12c-.725 0-1.45-.22-2.003-.659-1.106-.879-1.106-2.303 0-3.182s2.9-.879 4.006 0l.415.33M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          }
        />
        <StatCard
          label="Win Rate"
          value={`${winRate.toFixed(1)}%`}
          trend={winRate >= 50 ? 'up' : 'down'}
          loading={pnlLoading}
          sub={`${tradesQty} trades today`}
          icon={
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M16.5 18.75h-9m9 0a3 3 0 013 3h-15a3 3 0 013-3m9 0v-3.375c0-.621-.503-1.125-1.125-1.125h-.871M7.5 18.75v-3.375c0-.621.504-1.125 1.125-1.125h.872m5.007 0H9.497m5.007 0a7.454 7.454 0 01-.982-3.172M9.497 14.25a7.454 7.454 0 00.981-3.172M5.25 4.236c-.982.143-1.954.317-2.916.52A6.003 6.003 0 007.73 9.728M5.25 4.236V4.5c0 2.108.966 3.99 2.48 5.228M5.25 4.236V2.721C7.456 2.41 9.71 2.25 12 2.25c2.291 0 4.545.16 6.75.47v1.516M7.73 9.728a6.726 6.726 0 002.748 1.35m8.272-6.842V4.5c0 2.108-.966 3.99-2.48 5.228m2.48-5.492a46.32 46.32 0 012.916.52 6.003 6.003 0 01-5.395 4.972m0 0a6.726 6.726 0 01-2.749 1.35m0 0a6.772 6.772 0 01-3.044 0" />
            </svg>
          }
        />
        <StatCard
          label="Open Positions"
          value={openCount}
          trend="neutral"
          loading={posLoading}
          sub={posError || 'Live positions'}
          icon={
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 10V7" />
            </svg>
          }
        />
        <StatCard
          label="Trades Today"
          value={tradesQty}
          trend="neutral"
          loading={pnlLoading}
          sub="Completed executions"
          icon={
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M3 7.5L7.5 3m0 0L12 7.5M7.5 3v13.5m13.5 0L16.5 21m0 0L12 16.5m4.5 4.5V7.5" />
            </svg>
          }
        />
      </div>

      {/* ── Daily loss bar + sparkline ── */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
        <div className="lg:col-span-2">
          <PnLBar
            used={lossUsed}
            limit={lossLimit}
            loading={pnlLoading}
          />
        </div>
        <div>
          <PnLSparkline data={pnlSeries} loading={pnlLoading} />
        </div>
      </div>

      {/* ── Tables ── */}
      <SignalsTable
        signals={signals}
        loading={sigLoading}
        error={sigError}
      />

      <PositionsTable
        positions={positions}
        loading={posLoading}
        error={posError}
      />
    </div>
  )
}

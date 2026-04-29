import { useState, useEffect, useCallback } from 'react'
import {
  AreaChart, Area, XAxis, YAxis, Tooltip,
  ResponsiveContainer, ReferenceLine, CartesianGrid,
} from 'recharts'
import { fetchPnLToday, fetchPositions, fetchRecentSignals, fetchBotStatus } from '../api'
import { useWebSocket } from '../ws'
import SignalsTable from '../components/SignalsTable'
import PositionsTable from '../components/PositionsTable'

// ─── Retro Fintech palette ────────────────────────────────────────────────────

const C = {
  bg:          '#0d0b07',
  card:        '#141009',
  cardHover:   '#1c160d',
  border:      '#2c2314',
  borderFaint: '#1e180e',
  amber:       '#c9952a',
  amberDim:    '#7a5818',
  amberGlow:   'rgba(201,149,42,0.18)',
  text:        '#e4cfa0',
  textSub:     '#8a7448',
  textMuted:   '#4e4228',
  green:       '#78b050',
  greenDim:    '#3d5a28',
  red:         '#c05858',
  redDim:      '#5a2828',
  orange:      '#d07830',
}

const REGIME = {
  TRENDING_UP:     { label: 'TRENDING ↑', color: C.green  },
  TRENDING_DOWN:   { label: 'TRENDING ↓', color: C.red    },
  RANGING:         { label: 'RANGING',    color: C.amber  },
  HIGH_VOLATILITY: { label: 'HIGH VOL',   color: C.orange },
  UNKNOWN:         { label: 'UNKNOWN',    color: C.textMuted },
}

const MODE = {
  LIVE:      { label: 'LIVE',  color: C.green  },
  PAPER:     { label: 'PAPER', color: C.amber  },
  SEMI_AUTO: { label: 'SEMI',  color: C.orange },
  DEV:       { label: 'DEV',   color: C.textSub },
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function inr(n, dec = 2) {
  return Math.abs(n).toLocaleString('en-IN', {
    minimumFractionDigits: dec,
    maximumFractionDigits: dec,
  })
}

function nowIST() {
  return new Date().toLocaleTimeString('en-IN', {
    timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hour12: false,
  })
}

// ─── Shared style shorthands ──────────────────────────────────────────────────

const MONO = { fontFamily: "'JetBrains Mono', monospace" }
const SERIF = { fontFamily: "'Playfair Display', Georgia, serif" }
const SANS = { fontFamily: "'Inter', system-ui, sans-serif" }
const LABEL = { ...SANS, fontSize: 10, letterSpacing: '0.12em', textTransform: 'uppercase', fontWeight: 600, color: C.textSub }

// ─── Live clock ───────────────────────────────────────────────────────────────

function LiveClock() {
  const [t, setT] = useState(nowIST)
  useEffect(() => {
    const id = setInterval(() => setT(nowIST()), 15000)
    return () => clearInterval(id)
  }, [])
  return <span style={{ ...MONO, fontSize: 11, color: C.textSub, letterSpacing: '0.08em' }}>{t} IST</span>
}

// ─── Inline indicator tag ─────────────────────────────────────────────────────

function Tag({ label, color, loading }) {
  if (loading) {
    return <span style={{ display: 'inline-block', width: 72, height: 14, background: C.cardHover, borderRadius: 1 }} />
  }
  return (
    <span style={{
      ...MONO, fontSize: 11, fontWeight: 600, letterSpacing: '0.1em',
      color, borderLeft: `2px solid ${color}`, paddingLeft: 8,
    }}>
      {label}
    </span>
  )
}

// ─── Sparkline tooltip ────────────────────────────────────────────────────────

function RetroTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null
  const v = payload[0].value
  const pos = v >= 0
  return (
    <div style={{
      background: C.card, border: `1px solid ${C.border}`,
      borderLeft: `3px solid ${pos ? C.green : C.red}`,
      padding: '8px 14px', ...MONO, fontSize: 11,
    }}>
      <div style={{ color: C.textSub, marginBottom: 4 }}>{label}</div>
      <div style={{ color: pos ? C.green : C.red, fontWeight: 600 }}>
        {pos ? '+' : '−'}₹{inr(Math.abs(v))}
      </div>
    </div>
  )
}

// ─── P&L Sparkline ───────────────────────────────────────────────────────────

function PnLSparkline({ data, loading, isPos }) {
  const stroke = isPos ? C.green : C.red
  const fillId = isPos ? 'db-green' : 'db-red'

  if (loading) {
    return (
      <div style={{ background: C.card, border: `1px solid ${C.border}`, padding: 20, height: 200 }}>
        <div style={{ background: C.cardHover, height: '100%', borderRadius: 1 }} />
      </div>
    )
  }

  return (
    <div style={{ background: C.card, border: `1px solid ${C.border}`, padding: '16px 12px 8px 0' }}>
      {!data || data.length < 2 ? (
        <div style={{
          height: 160, display: 'flex', alignItems: 'center', justifyContent: 'center',
          ...MONO, fontSize: 12, color: C.textMuted, letterSpacing: '0.06em',
        }}>
          — awaiting intraday data —
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={160}>
          <AreaChart data={data} margin={{ top: 6, right: 16, bottom: 0, left: 4 }}>
            <defs>
              <linearGradient id="db-green" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={C.green} stopOpacity={0.2} />
                <stop offset="100%" stopColor={C.green} stopOpacity={0.01} />
              </linearGradient>
              <linearGradient id="db-red" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={C.red} stopOpacity={0.2} />
                <stop offset="100%" stopColor={C.red} stopOpacity={0.01} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke={C.borderFaint} strokeDasharray="2 5" vertical={false} />
            <XAxis dataKey="time" tick={{ ...MONO, fontSize: 9, fill: C.textMuted }} axisLine={false} tickLine={false} interval="preserveStartEnd" />
            <YAxis tick={{ ...MONO, fontSize: 9, fill: C.textMuted }} axisLine={false} tickLine={false} tickFormatter={v => `₹${(v / 1000).toFixed(0)}k`} width={42} />
            <Tooltip content={<RetroTooltip />} />
            <ReferenceLine y={0} stroke={C.border} strokeDasharray="3 5" />
            <Area type="monotone" dataKey="pnl" stroke={stroke} strokeWidth={1.5} fill={`url(#${fillId})`} dot={false} activeDot={{ r: 3, fill: stroke, stroke: C.card, strokeWidth: 2 }} />
          </AreaChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}

// ─── Stat card ────────────────────────────────────────────────────────────────

function StatCard({ label, value, sub, topColor, loading }) {
  return (
    <div style={{
      background: C.card, border: `1px solid ${C.border}`,
      borderTop: `2px solid ${topColor || C.amberDim}`,
      padding: '18px 20px',
    }}>
      {loading ? (
        <>
          <div style={{ background: C.cardHover, height: 9, width: '55%', marginBottom: 10, borderRadius: 1 }} />
          <div style={{ background: C.cardHover, height: 24, width: '70%', borderRadius: 1 }} />
        </>
      ) : (
        <>
          <div style={{ ...LABEL, marginBottom: 10 }}>{label}</div>
          <div style={{ ...MONO, fontSize: 24, fontWeight: 600, color: topColor || C.text, lineHeight: 1 }}>
            {value}
          </div>
          {sub && <div style={{ ...SANS, fontSize: 11, color: C.textMuted, marginTop: 6 }}>{sub}</div>}
        </>
      )}
    </div>
  )
}

// ─── Loss gauge ───────────────────────────────────────────────────────────────

function LossGauge({ used, limit, loading }) {
  const pct = limit > 0 ? Math.min((used / limit) * 100, 100) : 0
  const barColor = pct >= 80 ? C.red : pct >= 50 ? C.orange : C.amber

  return (
    <div style={{
      background: C.card, border: `1px solid ${C.border}`,
      borderTop: `2px solid ${C.amberDim}`,
      padding: '18px 20px', display: 'flex', flexDirection: 'column', justifyContent: 'space-between',
    }}>
      <div style={{ ...LABEL, marginBottom: 16 }}>Daily Risk Consumed</div>
      {loading ? (
        <div style={{ background: C.cardHover, height: 60, borderRadius: 1 }} />
      ) : (
        <>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 14 }}>
            <span style={{ ...MONO, fontSize: 26, fontWeight: 600, color: barColor }}>{pct.toFixed(1)}%</span>
            <span style={{ ...MONO, fontSize: 11, color: C.textMuted }}>₹{inr(used, 0)} / ₹{inr(limit, 0)}</span>
          </div>
          <div style={{ height: 3, background: C.cardHover, position: 'relative', overflow: 'hidden' }}>
            {[25, 50, 75].map(t => (
              <div key={t} style={{ position: 'absolute', left: `${t}%`, top: 0, bottom: 0, width: 1, background: C.border, zIndex: 1 }} />
            ))}
            <div style={{ height: '100%', width: `${pct}%`, background: barColor, transition: 'width 0.5s ease' }} />
          </div>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 5 }}>
            {['0', '25%', '50%', '75%', '100%'].map(t => (
              <span key={t} style={{ ...MONO, fontSize: 9, color: C.textMuted }}>{t}</span>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

// ─── Section divider ──────────────────────────────────────────────────────────

function Divider({ title }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 14, margin: '28px 0 14px' }}>
      <span style={{ ...SERIF, fontSize: 13, color: C.textSub, letterSpacing: '0.04em', whiteSpace: 'nowrap' }}>{title}</span>
      <div style={{ flex: 1, height: 1, background: C.borderFaint }} />
    </div>
  )
}

// ─── Main Dashboard ───────────────────────────────────────────────────────────

export default function Dashboard() {
  const [pnlData, setPnlData]       = useState(null)
  const [positions, setPositions]   = useState([])
  const [signals, setSignals]       = useState([])
  const [botStatus, setBotStatus]   = useState(null)

  const [pnlLoading, setPnlLoading]       = useState(true)
  const [posLoading, setPosLoading]       = useState(true)
  const [sigLoading, setSigLoading]       = useState(true)
  const [statusLoading, setStatusLoading] = useState(true)

  const [posError, setPosError] = useState(null)
  const [sigError, setSigError] = useState(null)

  const loadAll = useCallback(async () => {
    setPnlLoading(true); setPosLoading(true); setSigLoading(true); setStatusLoading(true)
    fetchPnLToday().then(setPnlData).catch(() => {}).finally(() => setPnlLoading(false))
    fetchPositions().then(setPositions).catch(e => setPosError(e.message)).finally(() => setPosLoading(false))
    fetchRecentSignals().then(setSignals).catch(e => setSigError(e.message)).finally(() => setSigLoading(false))
    fetchBotStatus().then(setBotStatus).catch(() => {}).finally(() => setStatusLoading(false))
  }, [])

  useEffect(() => { loadAll() }, [loadAll])

  const handleWsMessage = useCallback((msg) => {
    if (!msg?.type) return
    if (msg.type === 'positions_update') setPositions(msg.data ?? [])
    if (msg.type === 'signal') setSignals(prev => [msg.data, ...prev].slice(0, 20))
    if (msg.type === 'pnl_update') setPnlData(prev => prev ? { ...prev, ...msg.data } : msg.data)
  }, [])
  useWebSocket(handleWsMessage)

  const netPnl    = pnlData?.net_pnl ?? 0
  const winRate   = pnlData?.win_rate ?? 0
  const tradesQty = pnlData?.trades_today ?? 0
  const lossUsed  = pnlData?.daily_loss_used ?? 0
  const lossLimit = pnlData?.daily_loss_limit ?? 2000
  const regime    = pnlData?.market_regime ?? 'UNKNOWN'
  const pnlSeries = pnlData?.pnl_series ?? []
  const isPos     = netPnl >= 0

  const regimeCfg = REGIME[regime] || REGIME.UNKNOWN
  const modeCfg   = MODE[botStatus?.mode] || MODE.DEV
  const pnlColor  = isPos ? C.green : C.red

  return (
    <div style={{ background: C.bg, minHeight: '100vh', color: C.text }}>

      {/* ── Masthead ──────────────────────────────────────────────────────── */}
      <div style={{
        borderBottom: `1px solid ${C.border}`,
        display: 'flex', alignItems: 'stretch', height: 50, padding: '0 28px',
      }}>
        {/* Wordmark */}
        <div style={{
          display: 'flex', alignItems: 'center', paddingRight: 24,
          borderRight: `1px solid ${C.border}`,
        }}>
          <span style={{ ...SERIF, fontSize: 14, fontWeight: 700, color: C.amber, letterSpacing: '0.05em' }}>
            TRADEBOT
          </span>
        </div>

        {/* Status tags */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 20, paddingLeft: 24, flex: 1 }}>
          <Tag label={modeCfg.label} color={modeCfg.color} loading={statusLoading} />
          <Tag label={regimeCfg.label} color={regimeCfg.color} loading={pnlLoading} />
        </div>

        {/* Clock + refresh */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          <LiveClock />
          <button
            onClick={loadAll}
            style={{
              background: 'none', border: `1px solid ${C.border}`, cursor: 'pointer',
              ...SANS, fontSize: 10, letterSpacing: '0.1em', color: C.textSub,
              padding: '4px 14px', transition: 'border-color 0.15s, color 0.15s',
            }}
            onMouseEnter={e => { e.currentTarget.style.borderColor = C.amber; e.currentTarget.style.color = C.amber }}
            onMouseLeave={e => { e.currentTarget.style.borderColor = C.border; e.currentTarget.style.color = C.textSub }}
          >
            REFRESH
          </button>
        </div>
      </div>

      {/* ── Body ──────────────────────────────────────────────────────────── */}
      <div style={{ padding: '32px 28px 56px', maxWidth: 1440, margin: '0 auto' }}>

        {/* ── Hero P&L ── */}
        <div style={{ marginBottom: 28 }}>
          <div style={{ ...LABEL, marginBottom: 10 }}>Today's Net P&L</div>
          {pnlLoading ? (
            <div style={{ background: C.cardHover, height: 60, width: 300, borderRadius: 1 }} />
          ) : (
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 20 }}>
              <span style={{
                ...SERIF, fontSize: 56, fontWeight: 700, color: pnlColor, lineHeight: 1,
                textShadow: `0 0 48px ${isPos ? 'rgba(120,176,80,0.22)' : 'rgba(192,88,88,0.22)'}`,
              }}>
                {isPos ? '+' : '−'}₹{inr(Math.abs(netPnl))}
              </span>
              <span style={{ ...MONO, fontSize: 12, color: pnlColor, opacity: 0.65 }}>
                {tradesQty} trades · {winRate.toFixed(1)}% W/R
              </span>
            </div>
          )}
          <div style={{ height: 1, marginTop: 24, background: `linear-gradient(to right, ${C.border}, transparent)` }} />
        </div>

        {/* ── Sparkline + Loss gauge ── */}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 300px', gap: 14, marginBottom: 14 }}>
          <PnLSparkline data={pnlSeries} loading={pnlLoading} isPos={isPos} />
          <LossGauge used={lossUsed} limit={lossLimit} loading={pnlLoading} />
        </div>

        {/* ── Stat cards ── */}
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 14, marginBottom: 4 }}>
          <StatCard
            label="Win Rate"
            value={`${winRate.toFixed(1)}%`}
            sub={`${tradesQty} trades today`}
            topColor={winRate >= 50 ? C.green : C.red}
            loading={pnlLoading}
          />
          <StatCard
            label="Open Positions"
            value={positions.length}
            sub="live"
            topColor={C.amber}
            loading={posLoading}
          />
          <StatCard
            label="Trades Today"
            value={tradesQty}
            sub="completed"
            topColor={C.amberDim}
            loading={pnlLoading}
          />
          <StatCard
            label="Capital"
            value={botStatus?.capital ? `₹${(botStatus.capital / 1000).toFixed(0)}k` : '—'}
            sub={`loss limit ₹${(lossLimit / 1000).toFixed(0)}k / day`}
            topColor={C.textSub}
            loading={statusLoading}
          />
        </div>

        {/* ── Signals ── */}
        <Divider title="Signals" />
        <SignalsTable signals={signals} loading={sigLoading} error={sigError} />

        {/* ── Positions ── */}
        <Divider title="Positions" />
        <PositionsTable positions={positions} loading={posLoading} error={posError} />
      </div>
    </div>
  )
}

import { useState, useEffect, useCallback } from 'react'
import { fetchConfig, updateConfig, fetchConfigSchema } from '../api'

// ─── Helpers ──────────────────────────────────────────────────────────────────

function groupBy(schema) {
  const groups = {}
  for (const [key, meta] of Object.entries(schema)) {
    const g = meta.group || 'other'
    if (!groups[g]) groups[g] = []
    groups[g].push({ key, ...meta })
  }
  return groups
}

const GROUP_META = {
  execution:      { label: 'Execution',              desc: 'Confidence thresholds that gate signal-to-trade conversion.' },
  strategies:     { label: 'Strategies',             desc: 'Enable or disable individual signal strategies.' },
  indicators:     { label: 'Indicator Parameters',   desc: 'Periods and multipliers used when calculating technical indicators.' },
  timeframes:     { label: 'Timeframe Weights',      desc: 'How much each timeframe contributes to confluence scoring (higher = more weight).' },
  regime_caps:    { label: 'Regime Confidence Caps', desc: 'Maximum confidence a signal can have in each market regime.' },
  regime_signals: { label: 'Regime Signal Filters',  desc: 'Which signal types are allowed to fire in each market regime.' },
}

// ─── Toggle ───────────────────────────────────────────────────────────────────

function Toggle({ value, onChange }) {
  return (
    <button
      type="button"
      onClick={() => onChange(!value)}
      className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border-2 transition-colors ${
        value ? 'bg-blue-trade border-blue-trade' : 'bg-bg-hover border-border'
      }`}
    >
      <span
        className={`inline-block h-3 w-3 rounded-full bg-white shadow-sm transition-transform ${
          value ? 'translate-x-4' : 'translate-x-0.5'
        }`}
      />
    </button>
  )
}

// ─── Number input ─────────────────────────────────────────────────────────────

function NumberInput({ value, onChange, min, max, step, type }) {
  const [local, setLocal] = useState(String(value))

  useEffect(() => { setLocal(String(value)) }, [value])

  const commit = () => {
    const parsed = type === 'float' ? parseFloat(local) : parseInt(local, 10)
    if (isNaN(parsed)) { setLocal(String(value)); return }
    const clamped = Math.min(max, Math.max(min, parsed))
    setLocal(String(clamped))
    onChange(clamped)
  }

  return (
    <input
      type="number"
      min={min}
      max={max}
      step={step}
      value={local}
      onChange={(e) => setLocal(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => e.key === 'Enter' && commit()}
      className="w-24 bg-bg-hover border border-border rounded px-2 py-1 text-xs font-mono text-text-primary focus:outline-none focus:border-blue-trade text-right"
    />
  )
}

// ─── Signal pill list ─────────────────────────────────────────────────────────

const ALL_SIGNAL_TYPES = [
  // Breakout
  'BREAKOUT_HIGH', 'BREAKOUT_LOW',
  // Trend
  'EMA_CROSSOVER_UP', 'EMA_CROSSOVER_DOWN',
  'ABOVE_200_EMA', 'BELOW_200_EMA',
  // Momentum
  'RSI_OVERSOLD', 'RSI_OVERBOUGHT',
  'MACD_CROSS_UP', 'MACD_CROSS_DOWN',
  // Volume / Volatility
  'HIGH_RVOL',
  'BB_SQUEEZE', 'BB_EXPANSION',
  // Indian market
  'ORB_BREAKOUT',
  'VWAP_RECLAIM',
  // Candlestick patterns
  'HAMMER', 'SHOOTING_STAR',
  'ENGULFING_BULL', 'ENGULFING_BEAR',
  'MORNING_STAR', 'EVENING_STAR',
  // Chart patterns
  'DOUBLE_BOTTOM', 'DOUBLE_TOP',
  'BULL_FLAG', 'BEAR_FLAG',
  'DARVAS_BREAKOUT', 'NR7_SETUP',
  // Momentum engine (TRENDING_UP only)
  'BREAKOUT_52W', 'VOLUME_THRUST', 'EMA_RIBBON', 'BULL_MOMENTUM',
]

function SignalPills({ value, onChange }) {
  const active = new Set(value ? value.split(',').map((s) => s.trim()).filter(Boolean) : [])

  const toggle = (sig) => {
    const next = new Set(active)
    next.has(sig) ? next.delete(sig) : next.add(sig)
    onChange([...next].join(','))
  }

  return (
    <div className="flex flex-wrap gap-1.5 mt-2">
      {ALL_SIGNAL_TYPES.map((sig) => (
        <button
          key={sig}
          type="button"
          onClick={() => toggle(sig)}
          className={`px-2 py-0.5 rounded text-2xs font-mono border transition-colors ${
            active.has(sig)
              ? 'bg-blue-trade/20 text-blue-trade border-blue-trade/40'
              : 'bg-bg-hover text-text-muted border-border hover:border-border/80'
          }`}
        >
          {sig}
        </button>
      ))}
    </div>
  )
}

// ─── Config row ───────────────────────────────────────────────────────────────

function ConfigRow({ meta, value, onChange }) {
  const { key, label, desc, type, min, max, step, default: def } = meta
  const isDirty = value !== def

  return (
    <div className="flex items-start justify-between gap-4 py-3 border-b border-border/40 last:border-0">
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-sm text-text-primary font-medium">{label}</span>
          {isDirty && (
            <span className="text-2xs text-yellow-trade bg-yellow-trade/10 border border-yellow-trade/20 px-1.5 py-0.5 rounded font-mono">
              modified
            </span>
          )}
        </div>
        {desc && <p className="text-xs text-text-muted mt-0.5 leading-relaxed">{desc}</p>}
      </div>
      <div className="shrink-0">
        {type === 'bool' ? (
          <Toggle value={value} onChange={onChange} />
        ) : (
          <NumberInput value={value} onChange={onChange} min={min} max={max} step={step} type={type} />
        )}
      </div>
    </div>
  )
}

// ─── Regime signal section ────────────────────────────────────────────────────

function RegimeSignalRow({ meta, value, onChange }) {
  const { label, default: def } = meta
  const isDirty = value !== def

  return (
    <div className="py-3 border-b border-border/40 last:border-0">
      <div className="flex items-center gap-2 mb-1">
        <span className="text-sm text-text-primary font-medium">{label}</span>
        {isDirty && (
          <span className="text-2xs text-yellow-trade bg-yellow-trade/10 border border-yellow-trade/20 px-1.5 py-0.5 rounded font-mono">
            modified
          </span>
        )}
      </div>
      <SignalPills value={value} onChange={onChange} />
    </div>
  )
}

// ─── Group card ───────────────────────────────────────────────────────────────

function GroupCard({ groupKey, items, config, onChange }) {
  const gm = GROUP_META[groupKey] || { label: groupKey, desc: '' }

  return (
    <div className="card p-5">
      <div className="mb-4">
        <h3 className="font-semibold text-text-primary text-sm">{gm.label}</h3>
        {gm.desc && <p className="text-xs text-text-muted mt-0.5">{gm.desc}</p>}
      </div>
      <div>
        {items.map((meta) =>
          meta.group === 'regime_signals' ? (
            <RegimeSignalRow
              key={meta.key}
              meta={meta}
              value={config[meta.key] ?? meta.default}
              onChange={(v) => onChange(meta.key, v)}
            />
          ) : (
            <ConfigRow
              key={meta.key}
              meta={meta}
              value={config[meta.key] ?? meta.default}
              onChange={(v) => onChange(meta.key, v)}
            />
          )
        )}
      </div>
    </div>
  )
}

// ─── Daily Guide tab ──────────────────────────────────────────────────────────

function DailyGuide() {
  return (
    <div className="space-y-6 max-w-3xl">

      <div className="card p-5 border-l-2 border-l-border">
        <h3 className="font-semibold text-text-primary mb-3">First-Time Setup</h3>
        <p className="text-xs text-text-muted mb-3">Run once when setting up for the first time.</p>
        <ol className="space-y-2 text-sm text-text-secondary">
          <li className="flex gap-3 items-start">
            <span className="text-text-muted font-mono text-xs shrink-0 mt-0.5">1.</span>
            <span>Copy the environment file: <code className="bg-bg-hover px-1.5 py-0.5 rounded font-mono text-xs text-blue-trade">cp .env.example .env</code></span>
          </li>
          <li className="flex gap-3 items-start">
            <span className="text-text-muted font-mono text-xs shrink-0 mt-0.5">2.</span>
            <span>Create venv, install deps, start Docker, run migrations: <code className="bg-bg-hover px-1.5 py-0.5 rounded font-mono text-xs text-blue-trade">make setup</code></span>
          </li>
          <li className="flex gap-3 items-start">
            <span className="text-text-muted font-mono text-xs shrink-0 mt-0.5">3.</span>
            <span>Start everything: <code className="bg-bg-hover px-1.5 py-0.5 rounded font-mono text-xs text-blue-trade">make start</code></span>
          </li>
          <li className="flex gap-3 items-start">
            <span className="text-text-muted font-mono text-xs shrink-0 mt-0.5">4.</span>
            <span>Open <code className="bg-bg-hover px-1.5 py-0.5 rounded font-mono text-xs text-blue-trade">http://localhost:5173</code> in your browser</span>
          </li>
        </ol>
        <div className="mt-4 p-3 bg-bg-hover rounded text-xs text-text-muted space-y-1.5">
          <p><span className="text-text-secondary font-medium">No venv activation needed:</span> <code className="font-mono text-blue-trade">make start</code> uses the venv automatically every time.</p>
          <p><span className="text-text-secondary font-medium">After a reboot:</span> Docker may have stopped. <code className="font-mono text-blue-trade">make start</code> will restart it automatically, or run <code className="font-mono text-blue-trade">make up</code> manually first.</p>
        </div>
      </div>

      <div className="card p-5 border-l-2 border-l-green-trade">
        <h3 className="font-semibold text-text-primary mb-3 flex items-center gap-2">
          <span className="w-6 h-6 rounded-full bg-green-trade/20 border border-green-trade/30 flex items-center justify-center text-green-trade text-xs font-bold">1</span>
          Start of Day
        </h3>
        <ol className="space-y-2 text-sm text-text-secondary">
          <li className="flex gap-2"><span className="text-text-muted shrink-0">8:00 AM</span>Open terminal and run <code className="bg-bg-hover px-1.5 py-0.5 rounded font-mono text-xs text-blue-trade">make start</code> (or <code className="bg-bg-hover px-1.5 py-0.5 rounded font-mono text-xs text-blue-trade">make start-paper</code> for paper trading)</li>
          <li className="flex gap-2"><span className="text-text-muted shrink-0">8:30 AM</span>Bot auto-authenticates with Zerodha — check Telegram for confirmation</li>
          <li className="flex gap-2"><span className="text-text-muted shrink-0">9:10 AM</span>Market briefing sent via Telegram (regime, VIX, watchlist size)</li>
          <li className="flex gap-2"><span className="text-text-muted shrink-0">9:15 AM</span>Market opens — signals begin appearing in the Signals page</li>
        </ol>
      </div>

      <div className="card p-5 border-l-2 border-l-blue-trade">
        <h3 className="font-semibold text-text-primary mb-3 flex items-center gap-2">
          <span className="w-6 h-6 rounded-full bg-blue-trade/20 border border-blue-trade/30 flex items-center justify-center text-blue-trade text-xs font-bold">2</span>
          During the Day
        </h3>
        <div className="space-y-3 text-sm text-text-secondary">
          <p>The bot runs automatically. Monitor using this dashboard:</p>
          <ul className="space-y-1.5 ml-3">
            <li className="flex gap-2 items-start"><span className="text-blue-trade mt-0.5">•</span><span><strong className="text-text-primary">Dashboard</strong> — Live P&L, win rate, open positions count</span></li>
            <li className="flex gap-2 items-start"><span className="text-blue-trade mt-0.5">•</span><span><strong className="text-text-primary">Positions</strong> — Active trades with stop loss and target levels</span></li>
            <li className="flex gap-2 items-start"><span className="text-blue-trade mt-0.5">•</span><span><strong className="text-text-primary">Signals</strong> — Recent signals with confidence scores</span></li>
          </ul>
          <div className="mt-3 p-3 bg-yellow-trade/5 border border-yellow-trade/20 rounded text-xs">
            <span className="text-yellow-trade font-semibold">Semi-Auto mode:</span> When a trade is approved in Telegram, you'll see it appear in Positions automatically.
          </div>
        </div>
      </div>

      <div className="card p-5 border-l-2 border-l-red-trade">
        <h3 className="font-semibold text-text-primary mb-3 flex items-center gap-2">
          <span className="w-6 h-6 rounded-full bg-red-trade/20 border border-red-trade/30 flex items-center justify-center text-red-trade text-xs font-bold">3</span>
          Emergency Square Off
        </h3>
        <div className="space-y-2 text-sm text-text-secondary">
          <p>If you need to close all positions immediately:</p>
          <ul className="space-y-1.5 ml-3">
            <li className="flex gap-2 items-start"><span className="text-red-trade mt-0.5">•</span><span>Click the <strong className="text-text-primary">Square Off</strong> button in the top-right of the navbar</span></li>
            <li className="flex gap-2 items-start"><span className="text-red-trade mt-0.5">•</span><span>Or via Telegram: send <code className="bg-bg-hover px-1 py-0.5 rounded font-mono text-xs">/squareoff</code></span></li>
          </ul>
          <p className="text-xs text-text-muted mt-2">This closes all intraday positions at market price immediately. The bot auto-squares off at 3:12 PM IST regardless.</p>
        </div>
      </div>

      <div className="card p-5 border-l-2 border-l-border">
        <h3 className="font-semibold text-text-primary mb-3 flex items-center gap-2">
          <span className="w-6 h-6 rounded-full bg-bg-hover border border-border flex items-center justify-center text-text-muted text-xs font-bold">4</span>
          End of Day
        </h3>
        <ol className="space-y-2 text-sm text-text-secondary">
          <li className="flex gap-2"><span className="text-text-muted shrink-0">3:12 PM</span>Bot auto-squares off all open intraday positions</li>
          <li className="flex gap-2"><span className="text-text-muted shrink-0">4:30 PM</span>Daily P&L summary sent via Telegram</li>
          <li className="flex gap-2"><span className="text-text-muted shrink-0">Evening</span>Stop the bot: <code className="bg-bg-hover px-1.5 py-0.5 rounded font-mono text-xs text-blue-trade">Ctrl+C</code> in the terminal</li>
          <li className="flex gap-2"><span className="text-text-muted shrink-0">Optional</span>Review trades in the <strong className="text-text-primary">Trades</strong> and <strong className="text-text-primary">P&L</strong> pages</li>
        </ol>
      </div>

      <div className="card p-5">
        <h3 className="font-semibold text-text-primary mb-3">Trading Modes</h3>
        <div className="space-y-2">
          {[
            { mode: 'DEV', cmd: 'make start', color: 'text-yellow-trade bg-yellow-trade/10 border-yellow-trade/20', desc: 'Mock data feed. Safe for testing — no real orders.' },
            { mode: 'PAPER', cmd: 'make start-paper', color: 'text-cyan-trade bg-cyan-trade/10 border-cyan-trade/20', desc: 'Live market data, simulated order execution.' },
            { mode: 'SEMI-AUTO', cmd: 'make start-semi-auto', color: 'text-purple-400 bg-purple-500/10 border-purple-500/20', desc: 'Live data, real orders — requires Telegram approval per trade.' },
            { mode: 'LIVE', cmd: 'make start-live', color: 'text-red-trade bg-red-trade/10 border-red-trade/20', desc: 'Fully automated live trading. Requires Kite credentials.' },
          ].map(({ mode, cmd, color, desc }) => (
            <div key={mode} className="flex items-start gap-3 py-2 border-b border-border/30 last:border-0">
              <span className={`badge border shrink-0 mt-0.5 ${color}`}>{mode}</span>
              <div>
                <code className="text-xs font-mono text-blue-trade">{cmd}</code>
                <p className="text-xs text-text-muted mt-0.5">{desc}</p>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="card p-5">
        <h3 className="font-semibold text-text-primary mb-3">Telegram Commands</h3>
        <div className="space-y-1">
          {[
            ['/status', 'Bot status, mode, regime, today\'s P&L'],
            ['/pnl', 'Today\'s P&L summary'],
            ['/positions', 'List of open positions'],
            ['/squareoff', 'Emergency square off all positions'],
          ].map(([cmd, desc]) => (
            <div key={cmd} className="flex items-center gap-3 py-1.5 border-b border-border/30 last:border-0">
              <code className="text-xs font-mono text-blue-trade w-28 shrink-0">{cmd}</code>
              <span className="text-xs text-text-secondary">{desc}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

// ─── Main Settings page ───────────────────────────────────────────────────────

export default function Settings() {
  const [tab, setTab]           = useState('config')
  const [schema, setSchema]     = useState(null)
  const [config, setConfig]     = useState({})
  const [pending, setPending]   = useState({})
  const [loading, setLoading]   = useState(true)
  const [saving, setSaving]     = useState(false)
  const [saved, setSaved]       = useState(false)
  const [error, setError]       = useState(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [sc, cfg] = await Promise.all([fetchConfigSchema(), fetchConfig()])
      setSchema(sc)
      setConfig(cfg)
      setPending({})
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const handleChange = (key, value) => {
    setPending((prev) => ({ ...prev, [key]: value }))
    setConfig((prev) => ({ ...prev, [key]: value }))
    setSaved(false)
  }

  const hasPending = Object.keys(pending).length > 0

  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      const updated = await updateConfig(pending)
      setConfig(updated)
      setPending({})
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  const reset = () => {
    if (!schema) return
    const defaults = {}
    for (const [k, meta] of Object.entries(schema)) defaults[k] = meta.default
    setConfig(defaults)
    setPending(defaults)
    setSaved(false)
  }

  const grouped = schema ? groupBy(schema) : {}
  const GROUP_ORDER = ['execution', 'strategies', 'indicators', 'timeframes', 'regime_caps', 'regime_signals']

  return (
    <div className="p-5 space-y-4 animate-fade-in max-w-screen-xl mx-auto">

      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="page-title">Settings</h1>
          <p className="text-xs text-text-muted mt-0.5">All changes apply on the next signal cycle — no restart needed.</p>
        </div>

        {tab === 'config' && (
          <div className="flex items-center gap-2">
            {saved && (
              <span className="text-xs text-green-trade flex items-center gap-1.5">
                <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                </svg>
                Saved
              </span>
            )}
            <button
              onClick={reset}
              disabled={saving}
              className="btn bg-bg-card border border-border text-text-muted hover:text-text-primary text-xs"
            >
              Reset to defaults
            </button>
            <button
              onClick={save}
              disabled={!hasPending || saving}
              className={`btn text-xs ${
                hasPending && !saving
                  ? 'bg-blue-trade text-white hover:bg-blue-trade/90'
                  : 'bg-bg-hover text-text-muted border border-border cursor-not-allowed'
              }`}
            >
              {saving ? (
                <span className="flex items-center gap-1.5">
                  <svg className="w-3.5 h-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                  </svg>
                  Saving…
                </span>
              ) : hasPending ? `Save ${Object.keys(pending).length} change${Object.keys(pending).length > 1 ? 's' : ''}` : 'No changes'}
            </button>
          </div>
        )}
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-1 border-b border-border">
        {[
          { id: 'config', label: 'Configuration' },
          { id: 'guide',  label: 'Daily Guide' },
        ].map(({ id, label }) => (
          <button
            key={id}
            onClick={() => setTab(id)}
            className={`px-4 py-2 text-xs font-medium border-b-2 -mb-px transition-colors ${
              tab === id
                ? 'border-blue-trade text-blue-trade'
                : 'border-transparent text-text-muted hover:text-text-primary'
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Error banner */}
      {error && (
        <div className="px-4 py-3 bg-red-trade/10 border border-red-trade/30 rounded text-sm text-red-trade font-mono">
          {error}
        </div>
      )}

      {/* Content */}
      {tab === 'guide' ? (
        <DailyGuide />
      ) : loading ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {[...Array(6)].map((_, i) => (
            <div key={i} className="card p-5 space-y-3">
              <div className="skeleton h-4 w-32" />
              <div className="skeleton h-3 w-48" />
              <div className="space-y-2 mt-2">
                {[...Array(3)].map((_, j) => (
                  <div key={j} className="skeleton h-8 w-full" />
                ))}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {GROUP_ORDER.filter((g) => grouped[g]).map((groupKey) => (
            <div key={groupKey} className={groupKey === 'regime_signals' ? 'lg:col-span-2' : ''}>
              <GroupCard
                groupKey={groupKey}
                items={grouped[groupKey]}
                config={config}
                onChange={handleChange}
              />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

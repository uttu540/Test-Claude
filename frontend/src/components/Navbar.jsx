import { useState, useEffect, useCallback } from 'react'
import { NavLink } from 'react-router-dom'
import { fetchBotStatus, squareOffAll } from '../api'
import { useWebSocket } from '../ws'

const MODE_CONFIG = {
  DEV: {
    label: 'DEV',
    bg: 'bg-yellow-trade/10',
    text: 'text-yellow-trade',
    border: 'border-yellow-trade/30',
    dot: 'bg-yellow-trade',
  },
  PAPER: {
    label: 'PAPER',
    bg: 'bg-cyan-trade/10',
    text: 'text-cyan-trade',
    border: 'border-cyan-trade/30',
    dot: 'bg-cyan-trade',
  },
  SEMI_AUTO: {
    label: 'SEMI-AUTO',
    bg: 'bg-purple-500/10',
    text: 'text-purple-400',
    border: 'border-purple-500/30',
    dot: 'bg-purple-400',
  },
  LIVE: {
    label: 'LIVE',
    bg: 'bg-red-trade/10',
    text: 'text-red-trade',
    border: 'border-red-trade/30',
    dot: 'bg-red-trade',
  },
}

function WsStatusDot({ status }) {
  const configs = {
    connected: 'bg-green-trade animate-pulse-green',
    connecting: 'bg-yellow-trade animate-pulse',
    disconnected: 'bg-text-muted',
  }
  const labels = {
    connected: 'Live',
    connecting: 'Connecting…',
    disconnected: 'Offline',
  }
  return (
    <span className="flex items-center gap-1.5 text-xs text-text-muted">
      <span className={`w-1.5 h-1.5 rounded-full ${configs[status]}`} />
      {labels[status]}
    </span>
  )
}

export default function Navbar({ wsStatus }) {
  const [botStatus, setBotStatus] = useState(null)
  const [squareOffLoading, setSquareOffLoading] = useState(false)
  const [squareOffError, setSquareOffError] = useState(null)
  const [showConfirm, setShowConfirm] = useState(false)
  const [clock, setClock] = useState(new Date())

  useEffect(() => {
    fetchBotStatus()
      .then(setBotStatus)
      .catch(() => setBotStatus({ mode: 'DEV', capital: 100000, is_running: false, version: '—' }))
  }, [])

  // Clock tick
  useEffect(() => {
    const t = setInterval(() => setClock(new Date()), 1000)
    return () => clearInterval(t)
  }, [])

  const handleSquareOff = async () => {
    setSquareOffLoading(true)
    setSquareOffError(null)
    try {
      await squareOffAll()
      setShowConfirm(false)
    } catch (err) {
      setSquareOffError(err.message)
    } finally {
      setSquareOffLoading(false)
    }
  }

  const mode = botStatus?.mode || 'DEV'
  const modeConfig = MODE_CONFIG[mode] || MODE_CONFIG.DEV
  const capital = botStatus?.capital ?? 100000

  const istTime = clock.toLocaleTimeString('en-IN', {
    timeZone: 'Asia/Kolkata',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })

  const istDate = clock.toLocaleDateString('en-IN', {
    timeZone: 'Asia/Kolkata',
    day: '2-digit',
    month: 'short',
    year: 'numeric',
  })

  return (
    <>
      <nav className="sticky top-0 z-50 h-14 bg-bg-card border-b border-border flex items-center px-4 gap-4">
        {/* Logo */}
        <div className="flex items-center gap-2 mr-2 shrink-0">
          <div className="w-7 h-7 rounded bg-blue-trade/20 border border-blue-trade/30 flex items-center justify-center">
            <span className="text-blue-trade text-xs font-bold font-mono">TB</span>
          </div>
          <span className="font-semibold text-sm tracking-wide text-text-primary hidden sm:block">
            TradeBot
          </span>
        </div>

        {/* Nav links */}
        <div className="flex items-center gap-0.5">
          {[
            { to: '/',          label: 'Dashboard', end: true  },
            { to: '/trades',    label: 'Trades',    end: false },
            { to: '/pnl',       label: 'P&L',       end: false },
            { to: '/changelog', label: 'Guide',     end: false },
          ].map(({ to, label, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                `px-3 py-1.5 rounded text-sm font-medium transition-colors ${
                  isActive
                    ? 'bg-blue-trade/10 text-blue-trade'
                    : 'text-text-muted hover:text-text-primary hover:bg-bg-hover'
                }`
              }
            >
              {label}
            </NavLink>
          ))}
        </div>

        {/* Spacer */}
        <div className="flex-1" />

        {/* IST Clock */}
        <div className="hidden lg:flex flex-col items-end">
          <span className="font-mono text-sm text-text-primary tracking-wider">{istTime}</span>
          <span className="text-2xs text-text-muted">{istDate} IST</span>
        </div>

        <div className="w-px h-6 bg-border" />

        {/* WS Status */}
        <WsStatusDot status={wsStatus} />

        <div className="w-px h-6 bg-border" />

        {/* Mode badge */}
        <div className={`badge ${modeConfig.bg} ${modeConfig.text} border ${modeConfig.border}`}>
          <span className={`w-1.5 h-1.5 rounded-full ${modeConfig.dot}`} />
          {modeConfig.label}
        </div>

        {/* Capital */}
        <div className="hidden md:flex flex-col items-end">
          <span className="text-2xs text-text-muted uppercase tracking-wide">Capital</span>
          <span className="font-mono text-sm text-text-primary">
            ₹{capital.toLocaleString('en-IN')}
          </span>
        </div>

        <div className="w-px h-6 bg-border" />

        {/* Square Off */}
        <button
          onClick={() => setShowConfirm(true)}
          className="btn-danger text-xs shrink-0"
        >
          <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
          </svg>
          Square Off All
        </button>
      </nav>

      {/* Square Off Confirm Modal */}
      {showConfirm && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm animate-fade-in"
          onClick={(e) => e.target === e.currentTarget && setShowConfirm(false)}
        >
          <div className="card w-full max-w-sm mx-4 p-6 animate-slide-up">
            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 rounded-full bg-red-trade/10 border border-red-trade/30 flex items-center justify-center shrink-0">
                <svg className="w-5 h-5 text-red-trade" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
                </svg>
              </div>
              <div>
                <h3 className="font-semibold text-text-primary">Square Off All Positions</h3>
                <p className="text-sm text-text-secondary mt-0.5">
                  This will close all open positions immediately at market price.
                </p>
              </div>
            </div>

            {squareOffError && (
              <div className="mb-4 px-3 py-2 bg-red-trade/10 border border-red-trade/30 rounded text-sm text-red-trade font-mono">
                {squareOffError}
              </div>
            )}

            <div className="flex gap-2 justify-end">
              <button
                onClick={() => {
                  setShowConfirm(false)
                  setSquareOffError(null)
                }}
                className="btn bg-bg-hover text-text-secondary border border-border hover:text-text-primary"
              >
                Cancel
              </button>
              <button
                onClick={handleSquareOff}
                disabled={squareOffLoading}
                className="btn bg-red-trade text-white hover:bg-red-dim focus:ring-2 focus:ring-red-trade focus:ring-offset-1 focus:ring-offset-bg-primary disabled:opacity-50"
              >
                {squareOffLoading ? (
                  <>
                    <svg className="w-3.5 h-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                    </svg>
                    Closing…
                  </>
                ) : (
                  'Confirm Square Off'
                )}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

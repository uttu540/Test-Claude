function StatusBadge({ status }) {
  const configs = {
    OPEN:    { bg: 'bg-yellow-trade/10', text: 'text-yellow-trade', border: 'border-yellow-trade/20' },
    CLOSED:  { bg: 'bg-blue-trade/10',   text: 'text-blue-trade',   border: 'border-blue-trade/20'   },
    STOPPED: { bg: 'bg-red-trade/10',    text: 'text-red-trade',    border: 'border-red-trade/20'    },
    TARGET:  { bg: 'bg-green-muted',     text: 'text-green-trade',  border: 'border-green-trade/20'  },
  }
  const c = configs[status] || configs.CLOSED
  return (
    <span className={`badge ${c.bg} ${c.text} border ${c.border}`}>
      {status}
    </span>
  )
}

function DirectionBadge({ direction }) {
  if (direction === 'LONG') {
    return <span className="badge bg-green-muted text-green-trade border border-green-trade/20 text-2xs">▲ LONG</span>
  }
  return <span className="badge bg-red-muted text-red-trade border border-red-trade/20 text-2xs">▼ SHORT</span>
}

function PnLCell({ pnl, status }) {
  if (status === 'OPEN') {
    return <span className="font-mono text-sm text-yellow-trade">Open</span>
  }
  if (pnl === null || pnl === undefined) {
    return <span className="font-mono text-sm text-text-muted">—</span>
  }
  const isPos = pnl >= 0
  return (
    <span className={`font-mono text-sm font-semibold ${isPos ? 'text-green-trade' : 'text-red-trade'}`}>
      {isPos ? '+' : ''}₹{Math.abs(pnl).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
    </span>
  )
}

function RRCell({ rr }) {
  if (rr === null || rr === undefined) return <span className="font-mono text-text-muted text-sm">—</span>
  const isPos = rr >= 0
  return (
    <span className={`font-mono text-sm ${isPos ? 'text-green-trade' : 'text-red-trade'}`}>
      {rr >= 0 ? '+' : ''}{rr.toFixed(2)}R
    </span>
  )
}

function formatDateTime(isoString) {
  if (!isoString) return '—'
  try {
    const d = new Date(isoString)
    return d.toLocaleString('en-IN', {
      timeZone: 'Asia/Kolkata',
      day: '2-digit',
      month: 'short',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    })
  } catch {
    return isoString
  }
}

function SkeletonRow() {
  return (
    <tr>
      {Array.from({ length: 9 }).map((_, i) => (
        <td key={i} className="td">
          <div className="skeleton h-4 w-16" />
        </td>
      ))}
    </tr>
  )
}

function EmptyState({ colSpan }) {
  return (
    <tr>
      <td colSpan={colSpan} className="td text-center py-16">
        <div className="flex flex-col items-center gap-2 text-text-muted">
          <svg className="w-10 h-10 opacity-30" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
          </svg>
          <span className="text-sm">No trades found</span>
        </div>
      </td>
    </tr>
  )
}

export default function TradesTable({ trades = [], loading = false, error = null }) {
  return (
    <>
      {error && (
        <div className="mb-3 px-4 py-2 bg-red-trade/10 border border-red-trade/30 rounded-lg text-sm text-red-trade font-mono">
          {error}
        </div>
      )}

      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[900px]">
            <thead>
              <tr>
                <th className="th">Symbol</th>
                <th className="th">Direction</th>
                <th className="th">Strategy</th>
                <th className="th text-right">Entry</th>
                <th className="th text-right">Exit</th>
                <th className="th text-right">P&L</th>
                <th className="th text-right">R:R</th>
                <th className="th">Status</th>
                <th className="th">Entry Time</th>
              </tr>
            </thead>
            <tbody>
              {loading
                ? Array.from({ length: 10 }).map((_, i) => <SkeletonRow key={i} />)
                : trades.length === 0
                ? <EmptyState colSpan={9} />
                : trades.map((t, i) => {
                    const isProfit = t.pnl !== null && t.pnl > 0
                    const isLoss   = t.pnl !== null && t.pnl < 0
                    const isOpen   = t.status === 'OPEN'

                    const rowClass = isProfit
                      ? 'border-l-2 border-l-green-trade/40 bg-green-muted/5 hover:bg-green-muted/20'
                      : isLoss
                      ? 'border-l-2 border-l-red-trade/40 bg-red-muted/5 hover:bg-red-muted/20'
                      : isOpen
                      ? 'border-l-2 border-l-yellow-trade/40 bg-yellow-muted/5 hover:bg-yellow-muted/20'
                      : 'border-l-2 border-l-transparent hover:bg-bg-hover'

                    return (
                      <tr key={t.id || i} className={`transition-colors duration-100 ${rowClass}`}>
                        <td className="td">
                          <span className="font-mono font-semibold text-text-primary text-sm">
                            {t.symbol}
                          </span>
                        </td>
                        <td className="td">
                          <DirectionBadge direction={t.direction} />
                        </td>
                        <td className="td">
                          <span className="text-xs text-text-secondary uppercase tracking-wide">
                            {t.strategy || '—'}
                          </span>
                        </td>
                        <td className="td text-right">
                          <span className="font-mono text-sm text-text-secondary">
                            {t.entry_price != null
                              ? `₹${Number(t.entry_price).toLocaleString('en-IN', { minimumFractionDigits: 2 })}`
                              : '—'}
                          </span>
                        </td>
                        <td className="td text-right">
                          <span className="font-mono text-sm text-text-muted">
                            {t.exit_price != null
                              ? `₹${Number(t.exit_price).toLocaleString('en-IN', { minimumFractionDigits: 2 })}`
                              : '—'}
                          </span>
                        </td>
                        <td className="td text-right">
                          <PnLCell pnl={t.pnl} status={t.status} />
                        </td>
                        <td className="td text-right">
                          <RRCell rr={t.rr} />
                        </td>
                        <td className="td">
                          <StatusBadge status={t.status} />
                        </td>
                        <td className="td">
                          <span className="font-mono text-xs text-text-muted">
                            {formatDateTime(t.entry_time)}
                          </span>
                        </td>
                      </tr>
                    )
                  })}
            </tbody>
          </table>
        </div>
      </div>
    </>
  )
}

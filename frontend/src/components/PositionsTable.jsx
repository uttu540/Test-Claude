function PnLCell({ pnl }) {
  if (pnl === null || pnl === undefined) {
    return <span className="font-mono text-sm text-text-muted">—</span>
  }
  const isPos = pnl >= 0
  return (
    <span className={`font-mono text-sm font-medium ${isPos ? 'text-green-trade' : 'text-red-trade'}`}>
      {isPos ? '+' : ''}₹{Math.abs(pnl).toLocaleString('en-IN', { minimumFractionDigits: 2 })}
    </span>
  )
}

function DirectionBadge({ direction }) {
  if (direction === 'LONG') {
    return (
      <span className="badge bg-green-muted text-green-trade border border-green-trade/20 text-2xs">
        ▲ LONG
      </span>
    )
  }
  return (
    <span className="badge bg-red-muted text-red-trade border border-red-trade/20 text-2xs">
      ▼ SHORT
    </span>
  )
}

function SkeletonRow() {
  return (
    <tr>
      {Array.from({ length: 7 }).map((_, i) => (
        <td key={i} className="td">
          <div className="skeleton h-4 w-16" />
        </td>
      ))}
    </tr>
  )
}

function EmptyState() {
  return (
    <tr>
      <td colSpan={7} className="td text-center py-12">
        <div className="flex flex-col items-center gap-2 text-text-muted">
          <svg className="w-8 h-8 opacity-40" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 10V7" />
          </svg>
          <span className="text-sm">No open positions</span>
          <span className="text-xs">Bot is idle or market is closed</span>
        </div>
      </td>
    </tr>
  )
}

function PriceMono({ price, decimals = 2 }) {
  if (price === null || price === undefined) return <span className="font-mono text-text-muted">—</span>
  return (
    <span className="font-mono text-sm text-text-secondary">
      ₹{Number(price).toLocaleString('en-IN', { minimumFractionDigits: decimals })}
    </span>
  )
}

export default function PositionsTable({ positions = [], loading = false, error = null }) {
  return (
    <div className="card overflow-hidden">
      <div className="flex items-center justify-between px-4 py-3 border-b border-border">
        <h2 className="text-sm font-semibold text-text-primary flex items-center gap-2">
          <span className="w-1.5 h-1.5 rounded-full bg-green-trade animate-pulse" />
          Open Positions
        </h2>
        <span className="badge bg-green-muted text-green-trade border border-green-trade/20">
          {positions.length} active
        </span>
      </div>

      {error && (
        <div className="px-4 py-2 bg-red-trade/5 border-b border-red-trade/20 text-xs text-red-trade font-mono">
          {error}
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full min-w-[700px]">
          <thead>
            <tr>
              <th className="th">Symbol</th>
              <th className="th">Direction</th>
              <th className="th text-right">Entry</th>
              <th className="th text-right">Current P&L</th>
              <th className="th text-right">Stop Loss</th>
              <th className="th text-right">Target</th>
              <th className="th text-right">Qty</th>
            </tr>
          </thead>
          <tbody>
            {loading
              ? Array.from({ length: 3 }).map((_, i) => <SkeletonRow key={i} />)
              : positions.length === 0
              ? <EmptyState />
              : positions.map((p, i) => {
                  const pnl = p.pnl ?? 0
                  const rowBg =
                    pnl > 0
                      ? 'hover:bg-green-muted/30'
                      : pnl < 0
                      ? 'hover:bg-red-muted/30'
                      : 'hover:bg-bg-hover'

                  return (
                    <tr key={i} className={`transition-colors duration-100 ${rowBg} animate-fade-in`}>
                      <td className="td">
                        <div className="flex flex-col">
                          <span className="font-mono font-semibold text-text-primary text-sm">
                            {p.symbol}
                          </span>
                          {p.strategy && (
                            <span className="text-2xs text-text-muted">{p.strategy}</span>
                          )}
                        </div>
                      </td>
                      <td className="td">
                        <DirectionBadge direction={p.direction} />
                      </td>
                      <td className="td text-right">
                        <PriceMono price={p.entry_price} />
                      </td>
                      <td className="td text-right">
                        <PnLCell pnl={pnl} />
                      </td>
                      <td className="td text-right">
                        <PriceMono price={p.stop_loss} />
                      </td>
                      <td className="td text-right">
                        <PriceMono price={p.target} />
                      </td>
                      <td className="td text-right">
                        <span className="font-mono text-sm text-text-secondary">{p.qty}</span>
                      </td>
                    </tr>
                  )
                })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

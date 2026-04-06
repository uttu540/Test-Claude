/**
 * PnLBar — Daily loss limit progress bar
 * Props:
 *   used  — amount used (positive number)
 *   limit — daily loss limit
 *   loading — boolean
 */
export default function PnLBar({ used = 0, limit = 2000, loading = false }) {
  const pct = Math.min((used / limit) * 100, 100)

  const barColor =
    pct >= 90
      ? 'bg-red-trade glow-red'
      : pct >= 70
      ? 'bg-yellow-trade'
      : 'bg-green-trade'

  const textColor =
    pct >= 90 ? 'text-red-trade' : pct >= 70 ? 'text-yellow-trade' : 'text-green-trade'

  return (
    <div className="card px-4 py-3 flex items-center gap-4">
      <div className="flex flex-col shrink-0">
        <span className="text-2xs text-text-muted uppercase tracking-widest font-medium">
          Daily Loss Limit
        </span>
        {loading ? (
          <div className="skeleton h-4 w-24 mt-1" />
        ) : (
          <span className={`font-mono text-sm font-medium ${textColor}`}>
            ₹{used.toLocaleString('en-IN')}
            <span className="text-text-muted font-normal">
              {' '}/ ₹{limit.toLocaleString('en-IN')}
            </span>
          </span>
        )}
      </div>

      <div className="flex-1 flex flex-col gap-1.5">
        <div className="relative h-2 bg-bg-hover rounded-full overflow-hidden">
          {loading ? (
            <div className="skeleton absolute inset-0" />
          ) : (
            <div
              className={`h-full rounded-full transition-all duration-700 ${barColor}`}
              style={{ width: `${pct}%` }}
            />
          )}
        </div>
        {!loading && (
          <div className="flex justify-between items-center">
            <span className="text-2xs text-text-muted">
              {pct.toFixed(1)}% consumed
            </span>
            <span className="text-2xs text-text-muted">
              ₹{(limit - used).toLocaleString('en-IN')} remaining
            </span>
          </div>
        )}
      </div>

      {!loading && pct >= 90 && (
        <div className="badge bg-red-trade/10 text-red-trade border border-red-trade/30 shrink-0 animate-pulse">
          CRITICAL
        </div>
      )}
      {!loading && pct >= 70 && pct < 90 && (
        <div className="badge bg-yellow-trade/10 text-yellow-trade border border-yellow-trade/30 shrink-0">
          WARNING
        </div>
      )}
    </div>
  )
}

export function StatCardSkeleton() {
  return (
    <div className="card p-4 flex flex-col gap-2">
      <div className="skeleton h-3 w-20" />
      <div className="skeleton h-7 w-32" />
      <div className="skeleton h-3 w-16" />
    </div>
  )
}

/**
 * StatCard
 * Props:
 *   label      — string
 *   value      — string | number (already formatted)
 *   sub        — optional subtitle string
 *   trend      — 'up' | 'down' | 'neutral' (colors the value)
 *   icon       — optional JSX element
 *   accent     — optional tailwind color class for left border
 *   loading    — boolean
 */
export default function StatCard({ label, value, sub, trend = 'neutral', icon, accent, loading }) {
  if (loading) return <StatCardSkeleton />

  const trendColor = {
    up: 'text-green-trade',
    down: 'text-red-trade',
    neutral: 'text-text-primary',
  }[trend] || 'text-text-primary'

  const borderColor = accent || (trend === 'up' ? 'border-l-green-trade' : trend === 'down' ? 'border-l-red-trade' : 'border-l-border')

  return (
    <div className={`card p-4 flex flex-col gap-1 border-l-2 ${borderColor}`}>
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-text-muted uppercase tracking-widest">{label}</span>
        {icon && <span className="text-text-muted opacity-60">{icon}</span>}
      </div>
      <div className={`font-mono text-2xl font-semibold tracking-tight ${trendColor}`}>
        {value}
      </div>
      {sub && (
        <div className="text-xs text-text-muted">{sub}</div>
      )}
    </div>
  )
}

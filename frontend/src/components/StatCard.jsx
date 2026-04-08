export function StatCardSkeleton() {
  return (
    <div className="card p-4 flex flex-col gap-2">
      <div className="skeleton h-3 w-20" />
      <div className="skeleton h-7 w-32" />
      <div className="skeleton h-3 w-16" />
    </div>
  )
}

export default function StatCard({ label, value, sub, trend = 'neutral', icon, accent, loading }) {
  if (loading) return <StatCardSkeleton />

  const trendColor = {
    up: 'text-green-trade',
    down: 'text-red-trade',
    neutral: 'text-text-primary',
  }[trend] || 'text-text-primary'

  const borderColor = accent || (
    trend === 'up' ? 'border-l-green-trade' :
    trend === 'down' ? 'border-l-red-trade' :
    'border-l-border'
  )

  return (
    <div className={`card p-4 border-l-2 ${borderColor} flex flex-col gap-1`}>
      <div className="flex items-center justify-between">
        <span className="section-label">{label}</span>
        {icon && <span className="text-text-muted opacity-50">{icon}</span>}
      </div>
      <div className={`font-mono text-xl font-semibold tracking-tight leading-tight ${trendColor}`}>
        {value}
      </div>
      {sub && (
        <div className="text-2xs text-text-muted">{sub}</div>
      )}
    </div>
  )
}

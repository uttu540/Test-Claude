const BASE_URL = 'http://localhost:8000'

class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request(path, options = {}) {
  const url = `${BASE_URL}${path}`
  try {
    const res = await fetch(url, {
      headers: {
        'Content-Type': 'application/json',
        ...options.headers,
      },
      ...options,
    })

    if (!res.ok) {
      const text = await res.text().catch(() => 'Unknown error')
      throw new ApiError(`${res.status}: ${text}`, res.status)
    }

    // 204 No Content
    if (res.status === 204) return null

    return res.json()
  } catch (err) {
    if (err instanceof ApiError) throw err
    // Network error
    throw new ApiError(err.message || 'Network error — is the backend running?', 0)
  }
}

// ─── P&L ─────────────────────────────────────────────────────────────────────

export async function fetchPnLToday() {
  return request('/api/pnl/today')
}

// Expected shape:
// {
//   net_pnl: number,
//   win_rate: number,         // 0-100
//   trades_today: number,
//   daily_loss_used: number,
//   daily_loss_limit: number,
//   market_regime: "TRENDING" | "RANGING" | "UNKNOWN",
//   pnl_series: [{ time: string, pnl: number }]  // for sparkline
// }

// ─── Positions ───────────────────────────────────────────────────────────────

export async function fetchPositions() {
  return request('/api/positions')
}

// Expected shape: array of {
//   symbol: string,
//   direction: "LONG" | "SHORT",
//   entry_price: number,
//   current_price: number,
//   pnl: number,
//   stop_loss: number,
//   target: number,
//   qty: number,
//   strategy: string
// }

// ─── Signals ─────────────────────────────────────────────────────────────────

export async function fetchRecentSignals() {
  return request('/api/signals/recent')
}

// Expected shape: array of {
//   symbol: string,
//   direction: "LONG" | "SHORT",
//   signal_type: string,
//   confidence: number,       // 0-100
//   price: number,
//   time: string              // ISO timestamp
// }

// ─── Trades ──────────────────────────────────────────────────────────────────

export async function fetchTrades({ page = 1, per_page = 50 } = {}) {
  return request(`/api/trades?page=${page}&per_page=${per_page}`)
}

// Expected shape: {
//   trades: array of {
//     id: string,
//     symbol: string,
//     direction: "LONG" | "SHORT",
//     strategy: string,
//     entry_price: number,
//     exit_price: number | null,
//     pnl: number | null,
//     rr: number | null,
//     status: "OPEN" | "CLOSED" | "STOPPED",
//     entry_time: string,
//     exit_time: string | null,
//   },
//   total: number,
//   page: number,
//   per_page: number
// }

export async function fetchPnLHistory(days = 30) {
  return request(`/api/pnl/history?days=${days}`)
}

// ─── Config ──────────────────────────────────────────────────────────────────

export async function fetchConfig() {
  return request('/api/config')
}

export async function updateConfig(updates) {
  return request('/api/config', { method: 'POST', body: JSON.stringify(updates) })
}

export async function fetchConfigSchema() {
  return request('/api/config/schema')
}

// ─── Bot Control ─────────────────────────────────────────────────────────────

export async function squareOffAll() {
  return request('/api/bot/square-off', { method: 'POST' })
}

export async function fetchBotStatus() {
  return request('/api/bot/status')
}

// Expected shape: {
//   mode: "DEV" | "PAPER" | "SEMI_AUTO" | "LIVE",
//   capital: number,
//   is_running: boolean,
//   version: string
// }

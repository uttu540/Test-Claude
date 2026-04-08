// ─── Changelog & Guide ────────────────────────────────────────────────────────
// Static reference page: feature history, quick-start, mode guide, API ref.

const PHASES = [
  {
    phase: 5,
    label: 'Human Approval Gate',
    date: 'Apr 2026',
    status: 'done',
    features: [
      'Semi-auto mode — trades require Telegram ✅/❌ before execution',
      'Configurable approval timeout (default 60s, auto-rejects on expiry)',
      'Inline keyboard callbacks via python-telegram-bot v21',
      'Authorization list: only whitelisted Telegram user IDs can approve',
      'Approval message auto-edits to show outcome (approved / rejected / expired)',
    ],
    env: 'APP_ENV=semi-auto',
  },
  {
    phase: 4,
    label: 'Trade Lifecycle & P&L Tracking',
    date: 'Apr 2026',
    status: 'done',
    features: [
      'TradeLifecycleManager monitors open positions every 5s',
      'Detects SL hits, target hits, and intraday time exits (3:12 PM)',
      'Calculates gross P&L, brokerage, STT, GST, exchange charges, SEBI charges, stamp duty',
      'Daily P&L aggregation stored to daily_pnl table',
      'Kill switch: halts new orders if daily loss limit breached',
    ],
    env: null,
  },
  {
    phase: 3,
    label: 'Regime Filtering & Signals',
    date: 'Mar 2026',
    status: 'done',
    features: [
      'Market regime detection: TRENDING / RANGING / UNKNOWN',
      'ORB (Opening Range Breakout) signal on 15-min candle closes',
      'VWAP cross signal with volume confirmation',
      'Multi-timeframe alignment scoring',
      'Backtesting framework with walk-forward validation',
    ],
    env: null,
  },
  {
    phase: 2,
    label: 'Risk Engine & Claude AI',
    date: 'Mar 2026',
    status: 'done',
    features: [
      'RiskEngine: position sizing based on ATR, max 2% capital per trade',
      'Daily loss limit guard — halts trading after limit hit',
      'Claude AI strategy evaluation before each trade',
      'AI confidence score and reasoning stored with every trade',
      'Max 8 concurrent open positions enforced',
    ],
    env: null,
  },
  {
    phase: 1,
    label: 'Foundation',
    date: 'Feb 2026',
    status: 'done',
    features: [
      'Zerodha Kite Connect integration (WebSocket + REST)',
      'PostgreSQL schema with Alembic migrations',
      'Redis for real-time tick cache and regime state',
      'FastAPI REST + WebSocket dashboard API',
      'Telegram notifications for all trade events',
      'Paper trading mode with slippage simulation',
    ],
    env: null,
  },
  {
    phase: 6,
    label: 'Groww Integration',
    date: 'Upcoming',
    status: 'pending',
    features: [
      'GrowwOrderManager implementing BrokerInterface ABC',
      'One-line switch in broker_router.py',
      'GROWW_API_KEY / GROWW_API_SECRET config stubs already in place',
    ],
    env: null,
  },
]

const MODES = [
  {
    key: 'development',
    label: 'DEV',
    color: 'text-yellow-trade border-yellow-trade/30 bg-yellow-trade/10',
    cmd: 'make dev',
    desc: 'Paper orders, mock market feed, all Telegram alerts enabled. Safe for local testing.',
  },
  {
    key: 'paper',
    label: 'PAPER',
    color: 'text-cyan-trade border-cyan-trade/30 bg-cyan-trade/10',
    cmd: 'make paper',
    desc: 'Paper orders with real Zerodha WebSocket feed. Validates signals against live data without risking capital.',
  },
  {
    key: 'semi-auto',
    label: 'SEMI-AUTO',
    color: 'text-purple-400 border-purple-500/30 bg-purple-500/10',
    cmd: 'make semi-auto',
    desc: 'Real orders on Zerodha, but every trade requires your Telegram ✅ before execution. Requires TELEGRAM_AUTHORIZED_IDS.',
  },
  {
    key: 'live',
    label: 'LIVE',
    color: 'text-red-trade border-red-trade/30 bg-red-trade/10',
    cmd: 'make live',
    desc: 'Fully automated. Real money, real orders, no human gate. Confirmation prompt in terminal.',
  },
]

const QUICK_START = [
  { step: '1', title: 'Start infrastructure', cmd: 'make up', note: 'Starts PostgreSQL + Redis via Docker.' },
  { step: '2', title: 'Install dependencies', cmd: 'make install', note: 'Installs Python packages from requirements.txt.' },
  { step: '3', title: 'Create .env file', cmd: 'cp .env.example .env', note: 'Fill in KITE_API_KEY, TELEGRAM_BOT_TOKEN, etc.' },
  { step: '4', title: 'Run DB migrations', cmd: 'make db-upgrade', note: 'Applies Alembic migrations. Use db-stamp first on existing DBs.' },
  { step: '5', title: 'Start the bot', cmd: 'make paper', note: 'Run in paper mode first. Switch to live when ready.' },
  { step: '6', title: 'Start the dashboard API', cmd: 'uvicorn api.main:app --port 8000 --reload', note: 'Runs alongside the bot.' },
  { step: '7', title: 'Start the frontend', cmd: 'cd frontend && npm run dev', note: 'Opens at http://localhost:5173' },
]

const ENV_VARS = [
  { key: 'APP_ENV', values: 'development | paper | semi-auto | live', desc: 'Sets the running mode' },
  { key: 'KITE_API_KEY', values: 'string', desc: 'Zerodha API key' },
  { key: 'KITE_API_SECRET', values: 'string', desc: 'Zerodha API secret' },
  { key: 'KITE_USER_ID', values: 'string', desc: 'Zerodha client ID' },
  { key: 'KITE_PASSWORD', values: 'string', desc: 'Zerodha password (for auto-login)' },
  { key: 'KITE_TOTP_SECRET', values: 'string', desc: '2FA TOTP secret (base32)' },
  { key: 'ANTHROPIC_API_KEY', values: 'string', desc: 'Claude AI API key' },
  { key: 'TELEGRAM_BOT_TOKEN', values: 'string', desc: 'Telegram bot token from @BotFather' },
  { key: 'TELEGRAM_CHAT_ID', values: 'string', desc: 'Your Telegram chat/group ID' },
  { key: 'TELEGRAM_AUTHORIZED_IDS', values: '123,456', desc: 'Comma-separated user IDs that can approve semi-auto trades' },
  { key: 'APPROVAL_TIMEOUT_SECS', values: '60', desc: 'Seconds to wait for trade approval before auto-reject' },
  { key: 'TOTAL_CAPITAL', values: '100000', desc: 'Capital in INR (used for position sizing)' },
  { key: 'DAILY_LOSS_LIMIT_PCT', values: '2.0', desc: 'Daily loss % at which trading halts (default 2%)' },
  { key: 'MAX_RISK_PER_TRADE_PCT', values: '2.0', desc: 'Max capital % risked per trade' },
]

// ─── Components ───────────────────────────────────────────────────────────────

function PhaseCard({ data }) {
  const isDone    = data.status === 'done'
  const isPending = data.status === 'pending'

  return (
    <div className={`card p-4 border-l-2 ${isDone ? 'border-l-green-trade/60' : 'border-l-border'}`}>
      <div className="flex items-start justify-between gap-3 mb-3">
        <div className="flex items-center gap-2.5">
          <span className={`font-mono text-xs px-1.5 py-0.5 rounded border font-semibold ${
            isDone ? 'bg-green-trade/10 text-green-trade border-green-trade/30' :
            'bg-bg-hover text-text-muted border-border'
          }`}>
            Phase {data.phase}
          </span>
          <span className="font-medium text-sm text-text-primary">{data.label}</span>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {data.env && (
            <code className="text-2xs font-mono px-2 py-0.5 rounded bg-bg-hover border border-border text-text-secondary">
              {data.env}
            </code>
          )}
          <span className={`text-2xs font-mono ${isDone ? 'text-green-trade' : 'text-text-muted'}`}>
            {data.date}
          </span>
        </div>
      </div>
      <ul className="space-y-1">
        {data.features.map((f, i) => (
          <li key={i} className="flex items-start gap-2 text-xs text-text-secondary">
            <span className={`mt-0.5 shrink-0 ${isDone ? 'text-green-trade' : 'text-border'}`}>
              {isDone ? '✓' : '○'}
            </span>
            {f}
          </li>
        ))}
      </ul>
    </div>
  )
}

function Section({ title, children }) {
  return (
    <section className="space-y-3">
      <h2 className="text-xs font-semibold text-text-muted uppercase tracking-widest border-b border-border pb-2">
        {title}
      </h2>
      {children}
    </section>
  )
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function Changelog() {
  return (
    <div className="p-5 space-y-8 animate-fade-in max-w-screen-lg mx-auto">

      {/* Header */}
      <div>
        <h1 className="text-base font-semibold text-text-primary mb-0.5">Changelog &amp; Guide</h1>
        <p className="text-sm text-text-muted">Feature history, quick-start, and configuration reference.</p>
      </div>

      {/* Quick Start */}
      <Section title="Quick Start">
        <div className="space-y-2">
          {QUICK_START.map((s) => (
            <div key={s.step} className="flex items-start gap-3">
              <span className="w-5 h-5 rounded-full bg-blue-trade/10 border border-blue-trade/30 text-blue-trade text-2xs font-mono font-bold flex items-center justify-center shrink-0 mt-0.5">
                {s.step}
              </span>
              <div className="flex-1 min-w-0">
                <div className="text-sm text-text-primary font-medium">{s.title}</div>
                <code className="text-2xs font-mono text-text-secondary bg-bg-hover border border-border px-2 py-0.5 rounded inline-block mt-0.5">
                  {s.cmd}
                </code>
                <div className="text-xs text-text-muted mt-0.5">{s.note}</div>
              </div>
            </div>
          ))}
        </div>
      </Section>

      {/* Modes */}
      <Section title="Bot Modes">
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {MODES.map((m) => (
            <div key={m.key} className="card p-4 space-y-2">
              <div className="flex items-center gap-2">
                <span className={`badge border font-mono ${m.color}`}>{m.label}</span>
                <code className="text-2xs font-mono text-text-muted bg-bg-hover border border-border px-1.5 py-0.5 rounded">
                  {m.cmd}
                </code>
              </div>
              <p className="text-xs text-text-secondary leading-relaxed">{m.desc}</p>
            </div>
          ))}
        </div>
        <div className="card p-3 bg-yellow-trade/5 border-yellow-trade/20">
          <p className="text-xs text-yellow-trade">
            <strong>Semi-auto setup:</strong> Set <code className="font-mono bg-yellow-trade/10 px-1 rounded">TELEGRAM_AUTHORIZED_IDS=your_telegram_id</code> to restrict who can approve.
            Find your ID by messaging <code className="font-mono bg-yellow-trade/10 px-1 rounded">@userinfobot</code> on Telegram.
          </p>
        </div>
      </Section>

      {/* Phase History */}
      <Section title="Feature Changelog">
        <div className="space-y-3">
          {PHASES.filter(p => p.status === 'done')
            .sort((a, b) => b.phase - a.phase)
            .map(p => <PhaseCard key={p.phase} data={p} />)}
        </div>
        <h3 className="text-xs font-semibold text-text-muted uppercase tracking-widest mt-4 mb-2">Upcoming</h3>
        {PHASES.filter(p => p.status === 'pending').map(p => <PhaseCard key={p.phase} data={p} />)}
      </Section>

      {/* Environment Variables */}
      <Section title="Environment Variables (.env)">
        <div className="card overflow-hidden">
          <table className="w-full">
            <thead>
              <tr>
                <th className="th">Variable</th>
                <th className="th">Value / Format</th>
                <th className="th hidden md:table-cell">Description</th>
              </tr>
            </thead>
            <tbody>
              {ENV_VARS.map((e) => (
                <tr key={e.key} className="table-row-hover">
                  <td className="td">
                    <code className="font-mono text-xs text-blue-trade">{e.key}</code>
                  </td>
                  <td className="td">
                    <code className="font-mono text-xs text-text-secondary">{e.values}</code>
                  </td>
                  <td className="td text-xs text-text-muted hidden md:table-cell">{e.desc}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      {/* API Reference */}
      <Section title="API Endpoints">
        <div className="card overflow-hidden">
          <table className="w-full">
            <thead>
              <tr>
                <th className="th">Method</th>
                <th className="th">Path</th>
                <th className="th hidden sm:table-cell">Description</th>
              </tr>
            </thead>
            <tbody>
              {[
                ['GET',  '/api/positions',      'All currently open trades'],
                ['GET',  '/api/trades',          'Paginated trade history (?page=1&per_page=50)'],
                ['GET',  '/api/trades/{id}',     'Single trade detail with orders'],
                ['GET',  '/api/pnl/today',       'Today\'s aggregated P&L summary'],
                ['GET',  '/api/pnl/history',     'Daily P&L for last N days (?days=30)'],
                ['GET',  '/api/signals/recent',  'Latest signals from Redis cache'],
                ['GET',  '/api/bot/status',      'Bot health, mode, and stats'],
                ['POST', '/api/bot/square-off',  'Emergency close all intraday positions'],
                ['WS',   '/ws',                  'Live feed: signals + trade events + P&L updates'],
              ].map(([method, path, desc]) => (
                <tr key={path} className="table-row-hover">
                  <td className="td">
                    <span className={`badge font-mono text-2xs border ${
                      method === 'GET'  ? 'bg-green-trade/10 text-green-trade border-green-trade/30' :
                      method === 'POST' ? 'bg-yellow-trade/10 text-yellow-trade border-yellow-trade/30' :
                      'bg-blue-trade/10 text-blue-trade border-blue-trade/30'
                    }`}>
                      {method}
                    </span>
                  </td>
                  <td className="td">
                    <code className="font-mono text-xs text-text-secondary">{path}</code>
                  </td>
                  <td className="td text-xs text-text-muted hidden sm:table-cell">{desc}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      {/* Footer */}
      <div className="text-center py-4 text-xs text-text-muted">
        TradeBot · Built with Zerodha Kite Connect, Claude AI, FastAPI, React
      </div>
    </div>
  )
}

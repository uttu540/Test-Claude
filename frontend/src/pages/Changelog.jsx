// ─── Changelog & Guide ────────────────────────────────────────────────────────
// Static reference page: feature history, quick-start, mode guide, API ref.

const PHASES = [
  {
    phase: 8,
    label: 'Runtime Config & Settings UI',
    date: 'Apr 2026',
    status: 'done',
    features: [
      'config/bot_config.py — 35 parameters stored in Redis, applied without restart',
      'Settings page with toggles, number inputs, and signal pill selectors',
      'Strategy on/off switches, indicator periods, timeframe weights, regime caps',
      'GET/POST /api/config and GET /api/config/schema endpoints',
      'Makefile uses venv automatically — no manual activation needed',
      'Daily Guide tab in Settings with startup, modes, and Telegram command reference',
    ],
    env: null,
  },
  {
    phase: 7,
    label: 'Frontend Overhaul',
    date: 'Apr 2026',
    status: 'done',
    features: [
      'Dedicated Positions page with card layout, R:R ratio, risk amount',
      'Dedicated Signals page with direction filter, symbol search, confidence bars, indicators popover',
      'P&L History page with daily bar chart and trade table',
      'Fixed field name bugs: trading_symbol, entry_quantity, planned_stop_loss, planned_target_1',
      'Fixed signal payload keys: signal (not signal_type), timestamp (not time)',
      'Navbar UI polish, IST clock, WS status dot, Square Off modal',
    ],
    env: null,
  },
  {
    phase: 6,
    label: 'Broker Abstraction & Migrations',
    date: 'Apr 2026',
    status: 'done',
    features: [
      'BrokerInterface ABC — swap brokers with one config change',
      'ZerodhaOrderManager and MockOrderManager implementations',
      'Alembic migrations for schema versioning',
      'honcho Procfile — one command starts bot + API + frontend',
      'make setup / make start one-command workflow',
    ],
    env: null,
  },
  {
    phase: 5,
    label: 'Human Approval Gate',
    date: 'Apr 2026',
    status: 'done',
    features: [
      'Semi-auto mode — every trade requires Telegram ✅/❌ before execution',
      'Configurable timeout (default 60 s) — auto-rejects on expiry',
      'Inline keyboard callbacks via python-telegram-bot v21',
      'TELEGRAM_AUTHORIZED_IDS whitelist — only listed users can approve',
      'Approval message auto-edits to show final outcome',
    ],
    env: 'APP_ENV=semi-auto',
  },
  {
    phase: 4,
    label: 'Trade Lifecycle & P&L Tracking',
    date: 'Apr 2026',
    status: 'done',
    features: [
      'TradeLifecycleManager monitors open positions every 5 s',
      'Detects SL hits, target hits, and time exits (3:12 PM IST)',
      'Calculates gross P&L, brokerage, STT, GST, exchange charges, SEBI charges, stamp duty',
      'Kill switch: halts new orders once daily loss limit is breached',
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
      'Claude AI validates each signal before order placement',
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
      'PostgreSQL + TimescaleDB schema with Alembic migrations',
      'Redis for real-time tick cache and regime state',
      'FastAPI REST + WebSocket dashboard API',
      'Multi-user Telegram notifications for all trade events',
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
      'GROWW_API_KEY / GROWW_API_SECRET config already stubbed',
    ],
    env: null,
  },
]

const MODES = [
  {
    key: 'development',
    label: 'DEV',
    color: 'text-yellow-trade border-yellow-trade/30 bg-yellow-trade/10',
    cmd: 'make start',
    desc: 'Mock market feed, paper orders. No API key needed. Safe for local testing.',
  },
  {
    key: 'paper',
    label: 'PAPER',
    color: 'text-cyan-trade border-cyan-trade/30 bg-cyan-trade/10',
    cmd: 'make start-paper',
    desc: 'Real Zerodha WebSocket feed, simulated orders. Validates signals against live data without risking capital.',
  },
  {
    key: 'semi-auto',
    label: 'SEMI-AUTO',
    color: 'text-purple-400 border-purple-500/30 bg-purple-500/10',
    cmd: 'make start-semi-auto',
    desc: 'Real orders on Zerodha, but every trade requires Telegram ✅ before execution. Requires TELEGRAM_AUTHORIZED_IDS.',
  },
  {
    key: 'live',
    label: 'LIVE',
    color: 'text-red-trade border-red-trade/30 bg-red-trade/10',
    cmd: 'make start-live',
    desc: 'Fully automated live trading. Real money, no human gate. Terminal confirmation required.',
  },
]

const QUICK_START = [
  {
    step: '1',
    title: 'Configure environment',
    cmd: 'cp .env.example .env',
    note: 'Fill in KITE_API_KEY, ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN and other credentials.',
  },
  {
    step: '2',
    title: 'First-time setup',
    cmd: 'make setup',
    note: 'Creates venv, installs Python deps, starts Docker (PostgreSQL + Redis), runs DB migrations, installs frontend npm packages.',
  },
  {
    step: '3',
    title: 'Start the bot',
    cmd: 'make start',
    note: 'Starts bot + API + dashboard together via honcho. Dashboard at http://localhost:5173',
  },
]

const ENV_VARS = [
  { key: 'APP_ENV', values: 'development | paper | semi-auto | live', desc: 'Sets the running mode (set automatically by make start-*)' },
  { key: 'KITE_API_KEY', values: 'string', desc: 'Zerodha API key' },
  { key: 'KITE_API_SECRET', values: 'string', desc: 'Zerodha API secret' },
  { key: 'KITE_USER_ID', values: 'string', desc: 'Zerodha client ID' },
  { key: 'KITE_PASSWORD', values: 'string', desc: 'Zerodha password (for automated daily re-auth)' },
  { key: 'KITE_TOTP_SECRET', values: 'string', desc: '2FA TOTP secret (base32) for automated login' },
  { key: 'ANTHROPIC_API_KEY', values: 'string', desc: 'Claude AI API key for signal validation' },
  { key: 'CLAUDE_MODEL', values: 'claude-opus-4-6', desc: 'Claude model to use (default: claude-opus-4-6)' },
  { key: 'TELEGRAM_BOT_TOKEN', values: 'string', desc: 'Bot token from @BotFather' },
  { key: 'TELEGRAM_CHAT_ID', values: 'integer', desc: 'Single chat ID for notifications (legacy)' },
  { key: 'TELEGRAM_CHAT_IDS', values: '111,222,-100333', desc: 'Comma-separated IDs for multi-user notifications (overrides TELEGRAM_CHAT_ID)' },
  { key: 'TELEGRAM_AUTHORIZED_IDS', values: '123,456', desc: 'User IDs allowed to approve trades and use bot commands' },
  { key: 'APPROVAL_TIMEOUT_SECS', values: '60', desc: 'Seconds to wait for approval before auto-reject' },
  { key: 'TOTAL_CAPITAL', values: '100000', desc: 'Capital in INR used for position sizing' },
  { key: 'DAILY_LOSS_LIMIT_PCT', values: '2.0', desc: 'Daily loss % at which all trading halts (default 2% = ₹2,000 on ₹1L capital)' },
  { key: 'MAX_RISK_PER_TRADE_PCT', values: '2.0', desc: 'Max capital % risked per individual trade' },
  { key: 'MAX_OPEN_POSITIONS', values: '8', desc: 'Maximum concurrent open positions' },
  { key: 'NEWS_API_KEY', values: 'string', desc: 'NewsAPI.org key for sentiment analysis (optional)' },
]

// ─── Components ───────────────────────────────────────────────────────────────

function PhaseCard({ data }) {
  const isDone = data.status === 'done'

  return (
    <div className={`card p-4 border-l-2 ${isDone ? 'border-l-green-trade/60' : 'border-l-border'}`}>
      <div className="flex items-start justify-between gap-3 mb-3">
        <div className="flex items-center gap-2.5">
          <span className={`font-mono text-xs px-1.5 py-0.5 rounded border font-semibold ${
            isDone
              ? 'bg-green-trade/10 text-green-trade border-green-trade/30'
              : 'bg-bg-hover text-text-muted border-border'
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
      <ul className="space-y-1.5">
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
      <h2 className="text-xs font-semibold text-text-muted uppercase tracking-widest border-b border-border pb-2.5">
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
        <h1 className="text-base font-semibold text-text-primary mb-0.5">Guide &amp; Changelog</h1>
        <p className="text-sm text-text-muted">Quick-start, mode reference, environment variables, and feature history.</p>
      </div>

      {/* Quick Start */}
      <Section title="Quick Start">
        <div className="space-y-3">
          {QUICK_START.map((s) => (
            <div key={s.step} className="flex items-start gap-3">
              <span className="w-5 h-5 rounded-full bg-blue-trade/10 border border-blue-trade/30 text-blue-trade text-2xs font-mono font-bold flex items-center justify-center shrink-0 mt-0.5">
                {s.step}
              </span>
              <div className="flex-1 min-w-0">
                <div className="text-sm text-text-primary font-medium mb-1">{s.title}</div>
                <code className="text-xs font-mono text-text-secondary bg-bg-hover border border-border px-2.5 py-1 rounded-md inline-block">
                  {s.cmd}
                </code>
                <div className="text-xs text-text-muted mt-1.5">{s.note}</div>
              </div>
            </div>
          ))}
        </div>
        <div className="card p-3 bg-blue-trade/5 border-blue-trade/20 mt-1">
          <p className="text-xs text-text-secondary">
            <span className="text-blue-trade font-medium">All three processes</span> (bot, API, dashboard) start together via honcho.
            Dashboard → <code className="font-mono bg-bg-hover px-1 rounded text-2xs">localhost:5173</code>&nbsp;
            API → <code className="font-mono bg-bg-hover px-1 rounded text-2xs">localhost:8000</code>&nbsp;
            Docs → <code className="font-mono bg-bg-hover px-1 rounded text-2xs">localhost:8000/docs</code>
          </p>
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
            <strong>Semi-auto setup:</strong> Set{' '}
            <code className="font-mono bg-yellow-trade/10 px-1 rounded">TELEGRAM_AUTHORIZED_IDS=your_telegram_id</code>.
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
        <div className="mt-4 mb-2">
          <h3 className="text-xs font-semibold text-text-muted uppercase tracking-widest">Upcoming</h3>
        </div>
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
                ['GET',  '/api/trades/{id}',     'Single trade detail with all orders'],
                ['GET',  '/api/pnl/today',       'Today\'s aggregated P&L summary'],
                ['GET',  '/api/pnl/history',     'Daily P&L for last N days (?days=30)'],
                ['GET',  '/api/signals/recent',  'Latest signal per symbol from Redis cache'],
                ['GET',  '/api/bot/status',      'Bot health, mode, capital, and today\'s stats'],
                ['POST', '/api/bot/square-off',  'Emergency close all open intraday positions'],
                ['GET',  '/api/config',           'Current bot configuration (all 35 parameters)'],
                ['POST', '/api/config',           'Update config — changes apply on next signal cycle'],
                ['GET',  '/api/config/schema',    'Parameter schema with types, ranges, and labels'],
                ['WS',   '/ws',                  'Live feed: signals, position updates, P&L'],
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
      <div className="text-center py-4 text-xs text-text-muted border-t border-border">
        TradeBot · Zerodha Kite Connect · Claude AI · FastAPI · React · TimescaleDB
      </div>
    </div>
  )
}

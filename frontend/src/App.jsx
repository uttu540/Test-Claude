import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { useWebSocket } from './ws'
import Navbar from './components/Navbar'
import Dashboard from './pages/Dashboard'
import Trades from './pages/Trades'
import PnLHistory from './pages/PnLHistory'
import Changelog from './pages/Changelog'
import Positions from './pages/Positions'
import Signals from './pages/Signals'
import Settings from './pages/Settings'

/**
 * Inner app — rendered inside BrowserRouter so hooks can use router context.
 * A single WebSocket is opened here purely to drive the Navbar status dot.
 * Individual pages open their own connections for live data.
 */
function AppInner() {
  const { wsStatus } = useWebSocket(null)

  return (
    <div className="min-h-screen bg-bg-primary">
      <Navbar wsStatus={wsStatus} />
      <main>
        <Routes>
          <Route path="/"           element={<Dashboard />} />
          <Route path="/positions"  element={<Positions />} />
          <Route path="/signals"    element={<Signals />} />
          <Route path="/trades"     element={<Trades />} />
          <Route path="/pnl"        element={<PnLHistory />} />
          <Route path="/changelog"  element={<Changelog />} />
          <Route path="/settings"   element={<Settings />} />
          <Route
            path="*"
            element={
              <div className="flex flex-col items-center justify-center min-h-[60vh] gap-4">
                <span className="font-mono text-7xl font-bold text-border select-none">404</span>
                <p className="text-sm text-text-muted">This page doesn't exist.</p>
                <a
                  href="/"
                  className="btn bg-bg-card border border-border text-text-secondary hover:text-text-primary"
                >
                  ← Back to Dashboard
                </a>
              </div>
            }
          />
        </Routes>
      </main>
    </div>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <AppInner />
    </BrowserRouter>
  )
}

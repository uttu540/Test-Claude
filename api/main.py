"""
api/main.py
────────────
FastAPI REST + WebSocket API for the trading dashboard.

Endpoints:
  GET  /api/positions          — open positions
  GET  /api/trades             — trade history (paginated)
  GET  /api/trades/{id}        — single trade detail
  GET  /api/pnl/today          — today's P&L summary
  GET  /api/signals/recent     — recent signals from Redis
  GET  /api/bot/status         — bot health + stats
  POST /api/bot/square-off     — emergency square off all intraday
  WS   /ws                     — live feed: signals + trade events

Run alongside the bot:
  uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
"""
from __future__ import annotations

import json
import asyncio
from datetime import date, datetime
from typing import Any
from uuid import UUID

import structlog
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from config.settings import settings
from database.connection import get_db_session, get_redis, init_db

log = structlog.get_logger(__name__)

app = FastAPI(title="Trading Bot API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Tighten in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── WebSocket connection manager ───────────────────────────────────────────────

class ConnectionManager:
    def __init__(self) -> None:
        self._clients: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws) if hasattr(self._clients, "discard") else None
        if ws in self._clients:
            self._clients.remove(ws)

    async def broadcast(self, data: dict) -> None:
        dead = []
        for client in self._clients:
            try:
                await client.send_json(data)
            except Exception:
                dead.append(client)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


# ── Startup ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup() -> None:
    await init_db()
    asyncio.create_task(_redis_broadcast_loop())


async def _redis_broadcast_loop() -> None:
    """Poll Redis for new signals and broadcast to WebSocket clients."""
    redis = get_redis()
    seen: set[str] = set()
    while True:
        try:
            keys = await redis.keys("signal:latest:*")
            for key in keys:
                raw = await redis.get(key)
                if raw and key not in seen:
                    seen.add(key)
                    data = json.loads(raw)
                    await manager.broadcast({"type": "signal", "data": data})
            await asyncio.sleep(2)
        except Exception as e:
            log.warning("api.broadcast_error", error=str(e))
            await asyncio.sleep(5)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _row_to_dict(row) -> dict:
    return dict(row._mapping)


# ── Routes: Positions ──────────────────────────────────────────────────────────

@app.get("/api/positions")
async def get_positions() -> list[dict]:
    """All currently open trades."""
    async for session in get_db_session():
        result = await session.execute(
            text("""
                SELECT
                    id, trading_symbol, exchange, direction, strategy_name,
                    entry_price, entry_quantity, entry_time,
                    planned_stop_loss, planned_target_1,
                    initial_risk_amount, risk_reward_planned, broker, status
                FROM trades
                WHERE status = 'OPEN'
                ORDER BY entry_time DESC
            """)
        )
        rows = result.fetchall()
        return [_row_to_dict(r) for r in rows]
    return []


# ── Routes: Trades ─────────────────────────────────────────────────────────────

@app.get("/api/trades")
async def get_trades(limit: int = 50, offset: int = 0) -> list[dict]:
    """Paginated trade history, most recent first."""
    async for session in get_db_session():
        result = await session.execute(
            text("""
                SELECT
                    id, trading_symbol, direction, strategy_name,
                    entry_price, entry_quantity, entry_time,
                    exit_price, exit_time, exit_reason,
                    gross_pnl, net_pnl, risk_reward_actual,
                    planned_stop_loss, planned_target_1,
                    broker, status
                FROM trades
                ORDER BY entry_time DESC
                LIMIT :limit OFFSET :offset
            """),
            {"limit": limit, "offset": offset},
        )
        return [_row_to_dict(r) for r in result.fetchall()]
    return []


@app.get("/api/trades/{trade_id}")
async def get_trade(trade_id: str) -> dict:
    """Full detail for a single trade including all orders."""
    async for session in get_db_session():
        result = await session.execute(
            text("SELECT * FROM trades WHERE id = :id"),
            {"id": trade_id},
        )
        row = result.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Trade not found")

        orders = await session.execute(
            text("SELECT * FROM orders WHERE parent_trade_id = :tid ORDER BY placed_at"),
            {"tid": trade_id},
        )
        trade = _row_to_dict(row)
        trade["orders"] = [_row_to_dict(o) for o in orders.fetchall()]
        return trade
    raise HTTPException(status_code=404, detail="Trade not found")


# ── Routes: P&L ───────────────────────────────────────────────────────────────

@app.get("/api/pnl/today")
async def get_today_pnl() -> dict:
    """Today's aggregated P&L summary."""
    today = date.today()
    async for session in get_db_session():
        result = await session.execute(
            text("""
                SELECT
                    COUNT(*)                                         AS total_trades,
                    COUNT(*) FILTER (WHERE net_pnl > 0)             AS winning,
                    COUNT(*) FILTER (WHERE net_pnl < 0)             AS losing,
                    COUNT(*) FILTER (WHERE status = 'OPEN')         AS open_trades,
                    COALESCE(SUM(net_pnl) FILTER (WHERE status = 'CLOSED'), 0) AS net_pnl,
                    COALESCE(SUM(gross_pnl) FILTER (WHERE status = 'CLOSED'), 0) AS gross_pnl,
                    COALESCE(SUM(brokerage + stt + exchange_charges + gst)
                             FILTER (WHERE status = 'CLOSED'), 0)   AS total_charges
                FROM trades
                WHERE DATE(entry_time) = :today
            """),
            {"today": today},
        )
        row = result.fetchone()
        data = _row_to_dict(row) if row else {}
        data["trading_date"] = today.isoformat()
        data["daily_loss_limit"] = settings.daily_loss_limit_inr
        data["capital"] = settings.total_capital
        data["pnl_pct"] = (
            float(data.get("net_pnl", 0)) / settings.total_capital * 100
            if settings.total_capital else 0
        )
        return data
    return {}


@app.get("/api/pnl/history")
async def get_pnl_history(days: int = 30) -> list[dict]:
    """Daily P&L for the last N days."""
    async for session in get_db_session():
        result = await session.execute(
            text("""
                SELECT
                    DATE(entry_time)    AS trading_date,
                    COUNT(*)            AS total_trades,
                    SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) AS wins,
                    COALESCE(SUM(net_pnl), 0) AS net_pnl
                FROM trades
                WHERE status = 'CLOSED'
                  AND entry_time >= NOW() - INTERVAL ':days days'
                GROUP BY DATE(entry_time)
                ORDER BY trading_date DESC
            """),
            {"days": days},
        )
        return [_row_to_dict(r) for r in result.fetchall()]
    return []


# ── Routes: Signals ────────────────────────────────────────────────────────────

@app.get("/api/signals/recent")
async def get_recent_signals() -> list[dict]:
    """Latest signal per symbol from Redis."""
    redis = get_redis()
    keys  = await redis.keys("signal:latest:*")
    signals = []
    for key in keys:
        raw = await redis.get(key)
        if raw:
            signals.append(json.loads(raw))
    signals.sort(key=lambda s: s.get("timestamp", ""), reverse=True)
    return signals


# ── Routes: Bot Status ────────────────────────────────────────────────────────

@app.get("/api/bot/status")
async def get_bot_status() -> dict:
    """Bot health, mode, and today's stats."""
    redis = get_redis()
    regime = await redis.get("market:regime") or "UNKNOWN"
    pnl    = await get_today_pnl()

    return {
        "status":           "running",
        "env":              settings.app_env.value,
        "capital":          settings.total_capital,
        "daily_loss_limit": settings.daily_loss_limit_inr,
        "max_positions":    settings.max_open_positions,
        "market_regime":    regime,
        "today":            pnl,
        "timestamp":        datetime.now().isoformat(),
    }


# ── Routes: Controls ──────────────────────────────────────────────────────────

@app.post("/api/bot/square-off")
async def square_off_all() -> dict:
    """Emergency square off all intraday positions."""
    if settings.is_live:
        from services.execution.zerodha.order_manager import OrderManager
        om = OrderManager()
        await om.square_off_all_intraday()
    await manager.broadcast({"type": "system", "data": {"event": "square_off_triggered"}})
    return {"status": "ok", "message": "Square off initiated"}


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)
    try:
        # Send current state on connect
        positions = await get_positions()
        pnl       = await get_today_pnl()
        await ws.send_json({"type": "init", "data": {"positions": positions, "pnl": pnl}})

        while True:
            await ws.receive_text()   # Keep-alive ping from client
    except WebSocketDisconnect:
        manager.disconnect(ws)

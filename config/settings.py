"""
config/settings.py
──────────────────
Central configuration loaded from environment variables (.env file).
All services import from here — never read os.environ directly elsewhere.
"""
from __future__ import annotations

from enum import Enum
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnv(str, Enum):
    DEVELOPMENT = "development"
    PAPER       = "paper"       # Paper trading: real signals, simulated orders
    SEMI_AUTO   = "semi-auto"   # Human approval required before each live trade
    LIVE        = "live"        # Full automation — real money


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ───────────────────────────────────────────────────────────────────
    app_env: AppEnv = AppEnv.DEVELOPMENT
    log_level: str = "INFO"

    # ── Capital & Risk ────────────────────────────────────────────────────────
    total_capital: float = 100_000.0          # ₹1,00,000
    max_risk_per_trade_pct: float = 2.0       # Risk max 2% = ₹2,000 per trade
    daily_loss_limit_pct: float = 2.0         # Halt if day loss > 2% = ₹2,000
    max_open_positions: int = 8
    max_position_size_pct: float = 15.0       # Max 15% in a single position

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://trading:trading_secret@localhost:5432/trading_bot"
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = "redis://:redis_secret@localhost:6379/0"

    # ── Zerodha ───────────────────────────────────────────────────────────────
    kite_api_key: str = ""
    kite_api_secret: str = ""
    kite_user_id: str = ""
    kite_password: str = ""
    kite_totp_secret: str = ""

    # ── Groww (Phase 6) ───────────────────────────────────────────────────────
    groww_api_key: str = ""
    groww_api_secret: str = ""

    # ── Anthropic / Claude ────────────────────────────────────────────────────
    anthropic_api_key: str = ""
    claude_model: str = "claude-opus-4-6"

    # ── Telegram ──────────────────────────────────────────────────────────────
    telegram_bot_token: str = ""
    # Single chat ID (legacy / default)
    telegram_chat_id: str = ""
    # Multi-user: comma-separated chat IDs that receive all notifications.
    # If set, overrides telegram_chat_id.
    # e.g. TELEGRAM_CHAT_IDS=111111111,222222222,-100333333333
    # (-100... prefix = group/channel)
    telegram_chat_ids: str = ""
    # Comma-separated Telegram user IDs allowed to approve trades + use commands
    # e.g. TELEGRAM_AUTHORIZED_IDS=123456789,987654321
    # Leave empty = any user can interact (only safe on private bots)
    telegram_authorized_ids: str = ""
    # How long to wait for approval before auto-rejecting (seconds)
    approval_timeout_secs: int = 60

    # ── News API ──────────────────────────────────────────────────────────────
    news_api_key: str = ""

    # ── API / CORS ────────────────────────────────────────────────────────────
    # Comma-separated allowed origins for FastAPI CORS middleware.
    # Dev default: Vite dev server. Production: set your actual domain.
    # e.g. ALLOWED_ORIGINS=https://trading.yourdomain.com
    allowed_origins: str = "http://localhost:5173,http://localhost:3000"

    # ── Derived / Computed ────────────────────────────────────────────────────
    @property
    def is_live(self) -> bool:
        return self.app_env == AppEnv.LIVE

    @property
    def is_semi_auto(self) -> bool:
        return self.app_env == AppEnv.SEMI_AUTO

    @property
    def is_paper(self) -> bool:
        return self.app_env == AppEnv.PAPER

    @property
    def is_dev(self) -> bool:
        return self.app_env == AppEnv.DEVELOPMENT

    @property
    def uses_real_broker(self) -> bool:
        """True when orders go to a real broker (live or semi-auto)."""
        return self.app_env in (AppEnv.LIVE, AppEnv.SEMI_AUTO)

    @property
    def uses_simulated_broker(self) -> bool:
        """True when orders are simulated (dev or paper)."""
        return self.app_env in (AppEnv.DEVELOPMENT, AppEnv.PAPER)

    @property
    def authorized_telegram_ids(self) -> list[str]:
        """Parsed list of Telegram user IDs allowed to approve trades and use commands."""
        return [x.strip() for x in self.telegram_authorized_ids.split(",") if x.strip()]

    @property
    def notification_chat_ids(self) -> list[str]:
        """All chat IDs that receive bot notifications. Supports groups and individuals."""
        if self.telegram_chat_ids.strip():
            return [x.strip() for x in self.telegram_chat_ids.split(",") if x.strip()]
        if self.telegram_chat_id.strip():
            return [self.telegram_chat_id.strip()]
        return []

    @property
    def cors_origins(self) -> list[str]:
        """Parsed list of allowed CORS origins."""
        return [x.strip() for x in self.allowed_origins.split(",") if x.strip()]

    @property
    def max_risk_per_trade_inr(self) -> float:
        return self.total_capital * (self.max_risk_per_trade_pct / 100)

    @property
    def daily_loss_limit_inr(self) -> float:
        return self.total_capital * (self.daily_loss_limit_pct / 100)

    @property
    def max_position_size_inr(self) -> float:
        return self.total_capital * (self.max_position_size_pct / 100)

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        v = v.upper()
        if v not in allowed:
            raise ValueError(f"log_level must be one of {allowed}")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()


# Convenience alias — use this everywhere:  from config.settings import settings
settings = get_settings()

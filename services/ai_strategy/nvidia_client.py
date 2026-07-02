"""
services/ai_strategy/nvidia_client.py
───────────────────────────────────────
Thin async client for the NVIDIA NIM OpenAI-compatible endpoint (gpt-oss-*).

Purpose: a free-tier secondary LLM for OFFLINE research and evaluation —
pattern discovery on backtest data, strategy/code critique, and (optionally)
a cross-check against Claude's signal scoring. It is deliberately NOT wired
into the live trade-execution path; nothing here can place or gate a real
order. Trading decisions stay on the validated Claude pipeline.

The API key is read from settings (NVIDIA_API_KEY in .env) — never hardcode a
key in source. If a key was ever pasted into a chat/log, rotate it.

Usage:
    from services.ai_strategy.nvidia_client import get_nvidia_client
    client = get_nvidia_client()
    if client.enabled:
        text, reasoning = await client.complete("Find patterns in: ...")
"""
from __future__ import annotations

import asyncio

import structlog

from config.settings import settings

log = structlog.get_logger(__name__)


class NvidiaClient:
    """Async wrapper over the OpenAI-compatible NVIDIA NIM endpoint."""

    def __init__(self) -> None:
        self._client = None
        if not settings.nvidia_api_key:
            log.warning("nvidia_client.no_api_key", status="disabled")
            return
        try:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(
                base_url=settings.nvidia_base_url,
                api_key=settings.nvidia_api_key,
            )
        except Exception as e:  # openai not installed or bad config
            log.error("nvidia_client.init_failed", error=str(e))
            self._client = None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float = 1.0,
        top_p: float = 1.0,
        max_tokens: int = 4096,
        retries: int = 2,
    ) -> tuple[str, str | None]:
        """
        Single-shot completion. Returns (content, reasoning_content).

        gpt-oss models expose chain-of-thought via `reasoning_content`; it is
        returned separately so callers can log or discard it. Returns
        ("", None) on failure — never raises into the caller.
        """
        if not self._client:
            return "", None

        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = await self._client.chat.completions.create(
                    model=model or settings.nvidia_model,
                    messages=messages,
                    temperature=temperature,
                    top_p=top_p,
                    max_tokens=max_tokens,
                    stream=False,
                )
                msg = resp.choices[0].message
                content = msg.content or ""
                reasoning = getattr(msg, "reasoning_content", None)
                return content, reasoning
            except Exception as e:
                last_err = e
                # Back off on rate limits / transient errors.
                if attempt < retries:
                    await asyncio.sleep(2 ** attempt)

        log.error("nvidia_client.complete_failed", error=str(last_err))
        return "", None


_client: NvidiaClient | None = None


def get_nvidia_client() -> NvidiaClient:
    """Process-wide singleton."""
    global _client
    if _client is None:
        _client = NvidiaClient()
    return _client

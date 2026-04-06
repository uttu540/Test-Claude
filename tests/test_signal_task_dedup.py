"""
Tests for per-symbol signal task deduplication in main.py.

Verifies that concurrent _run_signals tasks for the same symbol are prevented
and a warning is logged when a trigger is skipped.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_main_state():
    """Reset module-level state between tests."""
    import main
    main._candle_buffer.clear()
    main._signal_tasks.clear()
    yield
    main._candle_buffer.clear()
    main._signal_tasks.clear()


@pytest.mark.asyncio
async def test_second_trigger_skipped_while_task_running():
    """If a signal task is already running for a symbol, the new trigger is skipped."""
    import main

    blocker = asyncio.Event()

    async def slow_signals(symbol: str) -> None:
        await blocker.wait()  # Blocks until we release it

    # Seed the candle buffer so _run_signals has data
    main._candle_buffer["NIFTY"] = {
        "15min": [{"open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100, "ts": i}
                  for i in range(30)]
    }

    with patch.object(main, "_run_signals", side_effect=slow_signals):
        # Build a fake OHLCVCandle
        candle = MagicMock()
        candle.trading_symbol = "NIFTY"
        candle.timeframe = "15min"
        candle.open = candle.high = candle.low = candle.close = candle.volume = 1
        candle.timestamp = 0

        # First trigger — should create a task
        main.on_candle_complete(candle)
        await asyncio.sleep(0)  # Let the event loop schedule the task
        assert "NIFTY" in main._signal_tasks
        assert not main._signal_tasks["NIFTY"].done()

        # Second trigger while first is still running — should be skipped
        with patch.object(main.log, "warning") as mock_warn:
            main.on_candle_complete(candle)
            await asyncio.sleep(0)

        mock_warn.assert_called_once()
        call_kwargs = mock_warn.call_args
        assert call_kwargs[0][0] == "signal.task_skipped"

        # Only one task should exist
        assert len([t for t in main._signal_tasks.values() if not t.done()]) == 1

        # Unblock and let the task finish
        blocker.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_task_registry_cleaned_up_after_completion():
    """Registry entry is removed once the task finishes."""
    import main

    async def fast_signals(symbol: str) -> None:
        pass

    main._candle_buffer["BANKNIFTY"] = {
        "15min": [{"open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100, "ts": i}
                  for i in range(30)]
    }

    with patch.object(main, "_run_signals", side_effect=fast_signals):
        candle = MagicMock()
        candle.trading_symbol = "BANKNIFTY"
        candle.timeframe = "15min"
        candle.open = candle.high = candle.low = candle.close = candle.volume = 1
        candle.timestamp = 0

        main.on_candle_complete(candle)
        await asyncio.sleep(0)   # Let task run and complete
        await asyncio.sleep(0)   # Let done-callback fire

        assert "BANKNIFTY" not in main._signal_tasks


@pytest.mark.asyncio
async def test_second_trigger_allowed_after_first_completes():
    """A new trigger is accepted once the previous task has finished."""
    import main

    call_count = 0

    async def counting_signals(symbol: str) -> None:
        nonlocal call_count
        call_count += 1

    main._candle_buffer["RELIANCE"] = {
        "15min": [{"open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100, "ts": i}
                  for i in range(30)]
    }

    with patch.object(main, "_run_signals", side_effect=counting_signals):
        candle = MagicMock()
        candle.trading_symbol = "RELIANCE"
        candle.timeframe = "15min"
        candle.open = candle.high = candle.low = candle.close = candle.volume = 1
        candle.timestamp = 0

        # First trigger
        main.on_candle_complete(candle)
        await asyncio.sleep(0)
        await asyncio.sleep(0)  # Task finishes + callback fires

        # Second trigger — should be allowed now
        main.on_candle_complete(candle)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert call_count == 2

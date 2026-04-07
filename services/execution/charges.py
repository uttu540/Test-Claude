"""
services/execution/charges.py
───────────────────────────────
Zerodha intraday equity charge calculator.

All rates as of 2025 (NSE MIS equity segment):
  Brokerage:        min(₹20, 0.03%) per side
  STT:              0.025% on sell-side turnover only
  Exchange (NSE):   0.00345% of total turnover
  SEBI charges:     ₹10 per crore of turnover (0.000001)
  GST:              18% on (brokerage + exchange charges + SEBI)
  Stamp duty:       0.003% on buy-side turnover (capped ₹1,500 for intraday)

Reference: https://zerodha.com/charges/
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChargeBreakdown:
    brokerage:        float
    stt:              float
    exchange_charges: float
    sebi_charges:     float
    gst:              float
    stamp_duty:       float
    total:            float


def calculate_intraday_charges(
    entry_price: float,
    exit_price:  float,
    quantity:    int,
    direction:   str = "LONG",   # "LONG" or "SHORT"
) -> ChargeBreakdown:
    """
    Compute the full Zerodha intraday equity charge breakdown for a round-trip trade.

    For a LONG trade:
        buy_value  = entry_price × qty
        sell_value = exit_price  × qty

    For a SHORT trade:
        buy_value  = exit_price  × qty   (buy-to-cover)
        sell_value = entry_price × qty   (initial sell)
    """
    if direction == "LONG":
        buy_value  = entry_price * quantity
        sell_value = exit_price  * quantity
    else:
        buy_value  = exit_price  * quantity
        sell_value = entry_price * quantity

    turnover = buy_value + sell_value

    # Brokerage: ₹20 flat or 0.03% of trade value — whichever is lower, per side
    brokerage = min(20.0, buy_value * 0.0003) + min(20.0, sell_value * 0.0003)

    # STT: 0.025% on sell side only (intraday equity delivery is different)
    stt = sell_value * 0.00025

    # Exchange transaction charges (NSE): 0.00345% of total turnover
    exchange_charges = turnover * 0.0000345

    # SEBI charges: ₹10 per crore = 0.000001 × turnover
    sebi_charges = turnover * 0.000001

    # GST: 18% on brokerage + exchange + SEBI
    gst = (brokerage + exchange_charges + sebi_charges) * 0.18

    # Stamp duty: 0.003% on buy-side turnover (intraday equity)
    stamp_duty = buy_value * 0.00003

    total = brokerage + stt + exchange_charges + sebi_charges + gst + stamp_duty

    return ChargeBreakdown(
        brokerage        = round(brokerage, 4),
        stt              = round(stt, 4),
        exchange_charges = round(exchange_charges, 4),
        sebi_charges     = round(sebi_charges, 4),
        gst              = round(gst, 4),
        stamp_duty       = round(stamp_duty, 4),
        total            = round(total, 4),
    )

"""Scan-ranking settings, trimmed from ButterflyGuy's equity_scan/config.py to just
what scanner.py needs. Universe/news/report/notifier fields are Phase 2 and dropped."""

from __future__ import annotations

from pydantic import BaseModel, Field


class EquityScanFilters(BaseModel):
    min_price: float = 5.0
    min_volume: int = 500_000
    prior_day_min_pct: float = 3.0
    premarket_min_gap_pct: float = 2.0
    min_rvol: float = 0.0  # 0 = disabled; e.g. 0.05 = 5% of 20d avg daily volume
    max_abs_pct: float | None = 50.0  # cap extreme % moves; None = off
    max_price_disagreement_pct: float | None = 5.0
    max_reference_price_deviation_pct: float | None = 25.0
    require_index_membership: bool = False  # symbol must be in sp500 or nq100


class EquityScanLimits(BaseModel):
    prior_gainers: int = 15
    prior_losers: int = 15
    premarket_gainers: int = 15
    premarket_losers: int = 15
    opening_focus: int = 12
    movers_per_bucket: int = 10


class EquityScanSettings(BaseModel):
    filters: EquityScanFilters = Field(default_factory=EquityScanFilters)
    limits: EquityScanLimits = Field(default_factory=EquityScanLimits)
    include_movers: bool = False
    movers_min_abs_pct: float = 1.0
    premarket_start_et: str = "04:00"

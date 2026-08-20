"""Tests for run.py's orchestration entry point. Only the pre-gateway early exit is
covered here — the rest of run_scan is an integration of every other module (already
tested individually) wired to live gateway/news/Discord I/O, which is out of scope
for a unit test; mirrors ButterflyGuy's own test_run_scan_skips_market_holiday
pattern, which stops at the same boundary."""

from __future__ import annotations

import datetime as dt

from equity_scanner import run
from equity_scanner.time_utils import EASTERN


async def test_run_scan_skips_market_holiday_before_gateway(monkeypatch):
    class AppSettingsShouldNotBeConstructed:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("AppSettings should not load on holidays")

    monkeypatch.setattr(
        run,
        "now_eastern",
        lambda: dt.datetime(2026, 7, 3, 8, 0, tzinfo=EASTERN),  # July 4th observed
    )
    monkeypatch.setattr(run, "AppSettings", AppSettingsShouldNotBeConstructed)

    messages = await run.run_scan(scan_config_path="missing.yaml")

    assert messages == []

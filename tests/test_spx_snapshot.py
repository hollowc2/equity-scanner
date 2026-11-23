import datetime as dt
import json
from contextlib import nullcontext

import httpx
import pytest
from schwab_gateway_sdk import GatewayMarketDataClient

from equity_scanner import spx_snapshot
from equity_scanner.provider import GatewayEquityDataProvider
from equity_scanner.scan_config import EquityScanSettings
from equity_scanner.time_utils import EASTERN
from equity_scanner.trade_setups import TapeQuote

NOW = dt.datetime(2026, 11, 20, 10, tzinfo=EASTERN)
OPEN = NOW.replace(hour=9, minute=30)
PRIOR = NOW - dt.timedelta(days=1)


def _bar(stamp, close, *, open_=None, low=None):
    return {
        "timestamp": stamp.isoformat(),
        "open": close if open_ is None else open_,
        "high": max(close, open_ or close) + 1,
        "low": close - 1 if low is None else low,
        "close": close,
        "volume": 100,
    }


def _contract(strike, side, mid, *, root="SPXW", age=0):
    return {
        "symbol": f"{root}   261120{side[0]}{strike:05d}000",
        "option_type": side,
        "expiration": NOW.date().isoformat(),
        "strike": strike,
        "bid": mid - 0.1,
        "ask": mid + 0.1,
        "mark": mid,
        "event_timestamp": (NOW - dt.timedelta(seconds=age)).isoformat(),
        "stale": False,
    }


async def test_snapshot_matches_open_and_volatility_rules_through_pinned_sdk(monkeypatch, tmp_path):
    monkeypatch.setattr(spx_snapshot, "now_eastern", lambda: NOW)
    calls = []
    stamp = NOW.isoformat()

    def handler(request):
        path, query = request.url.path, request.url.params
        calls.append(path)
        symbol = query.get("symbol")
        if path == "/v1/history":
            if query["frequency"] == "daily":
                close = {"$SPX": 6000, "$VIX": 16, "$VIX1D": 18, "$VIX3M": 20}[symbol]
                bars = [_bar(PRIOR, close), _bar(NOW, 300)]
            else:
                bars = [
                    _bar(
                        OPEN + dt.timedelta(minutes=minute),
                        6005 if symbol == "$SPX" else 600,
                    )
                    for minute in range(30)
                ]
                if symbol == "$SPX":
                    bars[0] = _bar(OPEN, 6005, open_=6010, low=5999)
            return httpx.Response(
                200,
                json={
                    "history": {
                        "symbol": symbol,
                        "frequency": query["frequency"],
                        "bars": bars,
                        "gateway_received_at": stamp,
                        "source": "fixture",
                        "stale": False,
                    }
                },
            )
        if path == "/v1/quotes":
            quotes = [
                {
                    "symbol": key,
                    "event_timestamp": stamp,
                    "gateway_received_at": stamp,
                    "source": "fixture",
                    "session": "regular",
                    "stale": False,
                    "last": price,
                    "close": price - 1,
                }
                for key, price in (("$SPX", 6005), ("$VIX", 17), ("SPY", 600))
            ]
            return httpx.Response(200, json={"quotes": quotes, "missing_symbols": []})
        assert path == "/v1/option-chain"
        assert symbol == "$SPX"
        assert query["expiration"] == NOW.date().isoformat()
        contracts = [
            _contract(6005, "CALL", 19),
            _contract(6005, "PUT", 20),
            _contract(6005, "PUT", 99, root="SPX"),  # AM settlement must be excluded.
            _contract(6000, "CALL", 10),
            _contract(6020, "CALL", 5),
            _contract(6040, "CALL", 1),
            _contract(6035, "CALL", 4, age=61),
        ]
        return httpx.Response(
            200,
            json={
                "option_chain": {
                    "symbol": symbol,
                    "expiration": NOW.date().isoformat(),
                    "call_contract_count": 5,
                    "put_contract_count": 2,
                    "strike_count": 5,
                    "contracts": contracts,
                    "event_timestamp": stamp,
                    "gateway_received_at": stamp,
                    "source": "fixture",
                    "stale": False,
                }
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(base_url="https://gateway.invalid", transport=transport) as http:
        client = GatewayMarketDataClient("https://gateway.invalid", "fake-key", client=http)
        snapshot = await spx_snapshot.collect_spx_snapshot(GatewayEquityDataProvider(client), NOW)
        monkeypatch.setattr(spx_snapshot, "AppSettings", lambda: None)
        monkeypatch.setattr(spx_snapshot, "build_gateway_client", lambda _: nullcontext(client))
        monkeypatch.setattr(
            spx_snapshot,
            "load_equity_scan_config",
            lambda _: EquityScanSettings(report_dir=str(tmp_path), universe_dir=str(tmp_path)),
        )
        monkeypatch.setattr(
            spx_snapshot,
            "load_universes",
            lambda *_, **__: {"sp500": ["$SPX", "$VIX", "SPY"]},
        )
        await spx_snapshot.run_spx_snapshot("unused", dry_run=True)

    assert calls.count("/v1/history") == 12
    assert calls.count("/v1/option-chain") == 2
    (archive,) = (tmp_path / "spx_snapshots").glob("*.json")
    assert json.loads(archive.read_text())["breadth"]["advances"] == 3
    assert "0DTE SPXW straddle" in archive.with_suffix(".md").read_text()
    assert snapshot.previous_close == 6000
    assert snapshot.cash_open == 6010
    assert snapshot.opening_gap_points == 10
    assert snapshot.gap_direction == "CALL"
    assert snapshot.gap_status == "Back inside the gap"
    assert snapshot.gap_filled is True
    assert snapshot.vix_bucket == "Goldilocks 1"
    assert [item.width for item in snapshot.width_context] == [20, 30, 40]
    first = snapshot.width_context[0]
    assert first.target == 6020
    assert first.sample is not None
    assert first.sample.midpoint == pytest.approx(1.0)
    assert first.sample.ask_premium_pct == pytest.approx(40.0)
    assert snapshot.prior_vix1d_vix == 1.125
    assert snapshot.prior_vix_vix3m == 0.8
    assert snapshot.straddle is not None and snapshot.straddle.midpoint == 39
    assert "0DTE SPXW straddle" in spx_snapshot.format_spx_snapshot(snapshot)


def test_missing_spot_does_not_hide_the_vix_width_bucket():
    vix = TapeQuote(24.5, None, NOW.isoformat())
    bucket, move, widths = spx_snapshot._vix_context(vix, None, None, {})
    assert bucket == "Goldilocks 2"
    assert move is None
    assert [(item.width, item.target) for item in widths] == [(40, None), (45, None), (50, None)]

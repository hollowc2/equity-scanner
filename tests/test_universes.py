"""Tests for universe loading/filtering, ported from ButterflyGuy's
test_equity_universes.py plus new coverage the Phase 2 scope explicitly calls for:
the network-fetching functions (fetch_sp500_tickers/fetch_sp500_sectors/
fetch_nq100_tickers/fetch_nasdaq_listed_symbols/fetch_nyse_listed_symbols) get
recorded-fixture tests here — small hand-built responses shaped like the real
GitHub/Wikipedia/nasdaqtrader.com payloads, with `urllib.request.urlopen`
monkeypatched so no live network call happens."""

from __future__ import annotations

import datetime as dt
import json

import pytest
from schwab_gateway_sdk import QuoteV1

from equity_scanner import universes

NASDAQ_SAMPLE = """\
Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
AAPL|Apple Inc. Common Stock|Q|N|N|100|N|N
QQQ|Invesco QQQ Trust, Series 1|G|N|N|100|Y|N
ZVZZT|Test Symbol|G|Y|N|100|N|N
BRK.A|Berkshire Hathaway Inc. Class A|Q|N|N|40|N|N
File Creation Time: 060820261200|||||||
"""

OTHERLISTED_SAMPLE = """\
ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
AAPL|Apple Inc. Common Stock|N|AAPL|N|100|N|AAPL
AAA|Alternative Access ETF|P|AAA|Y|100|N|AAA
ACCS|ACCESS Newswire Inc. Common Stock|A|ACCS|N|100|N|ACCS
ABR$F|Arbor Realty Preferred|N|ABRpF|N|100|N|ABR-F
ACHR.W|Archer Aviation Warrants|N|ACHR.WS|N|100|N|ACHR+
File Creation Time: 060820261200|||||||
"""

SP500_CSV_SAMPLE = (
    "Symbol,Security,GICS Sector,GICS Sub-Industry\n"
    "AAPL,Apple Inc.,Information Technology,Technology Hardware\n"
    "JPM,JPMorgan Chase,Financials,Diversified Banks\n"
)

NQ100_WIKI_SAMPLE = """
<table class="infobox"><tr><th>Constituents</th><td>102</td></tr></table>
<table class="wikitable sortable">
<tbody><tr><th id="ticker">Ticker</th><th id="company">Company</th></tr>
<tr><td id="aapl">AAPL</td><td><a href="/wiki/Apple">Apple Inc.</a></td></tr>
<tr><td><a href="/wiki/Microsoft">MSFT</a></td><td>Microsoft</td></tr>
<tr><td>AAPL</td><td>Apple Inc.</td></tr>
</tbody></table>
"""


def _quote(*, last: float | None, mark: float | None, close: float | None) -> QuoteV1:
    return QuoteV1(
        symbol="TEST",
        gateway_received_at=dt.datetime.now(dt.timezone.utc),
        source="test",
        last=last,
        mark=mark,
        close=close,
        stale=False,
    )


class _FakeUrlResponse:
    def __init__(self, text: str) -> None:
        self._data = text.encode()

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "_FakeUrlResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _fake_urlopen(responses: dict[str, str]):
    def _urlopen(req, timeout=30):  # noqa: ANN001, ARG001
        return _FakeUrlResponse(responses[req.full_url])

    return _urlopen


def test_parse_nasdaq_listed_text_filters_etfs_tests_and_preferreds():
    assert universes.parse_nasdaq_listed_text(NASDAQ_SAMPLE) == ["AAPL", "BRK.A"]


def test_parse_nyse_listed_text_keeps_nyse_common_stocks_only():
    assert universes.parse_nyse_listed_text(OTHERLISTED_SAMPLE) == ["AAPL"]


def test_fetch_nasdaq_listed_symbols_uses_recorded_fixture(monkeypatch):
    monkeypatch.setattr(
        universes.urllib.request,
        "urlopen",
        _fake_urlopen({universes.NASDAQ_LISTED_URL: NASDAQ_SAMPLE}),
    )
    assert universes.fetch_nasdaq_listed_symbols() == ["AAPL", "BRK.A"]


def test_fetch_nyse_listed_symbols_uses_recorded_fixture(monkeypatch):
    monkeypatch.setattr(
        universes.urllib.request,
        "urlopen",
        _fake_urlopen({universes.OTHER_LISTED_URL: OTHERLISTED_SAMPLE}),
    )
    assert universes.fetch_nyse_listed_symbols() == ["AAPL"]


def test_fetch_exchange_seed_map_prefers_nasdaq_over_nyse(monkeypatch):
    monkeypatch.setattr(
        universes.urllib.request,
        "urlopen",
        _fake_urlopen(
            {
                universes.NASDAQ_LISTED_URL: NASDAQ_SAMPLE,
                universes.OTHER_LISTED_URL: OTHERLISTED_SAMPLE,
            }
        ),
    )
    seed_map = universes.fetch_exchange_seed_map()
    assert seed_map["AAPL"] == "NASDAQ"  # present on both; NASDAQ wins (setdefault order)
    assert seed_map["BRK.A"] == "NASDAQ"


def test_fetch_sp500_tickers_and_sectors_use_recorded_fixture(monkeypatch):
    monkeypatch.setattr(
        universes.urllib.request,
        "urlopen",
        _fake_urlopen({universes.SP500_CSV_URL: SP500_CSV_SAMPLE}),
    )
    assert universes.fetch_sp500_tickers() == ["AAPL", "JPM"]
    assert universes.fetch_sp500_sectors() == {
        "AAPL": "Information Technology",
        "JPM": "Financials",
    }


def test_fetch_nq100_tickers_dedupes_using_recorded_fixture(monkeypatch):
    monkeypatch.setattr(
        universes.urllib.request,
        "urlopen",
        _fake_urlopen({universes.NQ100_WIKI_URL: NQ100_WIKI_SAMPLE}),
    )
    assert universes.fetch_nq100_tickers() == ["AAPL", "MSFT"]


def test_refresh_builtin_universes_dry_run_does_not_write(tmp_path, monkeypatch):
    monkeypatch.setattr(
        universes,
        "fetch_sp500_tickers",
        lambda: [f"SP{i}" for i in range(500)],
    )
    monkeypatch.setattr(
        universes,
        "fetch_nq100_tickers",
        lambda: [f"NQ{i}" for i in range(100)],
    )
    monkeypatch.setattr(
        universes,
        "fetch_sp500_sectors",
        lambda: {f"SP{i}": "Sector" for i in range(500)},
    )

    counts = universes.refresh_builtin_universes(tmp_path, dry_run=True)

    assert counts == {"sp500": 500, "nq100": 100, "sectors": 600}
    assert list(tmp_path.iterdir()) == []


def test_refresh_builtin_universes_preserves_files_when_fetch_is_implausibly_small(
    tmp_path, monkeypatch
):
    existing = {
        "sp500.txt": "OLD-SP500\n",
        "nq100.txt": "OLD-NQ100\n",
        "sectors.json": '{"OLD": "Sector"}\n',
    }
    for name, content in existing.items():
        (tmp_path / name).write_text(content)
    monkeypatch.setattr(
        universes,
        "fetch_sp500_tickers",
        lambda: [f"SP{i}" for i in range(500)],
    )
    monkeypatch.setattr(universes, "fetch_nq100_tickers", lambda: [])
    monkeypatch.setattr(
        universes,
        "fetch_sp500_sectors",
        lambda: {f"SP{i}": "Sector" for i in range(500)},
    )

    with pytest.raises(RuntimeError, match=r"Refusing to replace nq100: fetched 0"):
        universes.refresh_builtin_universes(tmp_path)

    for name, content in existing.items():
        assert (tmp_path / name).read_text() == content


def test_atomic_universe_write_preserves_existing_file_if_replace_fails(
    tmp_path, monkeypatch
):
    path = tmp_path / "nq100.txt"
    path.write_text("EXISTING\n")

    def fail_replace(_source, _destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(universes.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        universes.write_universe_file(path, ["AAPL", "MSFT"])

    assert path.read_text() == "EXISTING\n"
    assert list(tmp_path.iterdir()) == [path]


def test_load_universe_reads_ticker_files_and_strips_comments(tmp_path):
    universe_dir = tmp_path / "universes"
    universe_dir.mkdir()
    (universe_dir / "sp500.txt").write_text("aapl\n# comment\nmsft  # inline comment\n\n")
    custom_path = tmp_path / "custom.txt"
    custom_path.write_text("tsla\n")

    sp500 = universes.load_universe("sp500", universe_dir=universe_dir, custom_path=custom_path)
    custom = universes.load_universe("custom", universe_dir=universe_dir, custom_path=custom_path)
    nq100 = universes.load_universe("nq100", universe_dir=universe_dir, custom_path=custom_path)
    assert sp500 == ["AAPL", "MSFT"]
    assert custom == ["TSLA"]
    assert nq100 == []


def test_load_universes_and_build_symbol_map(tmp_path):
    universe_dir = tmp_path / "universes"
    universe_dir.mkdir()
    (universe_dir / "sp500.txt").write_text("AAPL\nMSFT\n")
    (universe_dir / "nq100.txt").write_text("AAPL\nGOOG\n")
    custom_path = tmp_path / "custom.txt"
    custom_path.write_text("")

    loaded = universes.load_universes(
        ["sp500", "nq100", "custom"], universe_dir=universe_dir, custom_watchlist=custom_path
    )
    symbol_map = universes.build_symbol_map(loaded)
    assert symbol_map["AAPL"] == {"sp500", "nq100"}
    assert symbol_map["MSFT"] == {"sp500"}
    assert symbol_map["GOOG"] == {"nq100"}


def test_extract_quote_price_prefers_last_then_mark_then_close():
    assert universes.extract_quote_price(_quote(last=9.0, mark=8.5, close=7.5)) == 9.0
    assert universes.extract_quote_price(_quote(last=None, mark=8.5, close=7.5)) == 8.5
    assert universes.extract_quote_price(_quote(last=None, mark=None, close=7.5)) == 7.5
    assert universes.extract_quote_price(_quote(last=None, mark=None, close=None)) is None


def test_filter_symbols_by_price():
    quotes = {
        "AAA": _quote(last=4.99, mark=None, close=None),
        "BBB": _quote(last=5.0, mark=None, close=None),
        "CCC": _quote(last=12.5, mark=None, close=None),
    }
    passed, prices = universes.filter_symbols_by_price(
        ["AAA", "BBB", "CCC", "DDD"], quotes, min_price=5.0
    )
    assert passed == ["BBB", "CCC"]
    assert prices == {"BBB": 5.0, "CCC": 12.5}


def test_filter_symbols_by_avg_volume():
    avg_volumes = {"AAA": 999_999.0, "BBB": 1_000_000.0, "CCC": 2_000_000.0}
    assert universes.filter_symbols_by_avg_volume(
        ["AAA", "BBB", "CCC", "DDD"], avg_volumes, min_avg_volume=1_000_000.0
    ) == ["BBB", "CCC"]


def test_build_and_load_liquid_meta_roundtrip(tmp_path):
    meta = universes.build_liquid_meta(
        ["AAA"],
        prices={"AAA": 10.0},
        avg_volumes={"AAA": 2_000_000.0},
        exchange_map={"AAA": "NASDAQ"},
    )
    path = tmp_path / "liquid_meta.json"
    universes.write_liquid_meta(path, meta)

    loaded = universes.load_liquid_meta(tmp_path)
    assert loaded == {"AAA": {"price": 10.0, "avg_volume_20d": 2_000_000.0, "exchange": "NASDAQ"}}


def test_load_sector_map_falls_back_to_liquid_meta_exchange(tmp_path):
    (tmp_path / "sectors.json").write_text(json.dumps({"AAPL": "Information Technology"}))
    (tmp_path / "liquid_meta.json").write_text(
        json.dumps({"AAPL": {"exchange": "NASDAQ"}, "XOM": {"exchange": "NYSE"}})
    )

    sectors = universes.load_sector_map(tmp_path)
    assert sectors["AAPL"] == "Information Technology"  # sectors.json wins over exchange fallback
    assert sectors["XOM"] == "NYSE"


@pytest.mark.parametrize("symbol", ["AIIA.R", "AIIA/R", "CELG.R"])
def test_nyse_listing_rights_are_not_common_stock(symbol):
    text = (
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
        f"{symbol}|Listing Rights|N|{symbol}|N|100|N|{symbol}\n"
        "BRK.A|Berkshire Class A|N|BRK.A|N|100|N|BRK.A\n"
    )
    assert universes.parse_nyse_listed_text(text) == ["BRK.A"]



def test_nyse_cqs_symbol_identifies_warrants_disguised_as_class_shares():
    text = (
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol\n"
        "NE.A|Noble Tranche 2 Warrants|N|NE.WS.A|N|100|N|NE+A\n"
        "NE|Noble Ordinary Shares|N|NE|N|100|N|NE\n"
        "BRK.A|Berkshire Class A|N|BRK.A|N|100|N|BRK.A\n"
    )
    assert universes.parse_nyse_listed_text(text) == ["NE", "BRK.A"]


def test_listing_parsers_drop_spac_warrants_units_and_rights_but_keep_partnership_units():
    nasdaq = (
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|"
        "NextShares\n"
        "AACI|Armada Acquisition Corp. III - Class A Ordinary Share|G|N|N|100|N|N\n"
        "AACIU|Armada Acquisition Corp. III - Units|G|N|N|100|N|N\n"
        "AACIW|Armada Acquisition Corp. III - Warrant|G|N|N|100|N|N\n"
        "ASPCR|A SPAC III Acquisition Corp. - Right|S|N|D|100|N|N\n"
        "ARLP|Alliance Resource Partners, L.P. - Common Units Representing Limited "
        "Partnership Interests|Q|N|N|100|N|N\n"
    )
    other = (
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|"
        "NASDAQ Symbol\n"
        "ET|Energy Transfer LP Common Units |N|ET|N|100|N|ET\n"
        "MARPS|Marine Petroleum Trust - Units of Beneficial Interest|N|MARPS|N|100|N|MARPS\n"
        "AMX|America Movil American Depositary Shares (each representing the right to "
        "receive 20 Series B Shares)|N|AMX|N|100|N|AMX\n"
        "BCAT.V|BlackRock Capital Allocation Term Trust Rights (expiring October 21, 2026) "
        "Rights when issued|N|BCAT.V|N|100|N|BCAT=\n"
        "ABCD.U|Example Acquisition Units, each consisting of one share|N|ABCD.U|N|100|N|"
        "ABCD=\n"
    )

    assert universes.parse_nasdaq_listed_text(nasdaq) == ["AACI", "ARLP"]
    assert universes.parse_nyse_listed_text(other) == ["ET", "MARPS", "AMX"]

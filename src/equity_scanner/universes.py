"""Universe loaders for S&P 500, Nasdaq-100, liquid, and custom watchlists, ported
from ButterflyGuy's equity_scan/universes.py. Almost all of this is mechanically
portable (file I/O and network fetchers against public GitHub, Wikipedia, and
nasdaqtrader.com sources). The Wikipedia reader identifies the constituent table by
its headers rather than depending on cell attributes. The one Schwab-touching piece,
`extract_quote_price` / `filter_symbols_by_price`, is adapted for the gateway's flat, already
session-resolved `QuoteV1` instead of ButterflyGuy's raw two-session payload dict —
see equity_scanner.scanner's module docstring for the quotes-gap background.

equity-scanner owns its own universe refresh (this module + a CLI entry point) rather
than reading ButterflyGuy's `configs/universes` output. This avoids a runtime
dependency on another repository's filesystem and lets the scanner own both schedules
after their separately approved migration."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import stat
import tempfile
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from schwab_gateway_sdk import QuoteV1

SP500_CSV_URL = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv"
)
NQ100_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
USER_AGENT = "equity-scanner/0.1"
MIN_SP500_CONSTITUENTS = 450
MIN_NQ100_CONSTITUENTS = 90
MIN_SP500_SECTORS = 450


def _read_ticker_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    tickers: list[str] = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip().upper()
        if line:
            tickers.append(line)
    return tickers


def load_universe(name: str, *, universe_dir: Path, custom_path: Path) -> list[str]:
    """Load tickers for a named universe."""
    if name == "custom":
        return _read_ticker_file(custom_path)
    path = universe_dir / f"{name}.txt"
    return _read_ticker_file(path)


def load_universes(
    names: list[str],
    *,
    universe_dir: str | Path,
    custom_watchlist: str | Path,
) -> dict[str, list[str]]:
    """Load all requested universes."""
    base = Path(universe_dir)
    custom_path = Path(custom_watchlist)
    return {name: load_universe(name, universe_dir=base, custom_path=custom_path) for name in names}


def build_symbol_map(universes: dict[str, list[str]]) -> dict[str, set[str]]:
    """Map each symbol to the universes it belongs to."""
    symbol_map: dict[str, set[str]] = {}
    for universe_name, tickers in universes.items():
        for ticker in tickers:
            symbol_map.setdefault(ticker, set()).add(universe_name)
    return symbol_map


def fetch_sp500_rows() -> list[dict[str, str]]:
    """Download S&P 500 constituents with GICS sector metadata."""
    req = urllib.request.Request(SP500_CSV_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        text = resp.read().decode()
    return list(csv.DictReader(io.StringIO(text)))


def fetch_sp500_tickers() -> list[str]:
    """Download the current S&P 500 constituents list."""
    rows = fetch_sp500_rows()
    return sorted({row["Symbol"].strip().upper() for row in rows if row.get("Symbol")})


def fetch_sp500_sectors() -> dict[str, str]:
    """Map S&P 500 tickers to GICS sector names."""
    sectors: dict[str, str] = {}
    for row in fetch_sp500_rows():
        symbol = (row.get("Symbol") or "").strip().upper()
        sector = (row.get("GICS Sector") or "").strip()
        if symbol and sector:
            sectors[symbol] = sector
    return sectors


class _HtmlTableParser(HTMLParser):
    """Collect text cells from HTML tables without depending on tag attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[tuple[str, str]]]] = []
        self._table_depth = 0
        self._rows: list[list[tuple[str, str]]] = []
        self._row: list[tuple[str, str]] | None = None
        self._cell_tag: str | None = None
        self._cell_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag == "table":
            if self._table_depth == 0:
                self._rows = []
            self._table_depth += 1
        elif self._table_depth == 1 and tag == "tr":
            self._row = []
        elif self._table_depth == 1 and self._row is not None and tag in {"th", "td"}:
            self._cell_tag = tag
            self._cell_text = []

    def handle_data(self, data: str) -> None:
        if self._cell_tag is not None:
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == self._cell_tag and self._row is not None:
            text = " ".join("".join(self._cell_text).split())
            self._row.append((tag, text))
            self._cell_tag = None
            self._cell_text = []
        elif tag == "tr" and self._table_depth == 1 and self._row is not None:
            if self._row:
                self._rows.append(self._row)
            self._row = None
        elif tag == "table" and self._table_depth:
            self._table_depth -= 1
            if self._table_depth == 0:
                self.tables.append(self._rows)
                self._rows = []


def parse_nq100_html(html: str) -> list[str]:
    """Extract constituents from the table identified by Ticker and Company headers."""
    parser = _HtmlTableParser()
    parser.feed(html)

    tickers: list[str] = []
    for table in parser.tables:
        ticker_index: int | None = None
        data_start = 0
        for index, row in enumerate(table):
            headers = [text.casefold() for tag, text in row if tag == "th"]
            if "ticker" in headers and "company" in headers:
                ticker_index = headers.index("ticker")
                data_start = index + 1
                break
        if ticker_index is None:
            continue
        for row in table[data_start:]:
            cells = [text.strip().upper() for tag, text in row if tag == "td"]
            if ticker_index >= len(cells):
                continue
            ticker = cells[ticker_index]
            if re.fullmatch(r"[A-Z][A-Z0-9.-]*", ticker):
                tickers.append(ticker)

    return list(dict.fromkeys(tickers))


def fetch_nq100_tickers() -> list[str]:
    """Download the current Nasdaq-100 constituents from Wikipedia."""
    req = urllib.request.Request(NQ100_WIKI_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        html = resp.read().decode()

    return parse_nq100_html(html)


def _atomic_write_text(path: Path, content: str) -> None:
    """Durably replace a file only after its complete contents are written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.chmod(mode)
        os.replace(temp_path, path)
        temp_path = None
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def write_universe_file(path: Path, tickers: list[str]) -> None:
    _atomic_write_text(path, "\n".join(tickers) + "\n")


def write_sector_map(path: Path, sectors: dict[str, str]) -> None:
    _atomic_write_text(path, json.dumps(dict(sorted(sectors.items())), indent=2) + "\n")


def load_sector_map(universe_dir: str | Path) -> dict[str, str]:
    """Load symbol -> sector mapping (GICS for index names, exchange fallback for liquid)."""
    path = Path(universe_dir) / "sectors.json"
    sectors: dict[str, str] = {}
    if path.exists():
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            sectors = {str(symbol).upper(): str(sector) for symbol, sector in data.items()}

    meta_path = Path(universe_dir) / "liquid_meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        if isinstance(meta, dict):
            for symbol, payload in meta.items():
                sym = str(symbol).upper()
                if sym in sectors or not isinstance(payload, dict):
                    continue
                exchange = str(payload.get("exchange") or "").strip()
                if exchange:
                    sectors[sym] = exchange
    return sectors


def _require_minimum_size(name: str, values: list[str] | dict[str, str], minimum: int) -> None:
    if len(values) < minimum:
        raise RuntimeError(
            f"Refusing to replace {name}: fetched {len(values)} entries; "
            f"minimum plausible size is {minimum}"
        )


def refresh_builtin_universes(
    universe_dir: str | Path,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Refresh sp500.txt, nq100.txt, and sectors.json from public sources."""
    base = Path(universe_dir)
    sp500 = fetch_sp500_tickers()
    nq100 = fetch_nq100_tickers()
    sectors = fetch_sp500_sectors()
    _require_minimum_size("sp500", sp500, MIN_SP500_CONSTITUENTS)
    _require_minimum_size("nq100", nq100, MIN_NQ100_CONSTITUENTS)
    _require_minimum_size("sp500 sectors", sectors, MIN_SP500_SECTORS)
    for ticker in nq100:
        if ticker not in sectors:
            sectors[ticker] = "Nasdaq-100"
    if not dry_run:
        write_universe_file(base / "sp500.txt", sp500)
        write_universe_file(base / "nq100.txt", nq100)
        write_sector_map(base / "sectors.json", sectors)
    return {"sp500": len(sp500), "nq100": len(nq100), "sectors": len(sectors)}


def _fetch_url_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return resp.read().decode()


def _is_symbol_directory_footer(line: str) -> bool:
    return line.startswith("File Creation Time")


def _is_common_equity_symbol(symbol: str) -> bool:
    """Exclude preferreds, warrants, units, and other non-common listings."""
    if any(ch in symbol for ch in ("$", "^", "+", "=")):
        return False
    if ".U" in symbol or ".WS" in symbol or symbol.endswith((".W", ".R", "/R")):
        return False
    return True


def _parse_pipe_delimited_rows(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or _is_symbol_directory_footer(line):
            continue
        rows.append(line.split("|"))
    return rows


def parse_nasdaq_listed_text(text: str) -> list[str]:
    """Parse nasdaqlisted.txt rows into common-stock NASDAQ symbols."""
    symbols: list[str] = []
    for fields in _parse_pipe_delimited_rows(text):
        if len(fields) < 8:
            continue
        symbol = fields[0].strip().upper()
        if symbol in {"SYMBOL", "ACT SYMBOL"}:
            continue
        test_issue = fields[3].strip().upper()
        etf = fields[6].strip().upper()
        if not symbol or test_issue == "Y" or etf == "Y":
            continue
        if not _is_common_equity_symbol(symbol):
            continue
        symbols.append(symbol)
    return symbols


def parse_nyse_listed_text(text: str) -> list[str]:
    """Parse otherlisted.txt rows into common-stock NYSE symbols (Exchange=N)."""
    symbols: list[str] = []
    for fields in _parse_pipe_delimited_rows(text):
        if len(fields) < 8:
            continue
        symbol = fields[0].strip().upper()
        if symbol in {"SYMBOL", "ACT SYMBOL"}:
            continue
        exchange = fields[2].strip().upper()
        etf = fields[4].strip().upper()
        test_issue = fields[6].strip().upper()
        if exchange != "N" or not symbol or test_issue == "Y" or etf == "Y":
            continue
        if not _is_common_equity_symbol(symbol) or not _is_common_equity_symbol(
            fields[3].strip().upper()
        ):
            continue
        symbols.append(symbol)
    return symbols


def fetch_nasdaq_listed_symbols() -> list[str]:
    """Download NASDAQ-listed common stock symbols."""
    return sorted(set(parse_nasdaq_listed_text(_fetch_url_text(NASDAQ_LISTED_URL))))


def fetch_nyse_listed_symbols() -> list[str]:
    """Download NYSE-listed common stock symbols."""
    return sorted(set(parse_nyse_listed_text(_fetch_url_text(OTHER_LISTED_URL))))


def fetch_exchange_seed_map() -> dict[str, str]:
    """Union of NASDAQ + NYSE seed symbols with exchange labels."""
    seed_map: dict[str, str] = {}
    for symbol in fetch_nasdaq_listed_symbols():
        seed_map[symbol] = "NASDAQ"
    for symbol in fetch_nyse_listed_symbols():
        seed_map.setdefault(symbol, "NYSE")
    return dict(sorted(seed_map.items()))


def extract_quote_price(quote: QuoteV1) -> float | None:
    """Best-effort price from a gateway quote for liquidity screening. The gateway
    already resolved regular-vs-extended freshness server-side (see
    equity_scanner.scanner's module docstring), so this is just last-or-mark on the
    flat quote, unlike ButterflyGuy's separate regular/extended comparison."""
    price = quote.last or quote.mark or quote.close
    if price is None or price <= 0:
        return None
    return price


def filter_symbols_by_price(
    symbols: list[str],
    quotes: dict[str, QuoteV1],
    *,
    min_price: float,
) -> tuple[list[str], dict[str, float]]:
    """Keep symbols whose gateway quote price meets the minimum."""
    passed: list[str] = []
    prices: dict[str, float] = {}
    for symbol in symbols:
        quote = quotes.get(symbol)
        if quote is None:
            continue
        price = extract_quote_price(quote)
        if price is None or price < min_price:
            continue
        passed.append(symbol)
        prices[symbol] = price
    return passed, prices


def filter_symbols_by_avg_volume(
    symbols: list[str],
    avg_volumes: dict[str, float],
    *,
    min_avg_volume: float,
) -> list[str]:
    """Keep symbols whose 20-day average daily volume meets the minimum."""
    return [
        symbol
        for symbol in symbols
        if (avg := avg_volumes.get(symbol)) is not None and avg >= min_avg_volume
    ]


def write_liquid_meta(path: Path, meta: dict[str, dict[str, Any]]) -> None:
    _atomic_write_text(path, json.dumps(dict(sorted(meta.items())), indent=2) + "\n")


def load_liquid_meta(universe_dir: str | Path) -> dict[str, dict[str, Any]]:
    path = Path(universe_dir) / "liquid_meta.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        return {}
    return {str(symbol).upper(): dict(values) for symbol, values in data.items()}


def build_liquid_meta(
    symbols: list[str],
    *,
    prices: dict[str, float],
    avg_volumes: dict[str, float],
    exchange_map: dict[str, str],
) -> dict[str, dict[str, Any]]:
    meta: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        meta[symbol] = {
            "price": prices[symbol],
            "avg_volume_20d": avg_volumes[symbol],
            "exchange": exchange_map.get(symbol, "Unknown"),
        }
    return meta

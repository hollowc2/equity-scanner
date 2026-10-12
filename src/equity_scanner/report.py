"""Discord formatting for equity morning scans, ported from ButterflyGuy's
equity_scan/report.py. Provider-agnostic like scanner.py — consumes ScanResults/
EquitySnapshot, never a client, so this is a mechanical port."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from equity_scanner.files import atomic_write_text
from equity_scanner.scan_config import EquityScanSettings
from equity_scanner.scanner import (
    GICS_SECTORS,
    STALE_FLAGS,
    EquitySnapshot,
    MarketContext,
    OpeningFocusItem,
    ScanResults,
    mover_change_pct,
    mover_symbol,
)
from equity_scanner.time_utils import EASTERN

DISCORD_CHAR_LIMIT = 1900

# Nearly every scanned name is in the liquid universe, so its tag carries no signal.
_QUIET_UNIVERSES = frozenset({"liquid"})
_OTHER_SECTOR = "Other"

_UNIVERSE_LABELS = {
    "sp500": "S&P",
    "nq100": "NDX",
    "liquid": "Liq",
    "custom": "★",
}

_SECTOR_SHORT = {
    "Information Technology": "Tech",
    "Consumer Discretionary": "Cons Disc",
    "Consumer Staples": "Cons Stap",
    "Health Care": "Health",
    "Communication Services": "Comm",
    "Real Estate": "RE",
}


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "unavailable"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.1f}%"


def _fmt_price(value: float) -> str:
    return f"${value:.2f}"


def _fmt_volume(volume: int) -> str:
    if volume >= 1_000_000:
        return f"{volume / 1_000_000:.1f}M"
    if volume >= 1_000:
        return f"{volume / 1_000:.0f}K"
    return str(volume)


def _fmt_universes(snapshot: EquitySnapshot) -> str:
    labels = [
        _UNIVERSE_LABELS.get(tag, tag)
        for tag in snapshot.universes
        if tag not in _QUIET_UNIVERSES
    ]
    if not labels:
        return ""
    return f" · {'+'.join(labels)}"


def _fmt_rvol(snapshot: EquitySnapshot) -> str:
    if snapshot.rvol is None:
        return ""
    label = "🔥" if snapshot.rvol >= 1.0 else ""
    value = f"{snapshot.rvol:.2f}" if snapshot.rvol < 0.1 else f"{snapshot.rvol:.1f}"
    if 0 < snapshot.rvol < 0.005:
        value = "<0.01"
    return f" · {label}RVOL {value}x"


def _fmt_quality(snapshot: EquitySnapshot, *, show_stale: bool = True) -> str:
    """Data-quality flags, with the gateway's two stale flags collapsed into one.

    Yesterday's lists hide staleness: a name that has not traded premarket is
    expected there and does not change yesterday's move."""
    flags = [flag for flag in snapshot.data_quality_flags if flag not in STALE_FLAGS]
    if show_stale and STALE_FLAGS & set(snapshot.data_quality_flags):
        flags.insert(0, "stale")
    if flags:
        return " · ⚠ " + ",".join(flags)
    return ""


def _fmt_news(snapshot: EquitySnapshot) -> str:
    if snapshot.news is None:
        return ""
    parts = [f"news {snapshot.news.score:.1f}"]
    if snapshot.news.reasons:
        parts.append("/".join(snapshot.news.reasons[:2]))
    return " · " + " · ".join(parts)


def _direction_emoji(pct: float) -> str:
    return "🟢" if pct >= 0 else "🔴"


def _format_snapshot_line(
    snapshot: EquitySnapshot, *, pct_field: str, headlines: frozenset[str]
) -> str:
    pct = getattr(snapshot, pct_field)
    star = " ⭐" if snapshot.symbol in headlines else ""
    return (
        f"{_direction_emoji(pct)} **{snapshot.symbol}** **{_fmt_pct(pct)}**{star} "
        f"@ {_fmt_price(snapshot.price)} · {_fmt_volume(snapshot.volume)} vol"
        f"{_fmt_rvol(snapshot)}{_fmt_news(snapshot)}{_fmt_universes(snapshot)}"
        f"{_fmt_quality(snapshot, show_stale=pct_field != 'prior_day_pct')}"
    )


def _format_headline_lines(item: OpeningFocusItem) -> list[str]:
    """A headline setup: the move and volume up front, the why on a subtext line."""
    snapshot = item.snapshot
    reasons = ", ".join(item.reasons)
    return [
        f"{_direction_emoji(snapshot.session_gap_pct)} **{snapshot.symbol}  "
        f"{_fmt_pct(snapshot.session_gap_pct)}** gap @ {_fmt_price(snapshot.price)} · "
        f"{_fmt_volume(snapshot.premarket_volume)} pm vol{_fmt_rvol(snapshot)}"
        f"{_fmt_universes(snapshot)}{_fmt_quality(snapshot)}",
        f"-# ↳ {reasons} · prior {_fmt_pct(snapshot.prior_day_pct)}{_fmt_news(snapshot)}",
    ]


def _format_focus_line(item: OpeningFocusItem) -> str:
    snapshot = item.snapshot
    reasons = ", ".join(item.reasons)
    return (
        f"{_direction_emoji(snapshot.session_gap_pct)} **{snapshot.symbol}** "
        f"gap {_fmt_pct(snapshot.session_gap_pct)} · prior {_fmt_pct(snapshot.prior_day_pct)} "
        f"@ {_fmt_price(snapshot.price)} · {reasons}{_fmt_rvol(snapshot)}"
        f"{_fmt_news(snapshot)}{_fmt_universes(snapshot)}{_fmt_quality(snapshot)}"
    )


def _format_catalyst_line(snapshot: EquitySnapshot) -> str:
    assert snapshot.news is not None
    labels = [*snapshot.news.upcoming_events, *snapshot.news.recent_headlines]
    detail = labels[0] if labels else ", ".join(snapshot.news.reasons)
    providers = "+".join(snapshot.news.providers)
    return (
        f"📰 **{snapshot.symbol}** news {snapshot.news.score:.1f} · "
        f"gap {_fmt_pct(snapshot.session_gap_pct)} · prior {_fmt_pct(snapshot.prior_day_pct)} "
        f"· {detail} · {providers}{_fmt_universes(snapshot)}"
    )


def _sector_label(sector: str) -> str:
    return _SECTOR_SHORT.get(sector, sector)


def _sector_group(sector: str) -> str:
    """GICS sectors group by name; exchange and index fallbacks share one group."""
    return sector if sector in GICS_SECTORS else _OTHER_SECTOR


def _format_sector_header(sector: str, count: int) -> str:
    label = _sector_label(sector).upper()
    return f"▸ __**{label}**__ · {count}"


def _group_snapshots_by_sector(
    snapshots: list[EquitySnapshot],
) -> list[tuple[str, list[EquitySnapshot]]]:
    grouped: dict[str, list[EquitySnapshot]] = {}
    for snapshot in snapshots:
        grouped.setdefault(_sector_group(snapshot.sector), []).append(snapshot)

    known = sorted(sector for sector in grouped if sector != _OTHER_SECTOR)
    if _OTHER_SECTOR in grouped:
        known.append(_OTHER_SECTOR)
    return [(sector, grouped[sector]) for sector in known]


def _format_snapshot_section(
    title: str,
    snapshots: list[EquitySnapshot],
    *,
    pct_field: str,
    empty_text: str,
    group_by_sector: bool,
    headlines: frozenset[str],
) -> str:
    if not snapshots:
        return _format_section(title, [], empty_text=empty_text)

    groups = _group_snapshots_by_sector(snapshots)
    if not group_by_sector or [sector for sector, _ in groups] == [_OTHER_SECTOR]:
        lines = [
            _format_snapshot_line(snapshot, pct_field=pct_field, headlines=headlines)
            for snapshot in snapshots
        ]
        return _format_section(title, lines, empty_text=empty_text)

    lines: list[str] = []
    for sector, sector_snapshots in groups:
        if lines:
            lines.append("")
        lines.append(_format_sector_header(sector, len(sector_snapshots)))
        lines.extend(
            _format_snapshot_line(snapshot, pct_field=pct_field, headlines=headlines)
            for snapshot in sector_snapshots
        )
    return _format_section(title, lines, empty_text=empty_text)


def _format_section(title: str, lines: list[str], *, empty_text: str) -> str:
    body = "\n".join(lines) if lines else empty_text
    return f"{title}\n{body}"


def _split_section(section: str) -> list[str]:
    if len(section) <= DISCORD_CHAR_LIMIT:
        return [section]
    lines = section.splitlines()
    if len(lines) <= 1:
        return [section[:DISCORD_CHAR_LIMIT]]

    title = lines[0]
    chunks: list[str] = []
    current = title
    for line in lines[1:]:
        candidate = f"{current}\n{line}"
        if len(candidate) > DISCORD_CHAR_LIMIT:
            chunks.append(current)
            current = f"{title} (cont.)\n{line}"
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _format_market_context(context: list[MarketContext]) -> str:
    if not context:
        return "_No index quotes available._"

    # A tuple, not a set: .index() below needs a fixed order, and a set's iteration
    # order depends on Python's per-process string hash randomization.
    priority = ("$SPX", "SPY", "QQQ", "$COMPX", "$DJI")
    ordered = sorted(
        context,
        key=lambda ctx: (
            0 if ctx.symbol in priority else 1,
            priority.index(ctx.symbol) if ctx.symbol in priority else 99,
            ctx.symbol,
        ),
    )
    parts = [f"**{ctx.symbol}** {_fmt_pct(ctx.change_pct)}" for ctx in ordered]
    return " · ".join(parts)


def _format_mover_item(item: dict[str, Any]) -> str:
    pct = mover_change_pct(item)
    return f"{_direction_emoji(pct or 0.0)} **{mover_symbol(item)}** {_fmt_pct(pct)}"


def _format_bad_data(results: ScanResults) -> str:
    rejected = results.rejected_symbols or {}
    rejected = {reason: count for reason, count in rejected.items() if reason != "filter_failed"}
    bad_data = results.bad_data or []
    if not rejected and not bad_data:
        return ""
    counts = ", ".join(f"{reason}: {count}" for reason, count in sorted(rejected.items()))
    examples = [f"{item.get('symbol', '?')} {item.get('reason', '?')}" for item in bad_data[:5]]
    suffix = f" · examples: {', '.join(examples)}" if examples else ""
    return f"{counts}{suffix}"


def _format_header(
    results: ScanResults,
    *,
    settings: EquityScanSettings,
    ts: dt.datetime,
) -> str:
    universe_labels = [_UNIVERSE_LABELS.get(u, u) for u in settings.universes]
    tape = _format_market_context(results.market_context)
    return (
        f"## 📈 Morning Equity Scan · {ts.strftime('%a %b %d')}\n"
        f"**Tape:** {tape}\n"
        f"-# {ts.strftime('%I:%M %p').lstrip('0')} ET · "
        f"scanned {results.scanned_symbols:,} names ({', '.join(universe_labels)}) "
        f"· {results.matched_symbols:,} passed filters"
    )


def build_report(
    results: ScanResults,
    *,
    settings: EquityScanSettings,
    generated_at: dt.datetime | None = None,
) -> list[str]:
    """Build one or more Discord messages within the 2000-char limit."""
    ts = generated_at or dt.datetime.now(EASTERN)
    header = _format_header(results, settings=settings, ts=ts)

    sections: list[str] = []
    group_by_sector = settings.group_by_sector
    top = [item for item in results.opening_focus if item.headline]
    rest = [item for item in results.opening_focus if not item.headline]
    headlines = frozenset(item.snapshot.symbol for item in top)

    if top:
        sections.append(
            _format_section(
                "### ⭐ Top Setups",
                [line for item in top for line in _format_headline_lines(item)],
                empty_text="",
            )
        )
    if rest or not top:
        sections.append(
            _format_section(
                f"**🎯 Opening Focus** ({len(rest)})",
                [_format_focus_line(item) for item in rest],
                empty_text="_No focused opening setups cleared the scan._",
            )
        )

    sections.append(
        _format_section(
            f"**📰 Catalyst Watch** ({len(results.catalyst_watch)})",
            [_format_catalyst_line(snapshot) for snapshot in results.catalyst_watch],
            empty_text="_No recent or upcoming catalyst signals found for filtered names._",
        )
    )

    prior_min = settings.filters.prior_day_min_pct
    sections.append(
        _format_snapshot_section(
            f"**📊 Yesterday's Rallies** ({len(results.prior_gainers)}) · >{prior_min:.0f}%",
            results.prior_gainers,
            pct_field="prior_day_pct",
            empty_text="_Nothing cleared the rally threshold._",
            group_by_sector=group_by_sector,
            headlines=headlines,
        )
    )
    sections.append(
        _format_snapshot_section(
            f"**📉 Yesterday's Selloffs** ({len(results.prior_losers)}) · <-{prior_min:.0f}%",
            results.prior_losers,
            pct_field="prior_day_pct",
            empty_text="_Nothing cleared the selloff threshold._",
            group_by_sector=group_by_sector,
            headlines=headlines,
        )
    )

    gap_min = settings.filters.premarket_min_gap_pct
    if results.show_premarket:
        sections.append(
            _format_snapshot_section(
                f"**🌅 Premarket Gap-Ups** ({len(results.premarket_gainers)}) · >{gap_min:.0f}%",
                results.premarket_gainers,
                pct_field="session_gap_pct",
                empty_text="_No meaningful gap-ups yet._",
                group_by_sector=group_by_sector,
                headlines=headlines,
            )
        )
        sections.append(
            _format_snapshot_section(
                f"**🌧 Premarket Gap-Downs** ({len(results.premarket_losers)}) · <-{gap_min:.0f}%",
                results.premarket_losers,
                pct_field="session_gap_pct",
                empty_text="_No meaningful gap-downs yet._",
                group_by_sector=group_by_sector,
                headlines=headlines,
            )
        )
    else:
        sections.append(
            _format_section(
                "**🌅 Premarket Gaps**",
                [],
                empty_text=(
                    f"_Gap scan starts at {settings.premarket_start_et} ET — "
                    "yesterday's moves above are still current._"
                ),
            )
        )

    if results.show_movers:
        sections.append(
            _format_section(
                f"**⚡ Schwab Movers Up** ({len(results.movers_up)})",
                [_format_mover_item(item) for item in results.movers_up],
                empty_text="_No Schwab mover gainers cleared the threshold._",
            )
        )
        sections.append(
            _format_section(
                f"**⚡ Schwab Movers Down** ({len(results.movers_down)})",
                [_format_mover_item(item) for item in results.movers_down],
                empty_text="_No Schwab mover losers cleared the threshold._",
            )
        )

    quote_sanity = _format_bad_data(results)
    if quote_sanity:
        sections.append(f"-# 🧪 Quote sanity: {quote_sanity}")

    messages: list[str] = []
    current = header
    for section in sections:
        for chunk in _split_section(section):
            candidate = f"{current}\n\n{chunk}"
            if len(candidate) > DISCORD_CHAR_LIMIT:
                messages.append(current)
                current = chunk
            else:
                current = candidate
    if current:
        messages.append(current)
    return messages


def archive_report(
    messages: list[str],
    *,
    report_dir: str,
    generated_at: dt.datetime,
) -> Path:
    """Write the scan report to a dated markdown file under report_dir."""
    path = Path(report_dir) / f"{generated_at.strftime('%Y-%m-%d')}.md"
    atomic_write_text(path, "\n\n---\n\n".join(messages) + "\n")
    return path


def archive_report_json(
    results: ScanResults,
    *,
    report_dir: str,
    generated_at: dt.datetime,
) -> Path:
    """Write machine-readable scan internals next to the markdown report."""
    path = Path(report_dir) / f"{generated_at.strftime('%Y-%m-%d')}.json"
    payload = asdict(results)
    payload["generated_at"] = generated_at.isoformat()
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path

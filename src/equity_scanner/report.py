"""Discord formatting for equity morning scans, ported from ButterflyGuy's
equity_scan/report.py. Provider-agnostic like scanner.py — consumes ScanResults/
EquitySnapshot, never a client, so this is a mechanical port."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from equity_scanner.scan_config import EquityScanSettings
from equity_scanner.scanner import EquitySnapshot, MarketContext, OpeningFocusItem, ScanResults
from equity_scanner.time_utils import EASTERN

DISCORD_CHAR_LIMIT = 1900

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


def _fmt_pct(value: float) -> str:
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
    if not snapshot.universes:
        return ""
    labels = [_UNIVERSE_LABELS.get(tag, tag) for tag in snapshot.universes]
    return f" · {'+'.join(labels)}"


def _fmt_rvol(snapshot: EquitySnapshot) -> str:
    if snapshot.rvol is None:
        return ""
    label = "🔥" if snapshot.rvol >= 1.0 else ""
    return f" · {label}RVOL {snapshot.rvol:.1f}x"


def _fmt_quality(snapshot: EquitySnapshot) -> str:
    if snapshot.data_quality_flags:
        return " · flags " + ",".join(snapshot.data_quality_flags)
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


def _format_snapshot_line(snapshot: EquitySnapshot, *, pct_field: str) -> str:
    pct = getattr(snapshot, pct_field)
    return (
        f"{_direction_emoji(pct)} **{snapshot.symbol}** **{_fmt_pct(pct)}** "
        f"@ {_fmt_price(snapshot.price)} · {_fmt_volume(snapshot.volume)} vol"
        f"{_fmt_rvol(snapshot)}{_fmt_news(snapshot)}{_fmt_universes(snapshot)}"
        f"{_fmt_quality(snapshot)}"
    )


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


def _format_sector_header(sector: str, count: int) -> str:
    label = _sector_label(sector).upper()
    return f"▸ __**{label}**__ · {count}"


def _group_snapshots_by_sector(
    snapshots: list[EquitySnapshot],
) -> list[tuple[str, list[EquitySnapshot]]]:
    grouped: dict[str, list[EquitySnapshot]] = {}
    for snapshot in snapshots:
        grouped.setdefault(snapshot.sector, []).append(snapshot)

    known = sorted(sector for sector in grouped if sector != "Unknown")
    if "Unknown" in grouped:
        known.append("Unknown")
    return [(sector, grouped[sector]) for sector in known]


def _format_snapshot_section(
    title: str,
    snapshots: list[EquitySnapshot],
    *,
    pct_field: str,
    empty_text: str,
    group_by_sector: bool,
) -> str:
    if not snapshots:
        return _format_section(title, [], empty_text=empty_text)

    if not group_by_sector:
        lines = [_format_snapshot_line(snapshot, pct_field=pct_field) for snapshot in snapshots]
        return _format_section(title, lines, empty_text=empty_text)

    lines: list[str] = []
    for sector, sector_snapshots in _group_snapshots_by_sector(snapshots):
        if lines:
            lines.append("")
        lines.append(_format_sector_header(sector, len(sector_snapshots)))
        lines.extend(
            _format_snapshot_line(snapshot, pct_field=pct_field) for snapshot in sector_snapshots
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
    symbol = str(item.get("symbol") or item.get("ticker") or "?")
    pct = item.get("changePercent") or item.get("netPercentChange")
    if pct is None:
        pct = item.get("change") or item.get("netChange") or 0
    try:
        pct_value = float(pct)
    except (TypeError, ValueError):
        pct_value = 0.0
    return f"{_direction_emoji(pct_value)} **{symbol}** {_fmt_pct(pct_value)}"


def _format_bad_data(results: ScanResults) -> str:
    rejected = results.rejected_symbols or {}
    rejected = {reason: count for reason, count in rejected.items() if reason != "filter_failed"}
    bad_data = results.bad_data or []
    if not rejected and not bad_data:
        return "_No quote sanity rejects._"
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
        f"📈 **Morning Equity Scan** · {ts.strftime('%a %b %d')} · "
        f"{ts.strftime('%I:%M %p').lstrip('0')} ET\n"
        f"**Tape:** {tape}\n"
        f"_Scanned {results.scanned_symbols:,} names ({', '.join(universe_labels)}) "
        f"· {results.matched_symbols:,} passed filters_"
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

    sections.append(
        _format_section(
            f"**🎯 Opening Focus** ({len(results.opening_focus)})",
            [_format_focus_line(item) for item in results.opening_focus],
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
        )
    )
    sections.append(
        _format_snapshot_section(
            f"**📉 Yesterday's Selloffs** ({len(results.prior_losers)}) · <-{prior_min:.0f}%",
            results.prior_losers,
            pct_field="prior_day_pct",
            empty_text="_Nothing cleared the selloff threshold._",
            group_by_sector=group_by_sector,
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
            )
        )
        sections.append(
            _format_snapshot_section(
                f"**🌧 Premarket Gap-Downs** ({len(results.premarket_losers)}) · <-{gap_min:.0f}%",
                results.premarket_losers,
                pct_field="session_gap_pct",
                empty_text="_No meaningful gap-downs yet._",
                group_by_sector=group_by_sector,
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
    if quote_sanity != "_No quote sanity rejects._":
        sections.append(
            _format_section(
                "**🧪 Quote Sanity**",
                [quote_sanity],
                empty_text="_No quote sanity rejects._",
            )
        )

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
    out_dir = Path(report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{generated_at.strftime('%Y-%m-%d')}.md"
    path.write_text("\n\n---\n\n".join(messages) + "\n")
    return path


def archive_report_json(
    results: ScanResults,
    *,
    report_dir: str,
    generated_at: dt.datetime,
) -> Path:
    """Write machine-readable scan internals next to the markdown report."""
    out_dir = Path(report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{generated_at.strftime('%Y-%m-%d')}.json"
    payload = asdict(results)
    payload["generated_at"] = generated_at.isoformat()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path

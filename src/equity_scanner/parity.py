"""Compare one ButterflyGuy report with one standalone EquityScanner report."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

SECTIONS = (
    "prior_gainers",
    "prior_losers",
    "premarket_gainers",
    "premarket_losers",
)
CALCULATED_FIELDS = (
    "price",
    "prior_day_pct",
    "session_gap_pct",
    "avg_volume_20d",
    "rvol",
    "volume",
    "premarket_volume",
)


def _symbol(item: dict[str, Any]) -> str | None:
    payload = item.get("snapshot", item)
    return payload.get("symbol") if isinstance(payload, dict) else None


def _snapshots(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    for section in (*SECTIONS, "opening_focus", "catalyst_watch"):
        for item in report.get(section, []):
            payload = item.get("snapshot", item)
            if isinstance(payload, dict) and payload.get("symbol"):
                snapshots[payload["symbol"]] = payload
    return snapshots


def _same_number(left: Any, right: Any, tolerance: float) -> bool:
    if left is None or right is None:
        return left is right
    try:
        return math.isclose(float(left), float(right), abs_tol=tolerance, rel_tol=0)
    except (TypeError, ValueError):
        return left == right


def compare_reports(
    reference: dict[str, Any],
    candidate: dict[str, Any],
    *,
    tolerance: float = 0.01,
) -> dict[str, Any]:
    section_differences = {}
    for section in SECTIONS:
        reference_symbols = [_symbol(item) for item in reference.get(section, [])]
        candidate_symbols = [_symbol(item) for item in candidate.get(section, [])]
        if reference_symbols != candidate_symbols:
            section_differences[section] = {
                "reference": reference_symbols,
                "candidate": candidate_symbols,
            }

    reference_snapshots = _snapshots(reference)
    candidate_snapshots = _snapshots(candidate)
    shared = sorted(set(reference_snapshots) & set(candidate_snapshots))
    value_differences = []
    for symbol in shared:
        for field in CALCULATED_FIELDS:
            left = reference_snapshots[symbol].get(field)
            right = candidate_snapshots[symbol].get(field)
            if not _same_number(left, right, tolerance):
                value_differences.append(
                    {"symbol": symbol, "field": field, "reference": left, "candidate": right}
                )

    count_differences = {}
    for field in ("scanned_symbols", "matched_symbols", "rejected_symbols"):
        if reference.get(field) != candidate.get(field):
            count_differences[field] = {
                "reference": reference.get(field),
                "candidate": candidate.get(field),
            }

    passed = not section_differences and not value_differences and not count_differences
    return {
        "verdict": "pass" if passed else "difference",
        "reference_generated_at": reference.get("generated_at"),
        "candidate_generated_at": candidate.get("generated_at"),
        "tolerance": tolerance,
        "count_differences": count_differences,
        "section_differences": section_differences,
        "calculated_value_differences": value_differences,
        "shared_ranked_symbols": len(shared),
        "excluded_from_gate": ["opening_focus", "catalyst_watch", "news_impacts"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--tolerance", type=float, default=0.01)
    args = parser.parse_args()
    result = compare_reports(
        json.loads(args.reference.read_text()),
        json.loads(args.candidate.read_text()),
        tolerance=args.tolerance,
    )
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()

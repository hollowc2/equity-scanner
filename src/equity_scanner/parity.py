"""Compare one ButterflyGuy report with one standalone EquityScanner report."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import yaml

SECTIONS = (
    "prior_gainers",
    "prior_losers",
    "premarket_gainers",
    "premarket_losers",
)
RANKED_SECTIONS = (*SECTIONS, "opening_focus", "catalyst_watch")
CALCULATED_FIELDS = (
    "price",
    "prior_day_pct",
    "session_gap_pct",
    "avg_volume_20d",
    "rvol",
    "volume",
    "premarket_volume",
)
DYNAMIC_FIELDS = (
    "price",
    "session_gap_pct",
    "volume",
    "premarket_volume",
    "rvol",
)
FROZEN_UNIVERSE_FILES = (
    "sp500.txt",
    "nq100.txt",
    "liquid.txt",
    "custom.txt",
    "sectors.json",
    "liquid_meta.json",
)
FROZEN_INPUT_FILES = (
    "equity_scan.reference.yaml",
    "equity_scan.candidate.yaml",
    *FROZEN_UNIVERSE_FILES,
)


def _comparator_source_hash() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _symbol(item: dict[str, Any]) -> str | None:
    payload = item.get("snapshot", item)
    return payload.get("symbol") if isinstance(payload, dict) else None


def _snapshots(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    for section in RANKED_SECTIONS:
        for item in report.get(section, []):
            payload = item.get("snapshot", item)
            if isinstance(payload, dict) and payload.get("symbol"):
                snapshots[payload["symbol"]] = payload
    return snapshots


def _snapshots_for_sections(
    report: dict[str, Any], sections: tuple[str, ...]
) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    for section in sections:
        for item in report.get(section, []):
            payload = item.get("snapshot", item)
            if isinstance(payload, dict) and payload.get("symbol"):
                snapshots[payload["symbol"]] = payload
    return snapshots


def _shared_order(
    left: list[str | None], right: list[str | None]
) -> tuple[list[str], list[str]]:
    """Return each ranking restricted to symbols present in both captures."""
    shared = {symbol for symbol in left if symbol is not None} & {
        symbol for symbol in right if symbol is not None
    }
    return (
        [symbol for symbol in left if symbol in shared],
        [symbol for symbol in right if symbol in shared],
    )


def _same_number(left: Any, right: Any, tolerance: float) -> bool:
    if left is None or right is None:
        return left is right
    try:
        return math.isclose(float(left), float(right), abs_tol=tolerance, rel_tol=0)
    except (TypeError, ValueError):
        return left == right


def _capture_skew_seconds(reference: dict[str, Any], candidate: dict[str, Any]) -> float | None:
    try:
        left = dt.datetime.fromisoformat(str(reference["generated_at"]).replace("Z", "+00:00"))
        right = dt.datetime.fromisoformat(str(candidate["generated_at"]).replace("Z", "+00:00"))
        return abs((right - left).total_seconds())
    except (KeyError, TypeError, ValueError):
        return None


def _quote_coverage_failure(report: dict[str, Any]) -> dict[str, Any] | None:
    """Return auditable gate evidence only when a report declares incomplete coverage.

    Older/reference reports do not carry this field, so absence preserves existing
    strict comparator behavior. Once coverage is declared, it must be internally
    consistent and complete for either comparator mode to pass.
    """
    coverage = report.get("quote_coverage")
    if coverage is None:
        return None
    if not isinstance(coverage, dict):
        return {"reason": "invalid_quote_coverage"}
    requested = coverage.get("requested_count")
    returned = coverage.get("returned_count")
    stale_retained = coverage.get("stale_retained_count")
    unavailable = coverage.get("unavailable_count")
    failed_batches = coverage.get("failed_batch_count")
    complete = coverage.get("complete")
    verdict = coverage.get("verdict")
    requested_symbols = coverage.get("requested_symbols")
    returned_symbols = coverage.get("returned_symbols")
    stale_retained_symbols = coverage.get("stale_retained_symbols")
    unavailable_symbols = coverage.get("unavailable_symbols")
    failed_batch_evidence = coverage.get("failed_batches")
    lists = (
        requested_symbols,
        returned_symbols,
        stale_retained_symbols,
        unavailable_symbols,
        failed_batch_evidence,
    )
    counts = (requested, returned, stale_retained, unavailable, failed_batches)
    internally_consistent = (
        all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in counts
        )
        and all(isinstance(value, list) for value in lists)
        and len(requested_symbols) == requested
        and len(returned_symbols) == returned
        and len(stale_retained_symbols) == stale_retained
        and len(unavailable_symbols) == unavailable
        and len(failed_batch_evidence) == failed_batches
        and len(set(requested_symbols)) == requested
        and set(requested_symbols) == set(returned_symbols) | set(unavailable_symbols)
        and not set(returned_symbols) & set(unavailable_symbols)
        and set(stale_retained_symbols) <= set(returned_symbols)
        and report.get("scanned_symbols") == returned
    )
    if not internally_consistent:
        return {
            "reason": "invalid_quote_coverage",
            "requested_count": requested,
            "returned_count": returned,
            "scanned_symbols": report.get("scanned_symbols"),
        }
    if (
        complete is True
        and verdict == "complete"
        and requested == returned
        and unavailable == 0
        and failed_batches == 0
    ):
        return None
    return {
        "reason": "incomplete_quote_coverage",
        "requested_count": requested,
        "returned_count": returned,
        "stale_retained_count": stale_retained,
        "unavailable_count": unavailable,
        "failed_batch_count": failed_batches,
        "unavailable_symbols": unavailable_symbols,
        "failed_batches": failed_batch_evidence,
    }


def _canonical_config_hash(path: Path) -> str:
    config = yaml.safe_load(path.read_text()) or {}
    for key in ("universe_dir", "custom_watchlist", "report_dir"):
        config.pop(key, None)
    payload = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _file_hash(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _verified_manifest(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {"path": str(path) if path else None, "verified": False, "sha256": {}}

    expected: dict[str, str] = {}
    verified = True
    for line in path.read_text().splitlines():
        parts = line.split(maxsplit=1)
        if (
            len(parts) != 2
            or len(parts[0]) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in parts[0])
        ):
            verified = False
            continue
        name = parts[1].lstrip(" *")
        if not name or Path(name).name != name:
            verified = False
            continue
        expected[name] = parts[0].lower()

    actual = {name: _file_hash(path.parent / name) for name in expected}
    if not set(FROZEN_INPUT_FILES).issubset(expected) or any(
        actual[name] != digest for name, digest in expected.items()
    ):
        verified = False
    return {
        "path": str(path),
        "verified": verified,
        "expected_sha256": expected,
        "verified_sha256": actual,
    }


def _universe_hashes(path: Path) -> dict[str, str | None]:
    return {
        name: hashlib.sha256((path / name).read_bytes()).hexdigest()
        if (path / name).is_file()
        else None
        for name in FROZEN_UNIVERSE_FILES
    }


def build_identity_evidence(
    *,
    reference_config: Path,
    candidate_config: Path,
    reference_universe_dir: Path,
    candidate_universe_dir: Path,
    input_manifest: Path | None = None,
) -> dict[str, Any]:
    reference_config_hash = _canonical_config_hash(reference_config)
    candidate_config_hash = _canonical_config_hash(candidate_config)
    reference_universes = _universe_hashes(reference_universe_dir)
    candidate_universes = _universe_hashes(candidate_universe_dir)
    manifest = _verified_manifest(input_manifest)
    universe_files_complete = all(reference_universes.values()) and all(
        candidate_universes.values()
    )
    return {
        "input_manifest": manifest,
        "input_manifest_verified": manifest["verified"],
        "reference_config_file_sha256": _file_hash(reference_config),
        "candidate_config_file_sha256": _file_hash(candidate_config),
        "reference_config_sha256": reference_config_hash,
        "candidate_config_sha256": candidate_config_hash,
        "config_equal": reference_config_hash == candidate_config_hash,
        "reference_universe_sha256": reference_universes,
        "candidate_universe_sha256": candidate_universes,
        "universe_files_complete": bool(universe_files_complete),
        "universes_equal": universe_files_complete
        and reference_universes == candidate_universes,
    }


def compare_reports(
    reference: dict[str, Any],
    candidate: dict[str, Any],
    *,
    tolerance: float = 0.01,
    mode: str = "strict",
    identity_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if mode not in {"strict", "sequential-skew-aware"}:
        raise ValueError(f"unsupported comparator mode: {mode}")

    capture_skew = _capture_skew_seconds(reference, candidate)
    if mode == "sequential-skew-aware":
        evidence = identity_evidence or {}
        stable_failures = {}
        if not evidence.get("config_equal"):
            stable_failures["config_identity"] = "different_or_missing"
        if not evidence.get("input_manifest_verified"):
            stable_failures["input_manifest"] = "missing_or_invalid"
        if not evidence.get("universe_files_complete") or not evidence.get("universes_equal"):
            stable_failures["universe_identity"] = "different_or_missing"
        if capture_skew is None:
            stable_failures["capture_timestamps"] = "missing_or_invalid"
        if coverage_failure := _quote_coverage_failure(candidate):
            stable_failures["quote_coverage"] = coverage_failure

        stable_section_differences = {}
        dynamic_section_differences = {}
        # Prior-day values are stable, but membership is not: run_scan builds both
        # prior sections from snapshots that already passed capture-time price,
        # volume, reference-price, and premarket-RVOL filters. Compare the relative
        # order of shared members while retaining exact membership as skew evidence.
        for section in ("prior_gainers", "prior_losers"):
            reference_symbols = [_symbol(item) for item in reference.get(section, [])]
            candidate_symbols = [_symbol(item) for item in candidate.get(section, [])]
            if reference_symbols != candidate_symbols:
                dynamic_section_differences[section] = {
                    "reference": reference_symbols,
                    "candidate": candidate_symbols,
                }
            reference_shared, candidate_shared = _shared_order(
                reference_symbols, candidate_symbols
            )
            if reference_shared != candidate_shared:
                stable_section_differences[section] = {
                    "reference": reference_shared,
                    "candidate": candidate_shared,
                }

        reference_prior_sections = {
            symbol: section
            for section in ("prior_gainers", "prior_losers")
            for item in reference.get(section, [])
            if (symbol := _symbol(item)) is not None
        }
        candidate_prior_sections = {
            symbol: section
            for section in ("prior_gainers", "prior_losers")
            for item in candidate.get(section, [])
            if (symbol := _symbol(item)) is not None
        }
        stable_section_assignment_differences = {
            symbol: {
                "reference": reference_prior_sections[symbol],
                "candidate": candidate_prior_sections[symbol],
            }
            for symbol in sorted(reference_prior_sections.keys() & candidate_prior_sections)
            if reference_prior_sections[symbol] != candidate_prior_sections[symbol]
        }

        stable_count_differences = {}
        if reference.get("scanned_symbols") != candidate.get("scanned_symbols"):
            stable_count_differences["scanned_symbols"] = {
                "reference": reference.get("scanned_symbols"),
                "candidate": candidate.get("scanned_symbols"),
            }

        reference_stable = _snapshots_for_sections(
            reference, ("prior_gainers", "prior_losers")
        )
        candidate_stable = _snapshots_for_sections(
            candidate, ("prior_gainers", "prior_losers")
        )
        shared_stable = sorted(set(reference_stable) & set(candidate_stable))
        stable_value_differences = []
        for symbol in shared_stable:
            left = reference_stable[symbol].get("prior_day_pct")
            right = candidate_stable[symbol].get("prior_day_pct")
            if not _same_number(left, right, tolerance):
                stable_value_differences.append(
                    {
                        "symbol": symbol,
                        "field": "prior_day_pct",
                        "reference": left,
                        "candidate": right,
                    }
                )

        for section in ("premarket_gainers", "premarket_losers"):
            reference_symbols = [_symbol(item) for item in reference.get(section, [])]
            candidate_symbols = [_symbol(item) for item in candidate.get(section, [])]
            if reference_symbols != candidate_symbols:
                dynamic_section_differences[section] = {
                    "reference": reference_symbols,
                    "candidate": candidate_symbols,
                }

        dynamic_count_differences = {}
        for field in ("matched_symbols", "rejected_symbols"):
            if reference.get(field) != candidate.get(field):
                dynamic_count_differences[field] = {
                    "reference": reference.get(field),
                    "candidate": candidate.get(field),
                }

        reference_snapshots = _snapshots(reference)
        candidate_snapshots = _snapshots(candidate)
        shared = sorted(set(reference_snapshots) & set(candidate_snapshots))
        dynamic_value_differences = []
        for symbol in shared:
            for field in DYNAMIC_FIELDS:
                left = reference_snapshots[symbol].get(field)
                right = candidate_snapshots[symbol].get(field)
                if not _same_number(left, right, tolerance):
                    dynamic_value_differences.append(
                        {
                            "symbol": symbol,
                            "field": field,
                            "reference": left,
                            "candidate": right,
                        }
                    )

        passed = not (
            stable_failures
            or stable_section_differences
            or stable_section_assignment_differences
            or stable_count_differences
            or stable_value_differences
        )
        return {
            "verdict": "pass" if passed else "difference",
            "stable_gate_verdict": "pass" if passed else "difference",
            "comparator_mode": mode,
            "comparator_source_sha256": _comparator_source_hash(),
            "reference_generated_at": reference.get("generated_at"),
            "candidate_generated_at": candidate.get("generated_at"),
            "capture_skew_seconds": capture_skew,
            "tolerance": tolerance,
            "identity_evidence": evidence,
            "stable_gate_failures": stable_failures,
            "stable_count_differences": stable_count_differences,
            "stable_section_differences": stable_section_differences,
            "stable_section_assignment_differences": (
                stable_section_assignment_differences
            ),
            "stable_calculated_value_differences": stable_value_differences,
            "shared_stable_ranked_symbols": len(shared_stable),
            "dynamic_capture_differences": {
                "count_differences": dynamic_count_differences,
                "section_differences": dynamic_section_differences,
                "calculated_value_differences": dynamic_value_differences,
            },
            "excluded_from_stable_gate": [
                "premarket_gainers",
                "premarket_losers",
                "prior_gainers membership",
                "prior_losers membership",
                "matched_symbols",
                "rejected_symbols",
                *DYNAMIC_FIELDS,
                "opening_focus",
                "catalyst_watch",
                "news_impacts",
            ],
        }

    section_differences = {}
    for section in RANKED_SECTIONS:
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

    coverage_failures = {}
    if failure := _quote_coverage_failure(reference):
        coverage_failures["reference"] = failure
    if failure := _quote_coverage_failure(candidate):
        coverage_failures["candidate"] = failure

    mover_differences = {}
    for section in ("movers_up", "movers_down"):
        if reference.get(section, []) != candidate.get(section, []):
            mover_differences[section] = {
                "reference": reference.get(section, []),
                "candidate": candidate.get(section, []),
            }

    opening_focus_detail_differences = None
    if reference.get("opening_focus", []) != candidate.get("opening_focus", []):
        opening_focus_detail_differences = {
            "reference": reference.get("opening_focus", []),
            "candidate": candidate.get("opening_focus", []),
        }

    news_differences = []
    for symbol in shared:
        left = reference_snapshots[symbol].get("news")
        right = candidate_snapshots[symbol].get("news")
        if left != right:
            news_differences.append(
                {"symbol": symbol, "reference": left, "candidate": right}
            )

    passed = not (
        section_differences
        or mover_differences
        or opening_focus_detail_differences
        or value_differences
        or news_differences
        or count_differences
        or coverage_failures
    )
    return {
        "verdict": "pass" if passed else "difference",
        "stable_gate_verdict": "pass" if passed else "difference",
        "comparator_mode": mode,
        "comparator_source_sha256": _comparator_source_hash(),
        "reference_generated_at": reference.get("generated_at"),
        "candidate_generated_at": candidate.get("generated_at"),
        "capture_skew_seconds": capture_skew,
        "tolerance": tolerance,
        "count_differences": count_differences,
        "coverage_failures": coverage_failures,
        "section_differences": section_differences,
        "mover_differences": mover_differences,
        "opening_focus_detail_differences": opening_focus_detail_differences,
        "calculated_value_differences": value_differences,
        "news_differences": news_differences,
        "shared_ranked_symbols": len(shared),
        "excluded_from_gate": [],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--tolerance", type=float, default=0.01)
    parser.add_argument(
        "--mode",
        choices=("strict", "sequential-skew-aware"),
        default="strict",
    )
    parser.add_argument("--reference-config", type=Path)
    parser.add_argument("--candidate-config", type=Path)
    parser.add_argument("--reference-universe-dir", type=Path)
    parser.add_argument("--candidate-universe-dir", type=Path)
    parser.add_argument("--input-manifest", type=Path)
    args = parser.parse_args()
    identity_evidence = None
    if args.mode == "sequential-skew-aware":
        evidence_paths = (
            args.reference_config,
            args.candidate_config,
            args.reference_universe_dir,
            args.candidate_universe_dir,
            args.input_manifest,
        )
        if not all(evidence_paths):
            parser.error("sequential-skew-aware mode requires config and universe evidence paths")
        identity_evidence = build_identity_evidence(
            reference_config=args.reference_config,
            candidate_config=args.candidate_config,
            reference_universe_dir=args.reference_universe_dir,
            candidate_universe_dir=args.candidate_universe_dir,
            input_manifest=args.input_manifest,
        )
    result = compare_reports(
        json.loads(args.reference.read_text()),
        json.loads(args.candidate.read_text()),
        tolerance=args.tolerance,
        mode=args.mode,
        identity_evidence=identity_evidence,
    )
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()

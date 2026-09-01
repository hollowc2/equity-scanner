import hashlib

import yaml

from equity_scanner.parity import (
    FROZEN_INPUT_FILES,
    build_identity_evidence,
    compare_reports,
)


def report(symbols=("WIN",), price=10.0):
    snapshots = [{"symbol": symbol, "price": price, "prior_day_pct": 4.0} for symbol in symbols]
    return {
        "generated_at": "2026-08-27T09:00:00-04:00",
        "scanned_symbols": 2,
        "matched_symbols": len(symbols),
        "rejected_symbols": {},
        "prior_gainers": snapshots,
        "prior_losers": [],
        "premarket_gainers": snapshots,
        "premarket_losers": [],
    }


def test_identical_reports_pass():
    result = compare_reports(report(), report())
    assert result["verdict"] == "pass"
    assert len(result["comparator_source_sha256"]) == 64


def test_ranking_and_calculated_value_differences_are_reported():
    result = compare_reports(report(("WIN", "ALT")), report(("ALT", "WIN"), price=11.0))
    assert result["verdict"] == "difference"
    assert "prior_gainers" in result["section_differences"]
    assert any(item["field"] == "price" for item in result["calculated_value_differences"])


def test_sequential_mode_records_dynamic_changes_without_failing_stable_gate():
    reference = report()
    candidate = report(price=11.0)
    candidate["generated_at"] = "2026-08-27T09:07:30-04:00"
    candidate["matched_symbols"] = 2
    candidate["premarket_gainers"] = []
    evidence = {
        "config_equal": True,
        "universe_files_complete": True,
        "universes_equal": True,
        "input_manifest_verified": True,
    }

    result = compare_reports(
        reference,
        candidate,
        mode="sequential-skew-aware",
        identity_evidence=evidence,
    )

    assert result["stable_gate_verdict"] == "pass"
    assert result["capture_skew_seconds"] == 450.0
    assert result["dynamic_capture_differences"]["section_differences"]
    assert result["dynamic_capture_differences"]["count_differences"]
    assert result["dynamic_capture_differences"]["calculated_value_differences"]


def test_sequential_mode_fails_on_stable_ranking_difference():
    evidence = {
        "config_equal": True,
        "universe_files_complete": True,
        "universes_equal": True,
        "input_manifest_verified": True,
    }
    result = compare_reports(
        report(("WIN", "ALT")),
        report(("ALT", "WIN")),
        mode="sequential-skew-aware",
        identity_evidence=evidence,
    )
    assert result["stable_gate_verdict"] == "difference"
    assert "prior_gainers" in result["stable_section_differences"]


def test_sequential_mode_applies_prior_day_absolute_tolerance():
    evidence = {
        "config_equal": True,
        "universe_files_complete": True,
        "universes_equal": True,
        "input_manifest_verified": True,
    }
    within_tolerance = report()
    within_tolerance["prior_gainers"][0]["prior_day_pct"] = 4.01
    outside_tolerance = report()
    outside_tolerance["prior_gainers"][0]["prior_day_pct"] = 4.011

    passing = compare_reports(
        report(),
        within_tolerance,
        mode="sequential-skew-aware",
        identity_evidence=evidence,
    )
    failing = compare_reports(
        report(),
        outside_tolerance,
        mode="sequential-skew-aware",
        identity_evidence=evidence,
    )

    assert passing["stable_gate_verdict"] == "pass"
    assert failing["stable_gate_verdict"] == "difference"
    assert failing["stable_calculated_value_differences"] == [
        {
            "symbol": "WIN",
            "field": "prior_day_pct",
            "reference": 4.0,
            "candidate": 4.011,
        }
    ]


def test_identity_evidence_records_and_verifies_frozen_hashes(tmp_path):
    reference_config = tmp_path / "equity_scan.reference.yaml"
    candidate_config = tmp_path / "equity_scan.candidate.yaml"
    reference_config.write_text(
        yaml.safe_dump({"universe_dir": "/host", "batch_size": 100})
    )
    candidate_config.write_text(
        yaml.safe_dump({"universe_dir": "/container", "batch_size": 100})
    )
    for name in FROZEN_INPUT_FILES[2:]:
        (tmp_path / name).write_text(f"{name}\n")

    manifest = tmp_path / "input.sha256"
    manifest.write_text(
        "".join(
            f"{hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()}  {name}\n"
            for name in FROZEN_INPUT_FILES
        )
    )

    evidence = build_identity_evidence(
        reference_config=reference_config,
        candidate_config=candidate_config,
        reference_universe_dir=tmp_path,
        candidate_universe_dir=tmp_path,
        input_manifest=manifest,
    )

    assert evidence["config_equal"] is True
    assert evidence["input_manifest_verified"] is True
    assert evidence["input_manifest"]["verified_sha256"] == evidence["input_manifest"][
        "expected_sha256"
    ]


def test_sequential_mode_fails_closed_on_incomplete_candidate_coverage():
    evidence = {
        "config_equal": True,
        "universe_files_complete": True,
        "universes_equal": True,
        "input_manifest_verified": True,
    }
    candidate = report()
    candidate["scanned_symbols"] = 1
    candidate["quote_coverage"] = {
        "complete": False,
        "verdict": "incomplete",
        "requested_count": 2,
        "returned_count": 1,
        "stale_retained_count": 1,
        "unavailable_count": 1,
        "failed_batch_count": 1,
        "requested_symbols": ["WIN", "MISS"],
        "returned_symbols": ["WIN"],
        "stale_retained_symbols": ["WIN"],
        "unavailable_symbols": ["MISS"],
        "failed_batches": [{"batch_index": 1, "symbols": ["MISS"]}],
    }

    result = compare_reports(
        report(),
        candidate,
        mode="sequential-skew-aware",
        identity_evidence=evidence,
    )

    assert result["stable_gate_verdict"] == "difference"
    assert result["stable_gate_failures"]["quote_coverage"]["reason"] == (
        "incomplete_quote_coverage"
    )


def test_complete_candidate_coverage_does_not_change_strict_comparison():
    candidate = report()
    candidate["quote_coverage"] = {
        "complete": True,
        "verdict": "complete",
        "requested_count": 2,
        "returned_count": 2,
        "stale_retained_count": 1,
        "unavailable_count": 0,
        "failed_batch_count": 0,
        "requested_symbols": ["WIN", "OTHER"],
        "returned_symbols": ["WIN", "OTHER"],
        "stale_retained_symbols": ["WIN"],
        "unavailable_symbols": [],
        "failed_batches": [],
    }

    result = compare_reports(report(), candidate)

    assert result["verdict"] == "pass"
    assert result["coverage_failures"] == {}


def test_strict_mode_fails_when_declared_coverage_is_incomplete():
    candidate = report()
    candidate["scanned_symbols"] = 1
    candidate["quote_coverage"] = {
        "complete": False,
        "verdict": "incomplete",
        "requested_count": 2,
        "returned_count": 1,
        "stale_retained_count": 0,
        "unavailable_count": 1,
        "failed_batch_count": 0,
        "requested_symbols": ["WIN", "MISS"],
        "returned_symbols": ["WIN"],
        "stale_retained_symbols": [],
        "unavailable_symbols": ["MISS"],
        "failed_batches": [],
    }

    result = compare_reports(report(), candidate)

    assert result["verdict"] == "difference"
    assert result["coverage_failures"]["candidate"]["unavailable_symbols"] == ["MISS"]


def test_comparator_rejects_internally_inconsistent_coverage():
    candidate = report()
    candidate["quote_coverage"] = {
        "complete": True,
        "verdict": "complete",
        "requested_count": 2,
        "returned_count": 2,
        "stale_retained_count": 0,
        "unavailable_count": 0,
        "failed_batch_count": 0,
        "requested_symbols": ["WIN", "OTHER"],
        "returned_symbols": ["WIN"],
        "stale_retained_symbols": [],
        "unavailable_symbols": [],
        "failed_batches": [],
    }

    result = compare_reports(report(), candidate)

    assert result["coverage_failures"]["candidate"]["reason"] == (
        "invalid_quote_coverage"
    )

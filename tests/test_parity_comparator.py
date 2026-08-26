from equity_scanner.parity import compare_reports


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
    assert compare_reports(report(), report())["verdict"] == "pass"


def test_ranking_and_calculated_value_differences_are_reported():
    result = compare_reports(report(("WIN", "ALT")), report(("ALT", "WIN"), price=11.0))
    assert result["verdict"] == "difference"
    assert "prior_gainers" in result["section_differences"]
    assert any(item["field"] == "price" for item in result["calculated_value_differences"])

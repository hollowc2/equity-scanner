from __future__ import annotations

import argparse
from pathlib import Path

import yaml

parser = argparse.ArgumentParser()
parser.add_argument("source", type=Path)
parser.add_argument("destination", type=Path)
parser.add_argument("universe_dir")
parser.add_argument("--custom-watchlist")
parser.add_argument("--report-dir", default="reports/equity_scans")
args = parser.parse_args()

source = args.source
destination = args.destination
config = yaml.safe_load(source.read_text()) or {}
config["universe_dir"] = args.universe_dir
config["custom_watchlist"] = args.custom_watchlist or f"{args.universe_dir}/custom.txt"
config["batch_size"] = 100
config.setdefault("filters", {})["min_rvol"] = 0.0
config["rvol_fetch_concurrency"] = 4
config["include_movers"] = False
limits = config.setdefault("limits", {})
for name, cap in {
    "prior_gainers": 15,
    "prior_losers": 15,
    "premarket_gainers": 15,
    "premarket_losers": 15,
    "opening_focus": 12,
}.items():
    limits[name] = min(int(limits.get(name, cap)), cap)
config.setdefault("news", {})["enabled"] = False
config["report_dir"] = args.report_dir
destination.write_text(yaml.safe_dump(config, sort_keys=False))

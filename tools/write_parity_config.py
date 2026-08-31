from __future__ import annotations

import sys
from pathlib import Path

import yaml

source, destination, universe_dir = map(Path, sys.argv[1:4])
config = yaml.safe_load(source.read_text()) or {}
config["universe_dir"] = "/app/parity-input"
config["custom_watchlist"] = "/app/parity-input/custom.txt"
config["batch_size"] = 100
config.setdefault("filters", {})["min_rvol"] = 0.0
config["rvol_fetch_concurrency"] = 4
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
config["report_dir"] = "reports/equity_scans"
destination.write_text(yaml.safe_dump(config, sort_keys=False))

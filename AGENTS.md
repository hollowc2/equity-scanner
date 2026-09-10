# AGENTS.md

EquityScanner is read-only market-data software. It must never contain account,
order-routing, position-management, Schwab OAuth, or token-management code.

- Runtime code lives in `src/equity_scanner/`; tests live in `tests/`.
- Schwab data must come only through the pinned SchwabGateway SDK.
- Keep gateway credentials in environment variables or external secret files. Never
  print, copy, commit, rotate, or rewrite them.
- External notification is disabled by `--dry-run`; prefer dry-run during development.
- Run `uv run pytest` and `uv run ruff check .` after Python changes.
- Helios checks are read-only unless a separate request explicitly approves deployment.

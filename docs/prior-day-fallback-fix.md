# Missing prior-day evidence: fix

On September 30, 2026, production completed its scan and notification delivery,
but no September 29 morning-closes file existed. The stored-closes calculation
therefore supplied zero prior-day changes. Daily history supplied 36 changes;
other symbols retained the live quote percentage, which can include today's
premarket trades. CAG and BMNR consequently showed the same prior-day and gap
percentage without evidence that those represented separate sessions.

## Behavior

During premarket, yesterday's change must come from consecutive saved morning
closes or completed daily history. Without either, `prior_day_pct` is `null` in
JSON and `prior unavailable` in text, with a `prior_day_unavailable` flag. Unknown
changes cannot enter yesterday's gainers/losers, contribute prior-day score or
sector membership, or produce continuation/fade reasons. A known zero remains
zero. Premarket price, gap, volume and RVOL remain independently usable.

The bounded history pass also includes the top candidates selected by the existing
news-candidate limit, including names omitted from yesterday's lists because their
prior change is unknown. Completed history overrides saved-closes changes. Empty
or failed history preserves the unknown state. No universe-wide history fan-out
or archive-derived reconstruction is introduced.

Coverage logs report known and unavailable prior-day values among matched
snapshots and warn when premarket prior-day coverage is incomplete. Saving today's
morning closes continues even when yesterday's file is absent, allowing coverage
to recover on the next trading day. A missing file still limits yesterday's lists;
the fix prevents that limitation from silently becoming false data.

## Verification and rollout

Regression tests cover missing closes plus empty history, valid saved closes,
unknown versus zero versus negative prior moves, text/JSON output, and independent
premarket gap ranking. Run `uv run pytest` and `uv run ruff check .`.

This change is local. A separately approved Helios deployment is required for
production. After deployment, verify prior-day coverage logs, consecutive morning
closes files, archives, and notification delivery. Historical reports remain intact.

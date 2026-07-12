# Add Reset Primer Routing

## Why

Accounts that have just reset can remain at 100% remaining with a weekly reset timer close to seven days. Normal routing strategies may leave those accounts untouched when another strategy signal is stronger. Operators need a small global primer so newly reset accounts receive at least one request and start moving off the untouched 100% state.

## What Changes

- Add a routing pre-selection step that runs before `usage_weighted`, `capacity_weighted`, `high_waterline`, and `primary_drain`.
- Prefer eligible accounts with explicit secondary usage at 0% and a secondary reset at least 24 hours away.
- Avoid immediately repeating the same primer account by respecting a short recent-selection cooldown.

## Impact

- Fully reset accounts can be selected before the active routing strategy.
- Accounts with missing secondary usage data, near reset, unavailable status, cooldown, or recent local selection are not primer candidates.

# Anchor Account Quota Timeline Resets

## Why

The Accounts quota timeline should make the 5h bars line up with the actual primary quota reset cycle. Fixed epoch-aligned 5h buckets can make the first bar after a reset appear offset from the point where remaining quota returned to 100%.

## What Changes

- Detect primary quota resets from primary usage history when used percent drops.
- Prefer `reset_at - window_minutes` as the new 5h bucket anchor when available.
- Rebuild subsequent 5h timeline buckets from that anchor.
- Keep weekly remaining and 7d reset markers mapped onto the resulting timeline buckets.

## Impact

- Changes only the account trends quota timeline API shape values, not field names.
- The chart can render short partial buckets around reset boundaries.

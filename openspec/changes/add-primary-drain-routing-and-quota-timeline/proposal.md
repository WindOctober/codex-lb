# Add Primary Drain Routing and Quota Timeline

## Why

Operators need a routing mode that intentionally concentrates traffic onto accounts that are already close to consuming their 5h quota, so a batch of primary windows can be exhausted and manually reset with less probabilistic spreading. The existing capacity and high-waterline strategies optimize balance, not deliberate drain-down.

The account usage detail also needs a coarser operational view than the current sparkline and gated-model quota rows: the operator wants to see the last 7 days in 5h buckets, including primary-window usage volume, the weekly remaining quota after each bucket, and weekly reset points.

## What Changes

- Add `primary_drain` as a first-class dashboard routing strategy.
- Route `primary_drain` deterministically by derived recent primary-window drain pressure, then stable tie-breakers, while keeping the existing hard eligibility, health-tier, group/source, model, sticky, and concurrency constraints.
- Fall back to capacity-weighted selection when no primary-drain candidate has a meaningful drain signal.
- Extend account trend data with a 7-day, 5h-bucket quota timeline.
- Hide the known GPT-5.3-Codex-Spark additional-quota row in the account usage panel when the quota timeline is available, so the new view becomes the primary operational surface for that concern.

## Impact

- Backend routing enum/settings validation changes.
- Account trend response schema grows additively.
- Frontend settings labels and account usage chart change.
- No database schema migration is required because routing strategy is stored as text.

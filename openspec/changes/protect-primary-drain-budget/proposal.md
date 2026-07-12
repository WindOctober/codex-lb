# Remove Primary Drain Budget Prefilter

## Why

`primary_drain` is an explicit drain mode. Operators use it to continue consuming selected accounts even when their weekly/secondary window is near exhaustion. The previous budget-safe prefilter stopped flagged accounts at the sticky reallocation budget threshold, so accounts with remaining quota could stop receiving new sessions before they were fully drained.

## What Changes

- Do not apply the sticky reallocation budget threshold prefilter before `primary_drain` selection.
- Keep the budget-safe prefilter for non-`primary_drain` routing strategies.
- Add regression coverage showing primary drain can select an over-threshold drain target.

## Impact

- `primary_drain` can continue routing new sessions to flagged or high drain-score accounts above the configured budget threshold.
- Non-primary-drain strategies keep their existing budget-safe protection.

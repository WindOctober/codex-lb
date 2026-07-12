# Change: Widen HTTP bridge pressure capacity

## Why

High-concurrency Codex batch runs can create many soft prompt-cache HTTP bridge sessions. The existing pressure-eviction capacity calculation only counts accounts already present in the local bridge session pool, so a burst can hit the pressure threshold before low-use eligible accounts have been introduced. That causes premature prompt-cache eviction and can race with in-flight bridge submissions, surfacing `session_retired_after_admission` 502s even though more accounts can still serve the model.

## What Changes

- Compute HTTP bridge pressure capacity from the current routable, budget-safe account pool when no explicit max-session cap is configured.
- Preserve the existing session-pool-based capacity only as a fallback when routable capacity cannot be resolved.
- Keep pressure eviction focused on idle soft prompt-cache batch sessions.

## Impact

- High-concurrency bursts can use the capacity of eligible accounts before pressure eviction starts.
- Accounts that are outside API-key scope, model support, group filters, or budget-safe routing are not counted for this pressure capacity.

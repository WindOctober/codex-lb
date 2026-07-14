## Why

Production Codex alpha search requests occasionally need more than the general 480-second proxy budget, and seven recent requests were terminated at exactly that boundary. The first upstream attempt currently receives the entire request budget, so a read timeout leaves no time for the already-designed second-account failover and surfaces a misleading `Proxy request budget exhausted` error.

## What Changes

- Give Codex alpha search an explicit configurable total request budget independent of the general proxy budget.
- Bound each upstream account attempt separately so a stalled first account leaves time for the existing second-account failover.
- Preserve the latest upstream timeout/error when bounded attempts are exhausted instead of replacing it with a generic budget error unless the total deadline truly expires outside an attempt.
- Add configuration and regression coverage for budget selection, timeout propagation, and account failover.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `codex-search-compat`: Alpha search uses dedicated total and per-account budgets that make bounded failover effective.

## Impact

Affected code is limited to typed runtime settings, the alpha search runtime budget calculation, example/local configuration, and search tests. The endpoint and JSON schemas, account routing order, model policy, Caddy, and Responses streaming behavior do not change.

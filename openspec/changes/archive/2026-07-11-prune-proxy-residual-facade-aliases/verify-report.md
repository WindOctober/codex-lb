# Verification Report: prune-proxy-residual-facade-aliases

## Summary

| Dimension | Status |
| --- | --- |
| Completeness | 8/8 tasks complete; 2/2 modified requirements implemented |
| Correctness | 5/5 modified scenarios covered by before/after tests, direct owner assertions, and isolated runtime validation |
| Coherence | All 4 design decisions followed |

Final assessment: all checks attributable to this change passed. The change is ready to sync and archive.

## Completeness

- The proxy facade no longer imports or exposes `_request_budget_seconds` or `_usage_window_row_from_entry`.
- Canonical definitions and live callers remain in `_service/budget.py` and `_service/rate_limits.py`.
- `REMOVED_PROXY_REDUNDANT_NAMES` now ratchets both aliases alongside the earlier proven-redundant names.
- No new runtime module, wrapper, protocol, or compatibility layer was added.
- The service facade decreased from 1,742 to 1,736 lines.

## Correctness

### Requirement and scenario mapping

- Proven redundant proxy facade symbols remain absent:
  - Implementation: only the two same-name service import blocks were removed and both names were added to the existing architecture absent-name set.
  - Coverage: pre-change import smoke confirmed both aliases resolved to their canonical owners; post-change import smoke confirmed both facade attributes are absent and both owners remain callable; architecture tests pass.
- Canonical proxy behavior owners are preserved:
  - Implementation: canonical budget and rate-limit modules were not edited.
  - Coverage: the identical seven-test pre/post matrix passed; direct assertions cover configured/default/absolute request deadlines and complete usage-row projection fields.

### Focused results

- Frozen proxy architecture, additional-limit aggregation, and WebSocket request-budget matrix: 7 passed before the edit and the identical 7 passed after it.
- Direct canonical-owner behavior assertions passed before and after for default, configured, and absolute request deadlines plus usage-row field projection.
- Post-change import smoke verified service alias absence and canonical owner presence.
- Module-aware AST checks found one canonical budget definition with one live load and one canonical usage-row definition with two live loads.

### Static and specification checks

- Ruff formatting left both changed Python files unchanged; Ruff format-check and lint passed.
- `py_compile` passed for the facade, both canonical owners, architecture test, and focused rate-limit test.
- `.venv/bin/ty check --python .venv/bin/python` passed for both canonical owners and focused tests.
- Import, AST reference, and scoped whitespace checks passed.
- The change passed strict OpenSpec validation and reported 8/8 tasks complete.
- Main `openspec validate --specs` passed 26/26 before delta sync.
- Isolated keepalive E2E on backend/mock ports 3456/3460 passed with a one-second silent upstream and exactly one upstream request.

## Coherence

- Removal required zero service loads, zero repository facade references including strings, and zero architecture or normative OpenSpec contract.
- Only facade import nodes were removed; canonical algorithms, inputs, outputs, callers, and exceptions remain unchanged.
- The existing absent-name ratchet captures the architectural intent without creating a cleanup-specific abstraction.
- All other audited aliases and all 49 local `ProxyService` methods were retained because they have callers, compatibility evidence, or active-change overlap.
- No request-budget, rate-limit, account routing, continuity, concurrency, streaming, WebSocket, persistence, configuration, or deployment behavior changed.

## Issues

### Critical

None.

### Warning

None attributable to this change.

### Suggestion

Do not create a standalone extraction for the remaining 11-line WebSocket API-key policy refresh while enforced-service-tier work is active; consolidate it only as part of a later coherent API-key lifecycle change.

## External Baselines

- Full-file `service.py` Ty still reports the known `_HTTPBridgeStreamService` protocol diagnostic; all change-scoped type targets pass.
- Repository-wide historical strict-main-spec Purpose warnings remain separate documentation debt; required non-strict main-spec validation passes.

## Runtime Safety

- Immediately before execution, the E2E script was re-audited: it contains no drain, Caddy, 2455, or 2456 action; defaults are isolated ports 3456/3460 and cleanup targets the explicit isolated PID file and mock PID.
- Script hashes remained `2771cca0b6293ee6aeaadc55d1548ff9586b050d74cd231f630fdd9d77b5b0cc` for the E2E script and `b70d8925cc28e6aefd0ed5d263e7f36dd0ba89872d205fa0e5f2739b0576f84b` for the restart helper.
- Before and after isolated validation, Caddy remained PID 14200 on port 2455 and the primary backend remained PID 2689757 on port 2456; both `/health/live` endpoints remained healthy.
- Isolated backend PID 3673580, its temporary directory, and ports 3456/3460 were cleared after validation.
- No drain endpoint, Caddy action, primary-backend restart, or gateway retarget occurred.

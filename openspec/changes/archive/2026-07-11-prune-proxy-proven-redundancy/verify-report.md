# Verification Report: prune-proxy-proven-redundancy

## Summary

| Dimension | Status |
| --- | --- |
| Completeness | 9/9 tasks complete; 3/3 requirements implemented |
| Correctness | 7/7 scenarios covered by focused tests, static checks, and isolated runtime validation |
| Coherence | All 6 design decisions followed |

Final assessment: all checks attributable to this change passed. The change is ready to sync and archive.

## Completeness

- The proxy facade no longer defines or imports the five audited redundant names: `_resolve_prompt_cache_key`, `_account_supports_http_bridge_request_model`, `_match_websocket_request_state_for_previous_response_error`, `_TEXT_DELTA_EVENT_TYPES`, and `_call_core_compact_responses`.
- The HTTP bridge session-acquisition module no longer imports `logging` or initializes an unused module logger; its structured timing and continuity observability remains unchanged.
- `_core_compact_responses_compatible` now performs the identical optional-keyword adapter call directly while resolving the service-module `core_compact_responses` binding at call time.
- Architecture ratchets require all removed names to remain absent while retaining the existing required-facade and canonical-owner checks.
- Canonical affinity, model-support, WebSocket previous-response matching, streaming text-event, compact provider, and HTTP bridge session-acquisition implementations remain in their existing owners.
- The cleanup reduced `service.py` from 1,794 to 1,742 lines and `session_acquire.py` from 1,101 to 1,098 lines without adding a replacement abstraction.

## Correctness

### Requirement and scenario mapping

- Proven redundant proxy facade symbols remain absent:
  - Implementation: audited facade definitions/imports were deleted and `REMOVED_PROXY_REDUNDANT_NAMES` plus `REMOVED_SESSION_ACQUIRE_NAMES` were added to architecture tests.
  - Coverage: module-aware reference checks, import smoke, and four architecture tests verify absence while the existing facade ratchets still pass.
- Canonical proxy behavior owners are preserved:
  - Implementation: only dead service wrappers and their dedicated imports were removed; canonical affinity, HTTP bridge policy/session, WebSocket event, streaming, forwarding, and observability owners were not changed.
  - Coverage: the identical frozen 15-test pre/post matrix passed after the cleanup; the expanded canonical-consumer matrix recorded ten passes, with one unrelated historical HTTP bridge fixture failure described below.
- Compact transport compatibility uses one facade layer:
  - Implementation: the single-caller passthrough was folded into `_core_compact_responses_compatible` with the same `_call_with_supported_optional_kwargs` arguments and dynamic global transport lookup.
  - Coverage: ten focused compact core-replacement, timeout/budget, provider fail-closed, service-tier/logging, request-ID, and selection regressions passed.

### Focused results

- Frozen affinity, model-support, session-acquisition, WebSocket previous-response, compact, architecture, and import matrix: 15 passed before the edit and the identical 15 passed after it.
- Focused compact compatibility/provider/budget matrix: 10 passed.
- Expanded mixed canonical-consumer matrix: 10 passed and one unrelated baseline failure caused by the pre-existing missing `service._http_bridge_soft_shard_index` compatibility attribute.
- Proxy architecture suite: 4 passed.
- Import smoke confirmed all five service names and both session-acquisition logger names are absent.

### Static and specification checks

- Ruff format and lint passed for `service.py`, `session_acquire.py`, and the architecture test.
- `py_compile` passed for all change-scoped Python files.
- `.venv/bin/ty check --python .venv/bin/python` passed for the session-acquisition module and architecture test.
- Module-aware reference checks found zero remaining facade references and confirmed the canonical owners still have real consumers.
- Scoped whitespace checks passed.
- The change passed strict OpenSpec validation.
- Main `openspec validate --specs` passed 24/24 before capability sync.
- Isolated keepalive E2E on backend/mock ports 3456/3460 passed with a one-second silent upstream and exactly one upstream request.

## Coherence

- Deletion required zero internal loads, zero external repository references, and no architecture or normative OpenSpec contract; caller count alone was not treated as sufficient evidence.
- Each removed wrapper or alias was deleted together with only the import dedicated to it; canonical implementations remain untouched.
- Independently owned text-delta constants with real callers remain in streaming, observability, forwarding, and HTTP bridge modules.
- Compact still supports service-module transport monkeypatching and legacy signatures because the dynamic lookup and optional-keyword adapter remain at the required facade hook.
- Response-create diagnostic helpers and other compatibility-protected facade exports remain intact despite low caller counts.
- No selection, failover, continuity, owner-forwarding, account-health, API, payload, persistence, configuration, or deployment behavior changed.

## Issues

### Critical

None.

### Warning

None attributable to this change.

### Suggestion

None required before archive.

## External Baselines

- The expanded mixed matrix still has the pre-existing `test_get_or_create_http_bridge_session_shards_busy_prompt_cache_session` failure because `ProxyService` lacks `_http_bridge_soft_shard_index`; this cleanup neither reads nor edits that attribute or its routing path.
- Full-file `service.py` Ty still reports the existing `_HTTPBridgeStreamService` protocol diagnostic; the change-scoped type targets pass.
- Repository-wide historical strict-main-spec Purpose warnings remain separate documentation debt; required non-strict main-spec validation passes.

## Runtime Safety

- Immediately before execution, the E2E script was re-audited: it contains no drain, Caddy, 2455, or 2456 action; defaults are isolated ports 3456/3460 and cleanup targets the explicit isolated PID file and mock PID.
- The script hashes were `2771cca0b6293ee6aeaadc55d1548ff9586b050d74cd231f630fdd9d77b5b0cc` for the E2E script and `b70d8925cc28e6aefd0ed5d263e7f36dd0ba89872d205fa0e5f2739b0576f84b` for the restart helper.
- Before and after isolated validation, Caddy remained PID 14200 on port 2455 and the primary backend remained PID 2689757 on port 2456; both `/health/live` endpoints remained healthy.
- Isolated backend PID 3635797, its temporary directory, and ports 3456/3460 were cleared after validation.
- No drain endpoint, Caddy action, primary-backend restart, or gateway retarget occurred.

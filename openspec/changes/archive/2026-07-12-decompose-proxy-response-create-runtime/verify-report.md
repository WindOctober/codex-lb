# Verification Report: decompose-proxy-response-create-runtime

## Summary

| Dimension | Status |
| --- | --- |
| Completeness | 9/9 tasks complete; 3/3 requirements implemented |
| Correctness | 7/7 scenarios covered by focused tests and runtime validation |
| Coherence | All 6 design decisions followed |

Final assessment: all checks attributable to this change passed. The change is ready for archive.

## Completeness

- `_ResponseCreateRuntimeService` declares the minimal admission and dynamic size-policy capabilities consumed by preparation.
- `_ResponseCreateRuntimeMixin` owns HTTP bridge preparation, shared request-state construction, and response-create admission acquisition.
- `ProxyService` inherits the mixin, retains dynamic maximum-size and enforcement hooks, and no longer defines the three runtime methods locally.
- Architecture ratchets require the module and mixin, verify method ownership, prohibit facade reintroduction, require both compatibility hooks, preserve direct facade helper exports, and reject reverse imports.

## Correctness

### Requirement and scenario mapping

- Typed boundary and inherited HTTP bridge/WebSocket callers:
  - Implementation: `app/modules/proxy/_service/response_create_runtime.py` and the `_ResponseCreateRuntimeMixin` base on `ProxyService`.
  - Coverage: focused service preparation tests plus HTTP bridge and downstream WebSocket integration tests.
- Payload and request-state semantics:
  - Implementation: the mechanically moved preparation body preserves field removal/addition, metadata, IDs, service tier, reasoning effort, event queue, session normalization, JSON encoding, input count/fingerprint, slimming, logging, and enforcement ordering.
  - Coverage: metadata, serialization, no-session-inference, direct fingerprint assertions, canonical slimming, threshold replacement, oversized HTTP bridge, and oversized WebSocket tests.
- Admission ordering and cleanup:
  - Implementation: the runtime acquires the session gate before shared work admission and retains the `BaseException` cleanup boundary.
  - Coverage: overload release, gate-before-shared-capacity, direct cancellation-after-gate-acquisition, replaced-session, connect/terminal release, and HTTP bridge cancellation tests.

### Focused results

- Initial post-move matrix found one missing facade export; after restoring and ratcheting it, the complete 21-node matrix passed.
- Two downstream fingerprint/continuity consumers passed.
- New direct input-count/fingerprint and cancellation-cleanup assertions passed.
- Canonical response-create policy suite: 5 passed.
- Focused HTTP bridge oversized/slimming/metadata cases: 3 passed.
- Focused WebSocket preparation/oversized/slimming cases: 3 passed.
- Proxy architecture: 4 passed.

### Static and specification checks

- Ruff lint passed for runtime, facade, architecture, and changed focused tests.
- Ruff format passed for runtime, facade, architecture, and the new test hunks.
- `py_compile` passed for all change-scoped Python files.
- `.venv/bin/ty check --python .venv/bin/python` passed for the new typed runtime boundary and architecture test.
- Scoped diff whitespace and dependency-direction checks passed.
- The change passed strict OpenSpec validation.
- The new main capability passed strict validation after sync.
- Main `openspec validate --specs` passed 22/22 after capability sync.
- Isolated keepalive E2E on backend/mock ports 3456/3460 passed with a one-second silent upstream and exactly one upstream request.

## Coherence

- Preparation/admission orchestration is separate from filesystem diagnostics in `_service/response_create.py`.
- Pure metadata, fingerprint, service-tier, slimming, and gate-release helpers remain canonical imports.
- `WorkAdmissionController` construction and mutable ownership remain on `ProxyService`.
- Dynamic size/dump monkeypatch behavior is preserved through service hooks resolved at call time.
- HTTP bridge and WebSocket orchestration continue consuming the same inherited method signatures.
- No account routing, continuity, transport, API, persistence, deployment, or runtime configuration changed.

## Resolved Issue

The first focused run exposed removal of the direct service facade export `_release_websocket_response_create_gate`; one test failed and a lease-GC warning followed from interrupted cleanup. The explicit alias was restored together with `_owner_lookup_session_id_from_headers`, all response-create facade seams were ratcheted, and the full matrix then passed without lease warnings.

## Issues

### Critical

None.

### Warning

None attributable to this change.

### Suggestion

None required before archive.

## External Baselines

- Full-file `service.py` Ty still reports the existing `_HTTPBridgeStreamService` protocol diagnostic; the new runtime boundary passes independently.
- Full `test_proxy_utils.py` formatting still proposes one unrelated pre-existing HTTP bridge line rewrite outside this change's hunks; it was left untouched.
- The repository-wide historical strict-main-spec short-Purpose warnings remain separate documentation debt. Required non-strict main-spec validation passes.

## Runtime Safety

- Before and after isolated validation, Caddy remained PID 14200 on port 2455 and the primary backend remained PID 2689757 on port 2456.
- Both primary `/health/live` endpoints remained healthy.
- The isolated PID and temporary directory were removed, and ports 3456/3460 were clear afterward.
- No Caddy action, drain endpoint, primary-backend restart, or gateway retarget occurred.

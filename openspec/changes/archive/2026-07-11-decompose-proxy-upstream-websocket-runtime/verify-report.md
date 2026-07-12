# Verification Report: decompose-proxy-upstream-websocket-runtime

## Summary

| Dimension | Status |
| --- | --- |
| Completeness | 10/10 tasks complete; 3/3 requirements implemented |
| Correctness | 9/9 scenarios covered by focused tests and isolated runtime validation |
| Coherence | All 6 design decisions followed |

Final assessment: all checks attributable to this change passed. The change is ready to sync and archive.

## Completeness

- `_UpstreamWebSocketRuntimeService` declares the minimal encryption, admission, inherited-open, and replaceable-factory capabilities used by the runtime.
- `_UpstreamWebSocketRuntimeMixin` owns budgeted and unbudgeted upstream WebSocket creation shared by HTTP bridge and downstream WebSocket consumers.
- `ProxyService` inherits the runtime and retains one local static compatibility method that dynamically resolves its module-level `connect_responses_websocket` binding.
- The two creation methods and obsolete top-level compatibility wrapper no longer exist locally in `service.py`; the facade decreased from 1,873 to 1,830 lines.
- Architecture ratchets require the new module, mixin inheritance, method ownership, no facade reintroduction, required compatibility exports, and one-way internal dependencies.
- Direct tests cover supported OAuth and API-key parameter derivation, the legacy factory seam, unsupported-provider fail-fast ordering, and lease cleanup across every factory terminal path.

## Correctness

### Requirement and scenario mapping

- Shared typed runtime boundary:
  - Implementation: `app/modules/proxy/_service/upstream_websocket.py` and the `_UpstreamWebSocketRuntimeMixin` base on `ProxyService`.
  - Coverage: architecture ownership tests, downstream WebSocket connection tests, HTTP bridge session creation tests, and HTTP bridge reconnect tests.
- Provider and socket-factory semantics:
  - Implementation: the runtime imports canonical upstream-account transforms and calls the typed facade factory seam only after provider validation, token decryption, and admission.
  - Coverage: direct OAuth/API-key forwarding tests assert headers, token, account header, base URL, wire API, call count, and lease release; the direct legacy-signature test proves `wire_api` filtering; the unsupported-provider test proves no decrypt, admission, or factory call occurs.
- Budget and admission cleanup:
  - Implementation: the mechanically moved `anyio.fail_after` wrapper preserves timeout translation, and the acquired connection lease remains inside a `try/finally` around the single factory call.
  - Coverage: direct success, `RuntimeError`, `CancelledError`, and timeout tests all assert exactly one release; consumer overload and handshake-budget tests preserve existing errors.

### Focused results

- Direct runtime plus architecture suite: 11 passed (7 direct runtime, 4 architecture).
- Downstream WebSocket and HTTP bridge consumer matrix: 13 passed.
- Core WebSocket transport/egress client suite: 7 passed.
- The recorded pre-change matrix was 8 passed and one existing V1 bridge-reuse failure. Post-change, all eight previously green cases remained green and the V1 case reproduced the same stale-session second-connect assertion after both requests completed successfully.
- The backend bridge-reuse case still failed in its existing fixture helper while parsing `[DONE]`; this occurs outside the extracted creation behavior and is part of the known HTTP bridge fixture/session baseline.

### Static and specification checks

- Ruff format and lint passed for the runtime, facade, architecture test, and direct runtime tests.
- `py_compile` passed for all change-scoped Python files.
- `.venv/bin/ty check --python .venv/bin/python` passed for the new typed runtime boundary and its tests.
- Scoped whitespace and dependency-direction checks passed.
- The change passed strict OpenSpec validation.
- Main `openspec validate --specs` passed 22/22 before capability sync.
- Isolated keepalive E2E on backend/mock ports 3456/3460 passed with a one-second silent upstream and exactly one upstream request.

## Coherence

- Shared socket creation lives in a top-level internal runtime rather than the downstream-only WebSocket connection slice because HTTP bridge also consumes it.
- Account selection, refresh, 401 retry, failover, routing, and account-health mutation remain in their existing consumer owners.
- Per-connection egress selection, handshake headers, transport mechanics, and upstream error mapping remain in the core WebSocket client; its full focused suite passed.
- The runtime has no repository or balancer access and does not import `app.modules.proxy.service`.
- The facade factory method is the only retained compatibility layer; the previous module-level wrapper was removed rather than duplicated.
- No API, payload, persistence, configuration, deployment, or primary runtime behavior changed.

## Issues

### Critical

None.

### Warning

None attributable to this change.

### Suggestion

None required before archive.

## External Baselines

- Full-file `service.py` Ty still reports the existing `_HTTPBridgeStreamService` protocol diagnostic; the new runtime boundary and tests pass independently.
- One focused reconnect test emits an existing account-model test-lease garbage-collection warning after passing; that lease type is outside upstream WebSocket connection admission.
- V1 HTTP bridge reuse still recreates a stale follow-up session and fails its one-connect assertion exactly as recorded before this change.
- The backend HTTP bridge reuse fixture still attempts to JSON-decode the terminal `[DONE]` marker and times out cancelling its fake reader.
- Repository-wide historical strict-main-spec Purpose warnings remain separate documentation debt; required non-strict main-spec validation passes.

## Runtime Safety

- The E2E script was re-audited immediately before execution: it contains no drain or Caddy action and defaults only to isolated ports 3456/3460; cleanup targets the explicit isolated PID file and mock PID.
- Before and after isolated validation, Caddy remained PID 14200 on port 2455 and the primary backend remained PID 2689757 on port 2456.
- Both primary `/health/live` endpoints remained healthy.
- Isolated PID 3548506, its temporary directory, and ports 3456/3460 were cleared after validation.
- No Caddy action, drain endpoint, primary-backend restart, or gateway retarget occurred.

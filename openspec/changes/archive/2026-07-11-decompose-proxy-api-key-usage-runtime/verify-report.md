# Verification Report: decompose-proxy-api-key-usage-runtime

## Summary

| Dimension | Status |
| --- | --- |
| Completeness | 10/10 tasks complete; 2/2 requirements implemented |
| Correctness | 7/7 scenarios covered by focused and representative transport tests |
| Coherence | All 3 design decisions followed |

Final assessment: all checks attributable to this change passed. The change is ready to sync and archive.

## Completeness

- `_ApiKeyUsageRuntimeMixin` owns reservation enforcement, reservation release, compact settlement, and stream settlement behind the narrow `_repo_factory` capability.
- `ProxyService` inherits all four existing method names and no longer implements their bodies locally.
- Reservation error translation, cancellation shielding, finalize-versus-release decisions, model fallback, cached-token accounting, service-tier attribution, warning behavior, and stream success indication remain intact.
- Architecture ratchets require the runtime module and mixin inheritance, keep the four methods out of the facade, and preserve one-way dependency direction.
- Direct runtime tests cover every reservation and settlement terminal path specified by the change.

## Correctness

### Requirement and scenario mapping

- Typed API-key usage responsibility boundary:
  - Implementation: `app/modules/proxy/_service/api_key_usage.py` plus inherited methods on `ProxyService`.
  - Coverage: direct success, rate-limit/auth mapping, release/no-op, architecture ownership, and import-direction checks.
- Transport settlement semantics:
  - Implementation: separate compact and stream settlement methods preserve their distinct response data and return contracts.
  - Coverage: direct compact finalize/release, stream finalize/release/failure isolation, plus representative ordinary streaming, compact, HTTP bridge, and WebSocket consumers.

### Focused results

- Direct API-key usage runtime plus proxy architecture suite: 11 passed (7 direct runtime, 4 architecture).
- Representative ordinary streaming, compact, HTTP bridge retry, WebSocket request preparation, and WebSocket integration matrix: 5 passed.
- The current combined application state passed isolated keepalive E2E on backend/mock ports 3456/3460 with exactly one upstream request.

### Static and specification checks

- Ruff format and lint passed for the runtime, direct tests, and architecture tests.
- `py_compile` passed for all three change-scoped Python files.
- `.venv/bin/ty check --python .venv/bin/python` passed for the runtime, direct tests, and architecture tests.
- The change passed strict OpenSpec validation and reported 10/10 tasks complete.
- Main `openspec validate --specs` passed 25/25 before capability sync.

## Coherence

- One runtime owns the shared reservation lifecycle instead of duplicating repository-backed accounting in individual transports.
- Compact and stream settlement remain separate because their input models and return contracts differ.
- Repository work remains awaited inside cancellation shields; no background queue, retry, batching, or service-lifetime behavior was introduced.
- Existing transport protocols and instance monkeypatch surfaces continue resolving the inherited method names.
- No quota rule, pricing, persistence, routing, retry, payload, endpoint, or error contract changed.

## Issues

### Critical

None.

### Warning

None attributable to this change.

### Suggestion

The adjacent WebSocket API-key policy refresh remains a facade method and is intentionally outside this usage-accounting change. Any consolidation should be specified and verified separately.

## External Baselines

- Full-file `service.py` Ty retains the known `_HTTPBridgeStreamService` protocol diagnostic; the change-scoped type targets pass.
- Repository-wide historical strict-main-spec Purpose warnings remain separate documentation debt; required non-strict main-spec validation passes.

## Runtime Safety

- The current application state was validated with the audited keepalive script on isolated backend/mock ports 3456/3460; cleanup targeted only the explicit isolated PID file and mock PID.
- Before and after validation, Caddy remained PID 14200 on port 2455 and the primary backend remained PID 2689757 on port 2456; both `/health/live` endpoints were healthy.
- Isolated backend PID 3635797, its temporary directory, and ports 3456/3460 were cleared.
- No drain endpoint, Caddy action, primary-backend restart, or gateway retarget occurred.

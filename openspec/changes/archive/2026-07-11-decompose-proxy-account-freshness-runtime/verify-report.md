# Verification Report: decompose-proxy-account-freshness-runtime

## Summary

| Dimension | Status |
| --- | --- |
| Completeness | 10/10 tasks complete; 3/3 requirements implemented |
| Correctness | 9/9 scenarios covered by focused tests and isolated runtime validation |
| Coherence | All 8 design decisions followed |

Final assessment: all checks attributable to this change passed. The change is ready to sync and archive.

## Completeness

- `_AccountFreshnessService` declares only the repository, work-admission, and inherited freshness capabilities used by the runtime.
- `_AccountFreshnessMixin` owns `_ensure_fresh` and `_ensure_fresh_with_budget`, the shared credential-freshness entry points used by six proxy execution paths.
- `ProxyService` inherits the runtime and no longer implements either method locally; the facade decreased from 1,830 to 1,794 lines.
- `AuthManager` and `ACCOUNT_PROVIDER_API_KEY` remain explicit canonical facade aliases, and the repository-HEAD `RefreshError` alias is restored.
- Architecture ratchets require the new module, mixin, method ownership, no facade reintroduction, the three compatibility aliases, and one-way internal dependencies.
- Direct tests cover provider bypass, canonical AuthManager/repository/admission wiring, nested timeout restoration on success, exception, and cancellation, legacy optional-keyword filtering, and facade identity.

## Correctness

### Requirement and scenario mapping

- Shared typed freshness boundary:
  - Implementation: `app/modules/proxy/_service/account_freshness.py` and the `_AccountFreshnessMixin` base on `ProxyService`.
  - Coverage: architecture ownership plus focused ordinary streaming, compact, transcription, downstream WebSocket, HTTP bridge session-create, and HTTP bridge reconnect tests.
- Provider and AuthManager semantics:
  - Implementation: the API-key provider branch remains first; OAuth work enters the existing repository bundle and constructs canonical `AuthManager` with the existing token-refresh admission callback and force value.
  - Coverage: direct zero-side-effect provider test, AuthManager capability test, existing fresh-account admission test, and existing same-account refresh-singleflight test.
- Timeout and compatibility scopes:
  - Implementation: timeout push/reset remains a `try/finally` around repository/AuthManager work; the budget adapter still dynamically invokes `self._ensure_fresh` through the shared optional-keyword policy.
  - Coverage: direct success, `RuntimeError`, and `CancelledError` nested-scope tests; direct legacy-signature test; direct facade identity test; HTTP bridge legacy exception-factory integrations.

### Focused results

- Direct account-freshness plus architecture suite: 11 passed (7 direct runtime, 4 architecture).
- Recorded pre-change matrix: 7 passed and one failure caused by the missing `service.RefreshError` alias.
- The identical post-change matrix: 8 passed; all seven prior passes remained green and the attributable facade failure is repaired.
- Additional refresh failure/timeout consumers across transcription, streaming, downstream WebSocket, and HTTP bridge: 5 passed.
- All three HTTP bridge integrations that construct `proxy_module.RefreshError`: 3 passed.

### Static and specification checks

- Ruff format and lint passed for the runtime, facade, architecture test, and direct runtime tests.
- `py_compile` passed for all change-scoped Python files.
- `.venv/bin/ty check --python .venv/bin/python` passed for the new typed runtime boundary and tests.
- Import smoke verified inherited method ownership and all three canonical facade identities.
- Scoped whitespace and dependency-direction checks passed.
- The change passed strict OpenSpec validation.
- Main `openspec validate --specs` passed 23/23 before capability sync.
- Isolated keepalive E2E on backend/mock ports 3456/3460 passed with a one-second silent upstream and exactly one upstream request.

## Coherence

- The runtime owns only proxy-level freshness orchestration; `AuthManager` still owns refresh necessity, singleflight, admission acquisition during real refresh, OAuth protocol, persistence, and deactivation.
- API-key providers bypass timeout ContextVar and repository work exactly as before.
- Six existing consumer runtimes continue using the inherited method signatures and can monkeypatch them at instance or class level.
- Error classification, account-health mutation, selection, routing, and failover remain untouched because active changes still own those semantics.
- `RefreshError` is a canonical alias only; no exception implementation, catch path, or refresh logic is duplicated.
- No public API, payload, persistence, configuration, deployment, or primary runtime behavior changed.

## Resolved Issue

Earlier proxy decomposition removed `RefreshError` from `app.modules.proxy.service` after production catch sites moved into internal modules. Repository HEAD and three HTTP bridge integration fakes established that the service facade still exposed this canonical exception. The explicit alias and architecture ratchet restore that compatibility; the exact pre-change failing case and its two siblings now pass.

## Issues

### Critical

None.

### Warning

None attributable to this change.

### Suggestion

None required before archive.

## External Baselines

- Full-file `service.py` Ty still reports the existing `_HTTPBridgeStreamService` protocol diagnostic; the new runtime boundary and tests pass independently.
- Repository-wide historical strict-main-spec Purpose warnings remain separate documentation debt; required non-strict main-spec validation passes.

## Runtime Safety

- The E2E script was re-audited immediately before execution: it contains no drain or Caddy action and defaults only to isolated ports 3456/3460; cleanup targets the explicit isolated PID file and mock PID.
- Before and after isolated validation, Caddy remained PID 14200 on port 2455 and the primary backend remained PID 2689757 on port 2456.
- Both primary `/health/live` endpoints remained healthy.
- Isolated PID 3611375, its temporary directory, and ports 3456/3460 were cleared after validation.
- No Caddy action, drain endpoint, primary-backend restart, or gateway retarget occurred.

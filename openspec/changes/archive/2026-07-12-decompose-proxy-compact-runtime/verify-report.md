# Verification Report: decompose-proxy-compact-runtime

## Summary

| Dimension | Status |
| --- | --- |
| Completeness | 10/10 tasks complete; 4/4 requirements implemented |
| Correctness | 11/11 scenarios covered by focused tests and runtime validation |
| Coherence | All 6 design decisions followed |

Final assessment: all checks attributable to this change passed. The change is ready for archive.

## Completeness

- `_CompactRuntimeService` declares the runtime capabilities consumed by compact orchestration, and `_CompactRuntimeMixin` owns the public method implementation in `app/modules/proxy/_service/compact.py`.
- `ProxyService` inherits the mixin, retains dynamic settings and core-transport compatibility hooks, and no longer defines `compact_responses` locally.
- Architecture ratchets require `compact.py`, require `_CompactRuntimeMixin`, prohibit local method reintroduction, and prohibit reverse imports of the service facade.
- Provider and admission terminal paths restore compact timeout override state to its pre-attempt values.

## Correctness

### Requirement and scenario mapping

- Typed runtime boundary and existing caller compatibility:
  - Implementation: `app/modules/proxy/_service/compact.py:73`, `app/modules/proxy/_service/compact.py:155`, `app/modules/proxy/service.py:699`, `app/modules/proxy/service.py:759`, `app/modules/proxy/service.py:809`.
  - Coverage: `tests/unit/test_proxy_architecture.py`, `tests/integration/test_proxy_compact.py`, and the compact-focused service injection tests in `tests/unit/test_proxy_utils.py`.
- Account selection, affinity, request budget, 401 refresh, same-contract retry, transient retry, deterministic failover, and provider fail-closed behavior:
  - Implementation: `app/modules/proxy/_service/compact.py:166` through the terminal state machine.
  - Coverage: `tests/integration/test_proxy_compact.py`, the 8 compact cases in `tests/integration/test_proxy_transient_retry.py`, the 5 focused compact affinity cases in `tests/integration/test_proxy_sticky_sessions.py`, and compact budget/provider cases in `tests/unit/test_proxy_utils.py`.
- API-key settlement, service-tier attribution, terminal logging, and local overload behavior:
  - Implementation: the success/error/finally paths in `app/modules/proxy/_service/compact.py` and the existing API-key/logging service capabilities.
  - Coverage: 12 compact cases in `tests/integration/test_api_keys_api.py`, 7 tests in `tests/unit/test_proxy_api_key_usage.py`, and compact logging/overload cases in `tests/unit/test_proxy_utils.py`.
- Attempt-local timeout cleanup:
  - Implementation: provider validation precedes override installation and the outer cleanup boundary covers admission acquisition at `app/modules/proxy/_service/compact.py:225` through `app/modules/proxy/_service/compact.py:257`.
  - Coverage: strengthened provider fail-closed and local admission overload tests in `tests/unit/test_proxy_utils.py` compare override state before and after the terminal path.

### Verification commands

- `.venv/bin/pytest -q tests/unit/test_proxy_utils.py -k compact` -> 14 passed.
- `.venv/bin/pytest -q tests/integration/test_proxy_compact.py -k 'not success_preserves_compaction_payload'` -> 9 passed, 1 deselected.
- `.venv/bin/pytest -q tests/integration/test_proxy_transient_retry.py -k compact` -> 8 passed, 14 deselected.
- Five focused compact sticky-session node IDs -> 5 passed.
- `.venv/bin/pytest -q tests/integration/test_api_keys_api.py -k compact` -> 12 passed, 36 deselected.
- `.venv/bin/pytest -q tests/unit/test_proxy_api_key_usage.py` -> 7 passed.
- `.venv/bin/pytest -q tests/unit/test_proxy_architecture.py` -> 4 passed.
- Ruff check, scoped Ruff format check, `py_compile`, scoped diff whitespace checks, and `.venv/bin/ty check --python .venv/bin/python app/modules/proxy/_service/compact.py tests/unit/test_proxy_architecture.py` passed.
- `npx --yes @fission-ai/openspec@latest validate decompose-proxy-compact-runtime --type change --strict --no-interactive` passed.
- `npx --yes @fission-ai/openspec@latest validate proxy-compact-runtime --type spec --strict --no-interactive` passed after sync.
- `npx --yes @fission-ai/openspec@latest validate --specs --no-interactive` passed 21/21 main specs after sync.
- `SILENCE_SECONDS=1` keepalive E2E on isolated backend/mock ports 3456/3460 passed with one upstream request; cleanup left both isolated ports closed.

## Coherence

- The state machine moved as one cohesive unit; account and inner-retry loop ordering matches the pre-extraction implementation.
- Pure helpers remain canonical imports rather than duplicated implementations.
- Historical monkeypatch surfaces remain dynamically resolved through `app.modules.proxy.service`.
- The compact module does not import the service facade.
- No endpoint, payload, persistence, deployment, Caddy, or primary-backend configuration changed.

## Issues

### Critical

None.

### Warning

None attributable to this change.

### Suggestion

None required before archive.

## External Baselines

- One compact pass-through integration fixture does not accept the unrelated current egress client's `proxy=` keyword. The failure occurs in the fake session after the extracted compatibility call chain and is not caused by this change.
- One mixed sticky/HTTP-bridge fixture lacks a current HTTP bridge settings field and fails before its compact request.
- Full-file `service.py` Ty reports the existing `_HTTPBridgeStreamService` protocol diagnostic; the compact type boundary passes.
- Strict validation of all main specs promotes 13 historical short-Purpose warnings to failures. Required non-strict main-spec validation passes 21/21 after sync, and both this change and the new main compact spec pass strict validation.
- `test_proxy_utils.py` has one pre-existing formatter-only HTTP bridge line outside the compact hunks; it was left untouched to avoid rewriting another session's change.

## Runtime Safety

- Before and after isolated validation, Caddy remained PID 14200 on port 2455 and the primary backend remained PID 2689757 on port 2456.
- Both primary `/health/live` endpoints remained healthy.
- No Caddy action, drain endpoint, primary-backend restart, or gateway retarget occurred.

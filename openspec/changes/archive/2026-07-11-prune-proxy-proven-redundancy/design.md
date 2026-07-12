## Context

Successive proxy extractions left a small set of private facade symbols whose implementation owners and consumers have already moved. A module-aware AST and repository search across `app/` and `tests/` found no internal loads or external facade references for `_resolve_prompt_cache_key`, `_account_supports_http_bridge_request_model`, the service previous-response matcher alias, or the service `_TEXT_DELTA_EVENT_TYPES`. Their source imports are each used only by the dead facade symbol. HTTP bridge `session_acquire.py` likewise imports `logging` only to initialize a logger that is never loaded.

Separately, `_call_core_compact_responses` has exactly one caller: the required `_core_compact_responses_compatible` service hook. It only delegates through the shared optional-keyword adapter. The hook must remain dynamic because tests and local integrations replace the service-module `core_compact_responses` binding.

Some other zero-caller exports, notably response-create diagnostic helpers, remain protected by existing OpenSpec compatibility requirements and are explicitly out of scope.

## Goals / Non-Goals

**Goals:**

- Remove only private proxy symbols proven to have zero repository callers and no normative compatibility contract.
- Remove imports and logger initialization made dead by those symbols.
- Collapse one proven single-caller compact passthrough without changing dynamic factory replacement or optional-keyword behavior.
- Preserve every canonical implementation and active request behavior.
- Ratchet the removed names so accidental facade reintroduction fails architecture tests.

**Non-Goals:**

- Remove public or architecture/OpenSpec-protected compatibility exports.
- Change affinity, model support, previous-response matching, compact provider policy, request budgets, account selection, routing, failover, or continuity.
- Refactor active routing, owner forwarding, or account-health code.
- Restart, drain, retarget, or otherwise modify the primary backend or Caddy during preflight.

## Decisions

1. Require both zero internal loads and zero external repository references before deleting a private facade symbol. String-based monkeypatch/import references and architecture/OpenSpec membership are included in the audit, not only direct calls.
2. Delete each dead wrapper/alias together with only its now-unused source import. Canonical functions in `_service/affinity.py`, `_service/http_bridge/policy.py`, and `_service/websocket/events.py` remain untouched.
3. Delete the service-local `_TEXT_DELTA_EVENT_TYPES` only. Independently owned constants in streaming, observability, forwarding, and HTTP bridge stream modules have real callers and remain local to those policies.
4. Remove `logging` and the logger initialization only from `session_acquire.py`. Existing structured observability calls remain unchanged.
5. Inline `_call_core_compact_responses` into `_core_compact_responses_compatible` using the identical `_call_with_supported_optional_kwargs` invocation. The method continues resolving service-module `core_compact_responses` at call time, preserving monkeypatch and legacy-signature behavior.
6. Add `REMOVED_PROXY_REDUNDANT_NAMES` to architecture tests and require all names to be absent from the service module. Existing required-facade and canonical-owner ratchets remain authoritative for retained names.

## Risks / Trade-offs

- [Risk] A seemingly private symbol is loaded indirectly by a test or monkeypatch string. -> Audit imports, module attributes, dotted patch strings, architecture sets, and OpenSpec exact-name references before deletion.
- [Risk] Removing a facade wrapper accidentally removes its canonical behavior. -> Delete only service imports dedicated to the wrapper and run focused canonical affinity/model/WebSocket tests.
- [Risk] Compact inlining bypasses dynamic service-module replacement. -> Keep the adapter call inside the local compatibility method and test core monkeypatch, provider fail-closed, and budget behavior.
- [Risk] Cleanup overlaps active routing work. -> Do not edit selection, failover, continuity, owner forwarding, or their canonical modules.
- [Trade-off] Some zero-caller exports remain. -> Contract evidence overrides caller count; protected response-create helpers require a separate specification decision before removal.

## Migration Plan

1. Remove the audited symbols/imports and unused session-acquire logger.
2. Inline the compact passthrough and add absent-name architecture checks.
3. Rerun the frozen focused matrix, static/type/import checks, and OpenSpec validation.
4. Validate an isolated backend on dedicated ports and clean it explicitly, leaving Caddy and the primary backend untouched.

Rollback is a source-only restoration of the removed private wrappers and imports; no API, schema, configuration, or persisted-state migration is involved.

## Open Questions

None.

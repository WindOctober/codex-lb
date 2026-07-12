## Context

After the first proven-redundancy cleanup, a module-aware AST inventory found two more private facade names that are only same-name imports: `_request_budget_seconds` from `_service/budget.py` and `_usage_window_row_from_entry` from `_service/rate_limits.py`. Neither name is loaded inside `service.py`; exact repository search found no facade import, attribute, dotted monkeypatch, architecture membership, or OpenSpec exact-name contract. Their canonical owners remain live: the budget helper feeds `_request_deadline_at`, and the usage-row helper feeds rate-limit header/payload projection.

The remaining local `ProxyService` methods all have production callers. Other zero-load module aliases are compatibility-protected or overlap active service-tier, continuity, topology, durable owner-handoff, account-health, or selection changes.

## Goals / Non-Goals

**Goals:**

- Remove only the two proven-zero-reference service re-exports.
- Preserve canonical budget and rate-limit behavior and ownership unchanged.
- Extend the existing absent-name ratchet with precise evidence.
- Verify the identical canonical behavior matrix and isolated runtime safety.

**Non-Goals:**

- Refactor budget calculation or rate-limit read-model algorithms.
- Remove names based only on low caller count.
- Move the WebSocket API-key policy refresh as a standalone micro-extraction.
- Touch account selection, error/account-health, owner forwarding, topology, durable continuity, or active service-tier work.
- Restart or drain the primary backend, or stop, replace, or retarget Caddy.

## Decisions

1. Require zero service-module loads, zero repository facade references including strings, and zero architecture/normative OpenSpec contracts before deleting either alias. The canonical owner having callers is additional evidence that the facade import is residue rather than the implementation.
2. Delete only the two import statements. `_service/budget.py`, `_service/rate_limits.py`, their callers, inputs, outputs, and exception behavior remain byte-for-byte unchanged.
3. Add both exact names to `REMOVED_PROXY_REDUNDANT_NAMES`. The existing general absent-name assertion is sufficient; no new runtime module, wrapper, or test-only abstraction is introduced.
4. Keep all other audited names. Compatibility evidence and active-change overlap override cosmetic facade-size reduction.

## Risks / Trade-offs

- [Risk] A dotted monkeypatch or import string was missed. -> Audit exact strings across `app/`, `tests/`, and OpenSpec artifacts, and rerun import smoke plus architecture tests.
- [Risk] A canonical implementation is confused with its re-export. -> Remove only service import nodes and verify canonical owner definitions and real callers before and after.
- [Risk] Rate-limit or budget behavior changes through an unrelated concurrent edit. -> Freeze service/architecture fingerprints and a focused pre-change matrix, then compare the same tests after the deletion.
- [Trade-off] Other apparent aliases remain. -> Retaining an evidenced compatibility seam or active-change surface is safer than maximizing deletion count.

## Migration Plan

1. Freeze reference evidence, file fingerprints, and canonical budget/rate-limit tests.
2. Remove the two service imports and extend the architecture absent-name set.
3. Run the identical behavior matrix, static/type/import checks, and strict OpenSpec validation.
4. Validate only on isolated backend/mock ports and clean those explicit processes.

Rollback is a source-only restoration of the two private imports and ratchet entries; there is no API, schema, configuration, or persisted-state migration.

## Open Questions

None.

# Proxy Facade Maintenance Context

## Purpose and Scope

Repeated responsibility extraction can leave private wrappers, aliases, constants, imports, and passthroughs behind after their behavior owners and consumers have moved. This capability defines a conservative evidence threshold for removing that residue without treating facade size as a goal by itself.

It applies only to private symbols proven redundant across code, tests, string-based injection seams, architecture ownership, and normative OpenSpec contracts. It does not authorize removal of public APIs, compatibility-protected exports, canonical implementations, or active routing and continuity behavior.

## Decisions and Constraints

- A private facade symbol is removable only after a module-aware audit finds no internal loads, no repository callers or dotted monkeypatch/import references, and no architecture or normative OpenSpec contract requiring it.
- A dead wrapper or alias is removed together with only its dedicated now-unused import. The canonical implementation remains in its existing owner.
- A same-name re-export is still accidental surface when no repository caller uses the facade path and all live loads resolve inside the canonical owner.
- Similar-looking constants are not assumed duplicate when they belong to independent policies and have real callers.
- Single-caller passthroughs may be folded only when the exact argument forwarding, dynamic lookup, compatibility filtering, and exception behavior remain at the required seam.
- Removed names are added to an architecture absent-name ratchet so accidental facade reintroduction fails quickly.
- Low caller count does not override explicit compatibility evidence. Response-create diagnostic helpers and other protected exports remain until a separate specification decision changes their contract.
- Maintenance changes must avoid active selection, failover, account-health, owner-forwarding, and durable-continuity work unless those behaviors are explicitly in scope.

## Failure Modes

- Text search alone can miss indirect module attribute loads or string-based monkeypatch paths, causing an apparently private deletion to break tests or integrations.
- Deleting a source import together with a wrapper can accidentally remove the canonical implementation if ownership was not established first.
- Confusing a live canonical helper with its zero-reference facade re-export can either preserve residue or delete real behavior; both definitions and loads must be resolved by module.
- Collapsing a passthrough into a statically bound function can break runtime replacement and legacy fake signatures even when ordinary production calls still work.
- Removing an unused conventional logger can still be unsafe if structured observability is routed through it; callers and side effects must be checked independently.
- Broad cleanup during active routing or continuity work can produce ambiguous failures and invalidate before/after attribution.

## Concrete Example

The compact facade formerly called `_call_core_compact_responses`, which had exactly one caller and only delegated to the shared optional-keyword adapter. The redundant function can be removed because `_core_compact_responses_compatible` now performs the identical adapter call while still resolving the service-module `core_compact_responses` binding at invocation time. Tests that replace that binding or expose an older signature therefore continue to work, while the extra facade layer stays absent through an architecture ratchet.

The request-budget and usage-row helpers later remained as same-name facade imports even though their only real loads were already inside `budget.py` and `rate_limits.py`. Removing those imports changes neither deadline calculation nor rate-limit projection; direct owner assertions and identical before/after tests distinguish the dead facade paths from the live implementations.

## Operational Notes

For each cleanup, freeze a focused pre-change matrix covering the canonical owners and compatibility seams, rerun the identical matrix after editing, and pair it with format, lint, compilation, scoped type, import, reference, and OpenSpec checks. Runtime preflight uses only isolated backend/mock ports with explicit cleanup. It must not drain or restart the primary backend, modify Caddy, or retarget the gateway.

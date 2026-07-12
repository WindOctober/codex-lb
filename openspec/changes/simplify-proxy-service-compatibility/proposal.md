## Why

The local proxy service has accumulated compatibility call sites and helpers across multiple HTTP bridge, streaming, and routing changes. Several helpers no longer have callers, while optional-argument compatibility is implemented independently in five places. This increases hot-path reflection code, makes new parameters easy to handle inconsistently, and obscures which bridge controls remain active.

## What Changes

- Remove private proxy helpers and constants that have no production or test callers.
- Centralize callable-signature compatibility into one optional-keyword adapter.
- Reuse the classified upstream failure when choosing same-account versus different-account bridge replay instead of classifying the same error twice.
- Preserve runtime behavior, public APIs, persistence contracts, routing policy, pressure eviction, concurrency leases, and durable continuity.
- Record the larger proxy-service decomposition as a staged follow-up rather than mixing a high-risk file move into this cleanup.

## Impact

- Affected code: `app/modules/proxy/service.py`, `app/modules/proxy/helpers.py`, focused proxy tests.
- No API or database schema changes.
- Expected effect: less duplicated compatibility logic and fewer legacy symbols, with unchanged request behavior.

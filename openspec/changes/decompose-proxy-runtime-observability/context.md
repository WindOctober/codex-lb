## Boundary

This stage moves immutable runtime data contracts and pure calculations. `ProxyService` reads each live session once while holding the appropriate lock and converts it to an immutable observation. Grouping, ordering, capacity mathematics, and final snapshot construction operate only on those observations. Repository access and egress runtime lookup remain in `ProxyService`.

## Compatibility Decision

The service module imports the extracted names directly. Existing imports from `app.modules.proxy.service`, dashboard serialization, type checks, and test patch points therefore continue to resolve to the same class and function objects.

## Follow-up

Stateful HTTP bridge request submission and upstream event handling should be decomposed only after observability no longer depends on service-local data definitions.

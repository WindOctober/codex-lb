## Why

`app/modules/proxy/service.py` owns request orchestration, HTTP bridge lifecycle, runtime observability, routing, streaming, and websocket behavior in one file. The runtime snapshot contracts are immutable data structures with no service-instance behavior, but they currently add substantial surface area to the service module and obscure the orchestration boundary.

## What Changes

- Move HTTP bridge runtime snapshot data contracts and pure health classification into a focused internal runtime module.
- Move session aggregation, grouping, ordering, capacity mathematics, and final snapshot construction into pure runtime helpers.
- Keep the existing `app.modules.proxy.service` names available as compatibility re-exports.
- Preserve dashboard payload fields, runtime metric semantics, routing, bridge lifecycle, durable continuity, and account selection behavior.
- Use this extraction as the first slice of a staged HTTP bridge observability decomposition.

## Impact

- Affected code: `app/modules/proxy/service.py`, `app/modules/proxy/_service/http_bridge/runtime.py`, focused proxy runtime tests.
- No API, database, configuration, or persistence changes.
- Expected effect: a smaller service module and an explicit home for runtime observability contracts and calculations, with unchanged runtime behavior.

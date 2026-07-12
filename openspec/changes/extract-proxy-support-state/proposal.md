## Why

`ProxyService` still defines the shared request, stream settlement, HTTP bridge session, and websocket control state used by several independent proxy domains. Those definitions are not service orchestration, and keeping them at the bottom of `service.py` forces future request-submit, upstream-event, streaming, and websocket modules to depend on the monolith.

## What Changes

- Move low-dependency proxy state types and their directly associated pure helpers into `app/modules/proxy/_service/support.py`.
- Preserve all existing names through imports in `app.modules.proxy.service`.
- Keep field order, defaults, dataclass options, exception payloads, affinity strength rules, and stream error classification unchanged.
- Do not move stateful service methods in this stage.

## Impact

- Affected code: `app/modules/proxy/service.py`, `app/modules/proxy/_service/support.py`, focused proxy state tests.
- No API, database, configuration, routing, or request behavior changes.
- This establishes the shared type boundary required by later HTTP bridge, streaming, and websocket decomposition.

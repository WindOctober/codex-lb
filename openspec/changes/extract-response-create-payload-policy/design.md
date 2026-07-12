## Context

The service-level HTTP bridge/WebSocket path and the core upstream WebSocket client independently implement the same historical tool-output and inline-image slimming algorithm, omission notices, recent-user suffix rule, and payload-too-large error envelope. The service also owns image capability detection and filesystem diagnostics. Existing tests replace threshold and dump-directory constants through both legacy module import paths, so a direct function move would silently break those seams.

## Goals / Non-Goals

**Goals:**

- Establish one pure, dependency-light implementation of shared `response.create` transformations.
- Move request-state size enforcement and diagnostic serialization outside `ProxyService`.
- Preserve service and core-client constants as explicit threshold injection points.
- Preserve helper exports, exact error payloads, omission text, dump metadata, and logging behavior.
- Prohibit extracted modules from importing the proxy service monolith.

**Non-Goals:**

- Change the 12 MiB warning or 15 MiB rejection thresholds.
- Add iterative slimming, compression, truncation, or different history-retention policy.
- Change image-generation detection, upstream transport selection, or 413 handling.
- Change the configured dump location or introduce a new runtime setting.

## Decisions

### Put pure payload policy in `app/core/openai/response_create.py`

The core upstream client and proxy service both need the transformations. A core OpenAI module is the lowest common dependency and avoids a forbidden `core -> modules.proxy` dependency. It owns the error envelope, historical slimming, image detection, summary construction, and JSON byte-size helpers.

Alternative considered: place everything under `app/modules/proxy/_service`. This would force the reusable core upstream client to import an API-facing feature module and invert the repository dependency direction.

### Put request-state diagnostics in `_service/response_create.py`

Filesystem dumps and request-state logging are proxy-runtime concerns. The module consumes a typed protocol describing only the request fields it needs and explicit `warn_bytes`, `max_bytes`, and `dump_dir` arguments. It does not inspect or import `service.py`.

Alternative considered: put diagnostics in the core module. That would mix pure OpenAI payload policy with proxy-specific persistence and logging side effects.

### Retain thin compatibility facades at legacy import paths

`service.py` keeps threshold and dump-directory constants plus thin wrappers that pass their current values into the extracted runtime functions. `app/core/clients/proxy.py` keeps its threshold constants and calls the canonical pure helpers. Existing direct helper imports remain aliases to the canonical function objects where monkeypatch semantics do not require a wrapper.

Alternative considered: move all constants into the canonical module. Existing tests and local integrations patch the two legacy modules independently; centralizing the constants immediately would change those replacement points.

### Ratchet by symbol ownership, not a target line count

Architecture tests require the new modules, reject redefinitions of moved helpers in `service.py` and the core client, and reject imports of the service monolith from extracted modules. File length remains only a broad regression guard, not the design objective.

## Risks / Trade-offs

- [Risk] Legacy monkeypatches no longer affect extracted functions. -> Keep wrappers wherever a function reads replaceable module constants and add focused tests for both legacy paths.
- [Risk] The two duplicate implementations have subtly diverged. -> Compare behavior with shared table-driven tests before deleting either copy; preserve the service behavior where differences are only typing/order.
- [Risk] Moving diagnostic code changes dump names or metadata ordering. -> Move the implementation mechanically and assert decompressed JSON plus metadata fields in existing focused tests.
- [Trade-off] Threshold constants remain declared at two call sites. -> They are intentional transport-specific injection points; the transformation algorithm and default values are canonicalized without changing compatibility.

## Migration Plan

1. Add canonical pure-policy tests and the core module.
2. Switch the core upstream client to canonical helpers while retaining local thresholds.
3. Add the proxy diagnostic module and service facades.
4. Delete duplicate helper bodies and ratchet architecture tests.
5. Run focused payload, HTTP bridge, ordinary streaming, and WebSocket regressions, then isolated backend validation.

Rollback is a source-level revert of imports/facades because no schema, configuration, or persisted data changes are introduced.

## Open Questions

None.

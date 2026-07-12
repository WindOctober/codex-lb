## Context

HTTP bridge and downstream WebSocket orchestration already live in typed internal modules, but both call three preparation/admission methods still implemented on `ProxyService`. Those methods construct the shared `_WebSocketRequestState`, normalize and serialize the upstream `response.create` payload, apply canonical slimming and proxy-specific size enforcement, and coordinate the response-create semaphore with work admission.

The existing `_service/response_create.py` module owns diagnostic size enforcement and dumps. It deliberately accepts explicit thresholds because tests and local integrations replace service-module constants. The new orchestration boundary must preserve that seam without mixing state-machine coordination into the diagnostics module or importing `service.py` back into an internal module.

## Goals / Non-Goals

**Goals:**

- Give response-create request preparation and admission one typed runtime owner shared by HTTP bridge and WebSocket transports.
- Preserve payload contents, field removal/addition, JSON encoding, request-state defaults, IDs, metadata, fingerprints, service-tier values, slimming, size enforcement, timestamps, and cleanup ordering.
- Keep `ProxyService` method lookup and service-module threshold/dump monkeypatch behavior compatible.
- Ratchet ownership and dependency direction so the methods cannot return to the facade.

**Non-Goals:**

- Change payload slimming, maximum sizes, diagnostic dump policy, or response-create schemas.
- Change transport selection, account routing, continuity, API-key reservation policy, or upstream WebSocket behavior.
- Merge HTTP bridge or WebSocket orchestration into the preparation runtime.
- Restart or drain the primary backend during preflight.

## Decisions

1. Add `_ResponseCreateRuntimeMixin` and a structural `_ResponseCreateRuntimeService` protocol in a new `_service/response_create_runtime.py`. A separate module keeps state preparation/admission distinct from filesystem diagnostics in `_service/response_create.py`.
2. Move `_prepare_http_bridge_request`, `_prepare_response_bridge_request_state`, and `_acquire_request_state_response_create_admission` as one unit. They share the request-state contract and form the lifecycle from payload preparation through admission acquisition.
3. Import canonical pure metadata, fingerprint, service-tier, slimming, and gate-release helpers directly. No duplicate helper implementation is introduced.
4. Route the replaceable maximum size and size-enforcement call through explicit service compatibility hooks. Those hooks resolve service-module globals at call time so existing monkeypatches remain effective without a reverse import.
5. Leave `WorkAdmissionController` construction and mutable ownership on `ProxyService`; the runtime only calls the typed `_get_work_admission` capability.
6. Add architecture checks for required file ownership, mixin inheritance, local-method exclusion, and internal-module dependency direction.

## Risks / Trade-offs

- [Risk] Moving the admission method changes gate-release ordering on overload or cancellation. -> Preserve the `BaseException` cleanup boundary exactly and run focused gate/admission timing tests.
- [Risk] Direct imports bypass dynamic service constant replacement. -> Use explicit hooks for the maximum bytes and enforcement wrapper, then run oversized bridge/WebSocket monkeypatch tests.
- [Risk] Request-state construction changes defaults or serialized bytes. -> Move the body mechanically and cover metadata, fingerprints, request IDs, service tier, slimmed payloads, and repeated construction.
- [Trade-off] Two thin hooks remain on the facade. -> They are intentional compatibility seams; orchestration and mutable request state still leave the facade.

## Migration Plan

1. Add the typed runtime module and move the three methods without changing their signatures.
2. Add service compatibility hooks, inherit the mixin, remove local implementations, and ratchet architecture ownership.
3. Run focused preparation, payload-size, gate/admission, HTTP bridge, and WebSocket tests plus static checks.
4. Validate OpenSpec and an isolated backend while leaving Caddy and the primary backend untouched.

Rollback is a source-level move back into `service.py`; no schema, configuration, or persisted state migration is involved.

## Open Questions

None.

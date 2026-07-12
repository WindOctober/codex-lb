## Context

HTTP bridge session creation/reconnection and downstream WebSocket connection policy both call `ProxyService._open_upstream_websocket_with_budget`. The facade still implements that method and its unbudgeted companion, combining timeout translation, provider transport gating, token decryption, account-derived upstream parameters, local connection admission, and dispatch through a replaceable socket factory.

The existing WebSocket connection runtime deliberately leaves socket creation as a shared service capability because HTTP bridge also consumes it. The core socket client now owns per-connection egress selection and real handshake/error mapping. Several tests replace `app.modules.proxy.service.connect_responses_websocket` with older-signature fakes, so that service-module binding remains a compatibility contract for this internal refactor.

## Goals / Non-Goals

**Goals:**

- Give shared upstream WebSocket creation one typed runtime owner outside the proxy facade.
- Preserve budget translation, provider rejection, credential and account parameter derivation, connection admission ordering, and unconditional lease release.
- Preserve inherited `ProxyService` method lookup and service-module socket-factory replacement.
- Ratchet ownership and dependency direction, and directly cover currently implicit factory-forwarding and cleanup behavior.

**Non-Goals:**

- Change account selection, routing, token refresh, 401 handling, failover, or account-health mutation.
- Change egress selection, retry across egress routes, socket headers, handshake behavior, or upstream error mapping.
- Change HTTP bridge or downstream WebSocket orchestration.
- Restart, drain, retarget, or otherwise modify the primary backend or Caddy during preflight.

## Decisions

1. Add `_UpstreamWebSocketRuntimeMixin` and a structural `_UpstreamWebSocketRuntimeService` protocol in `_service/upstream_websocket.py`. A top-level shared module is used instead of `websocket/connection.py` because HTTP bridge session creation and reconnect also consume the capability.
2. Move `_open_upstream_websocket_with_budget` and `_open_upstream_websocket` together. Splitting the timeout wrapper from its creation lifecycle would leave a misleading partial owner and weaken direct testing of the boundary.
3. Expose only `_encryptor`, `_get_work_admission`, and `_connect_responses_websocket_compatible` through the protocol. Account/provider transforms remain canonical pure helper imports; the runtime does not gain repositories, routing, or balancer access.
4. Implement `_connect_responses_websocket_compatible` as a local static method on `ProxyService`. It resolves the service-module `connect_responses_websocket` binding at call time and uses the existing optional-argument adapter so legacy fakes without `wire_api` remain valid. This replaces the current top-level compatibility wrapper rather than adding another delegation layer.
5. Keep the core socket factory responsible for egress selection and the actual connection attempt. The runtime calls it exactly once per admitted creation attempt and does not add replay or cross-egress failover.
6. Add architecture checks for required file ownership, inherited methods, local facade compatibility seams, and one-way imports. Add direct runtime tests for parameter forwarding, provider gating, timeout mapping, and lease cleanup on success, failure, and cancellation.

## Risks / Trade-offs

- [Risk] A moved `finally` boundary can leak or double-release connection admission. -> Move the acquire/call/finally sequence mechanically and test all terminal paths with an observable lease.
- [Risk] Directly importing the core factory would bypass service-module monkeypatches. -> Require a typed facade compatibility method that resolves the replaceable binding at call time.
- [Risk] Provider checks or account transforms can move after admission or decryption. -> Preserve the existing order and assert unsupported providers never acquire admission or invoke the factory.
- [Risk] The boundary could accidentally absorb active routing or egress work. -> Restrict touched runtime code to the two creation methods and leave all selection, failover, health, and core client files unchanged.
- [Trade-off] One thin facade compatibility method remains. -> It is the explicit injection seam; mutable creation lifecycle and cleanup still leave the facade.

## Migration Plan

1. Add the typed runtime and direct behavior tests.
2. Add the facade compatibility method, inherit the mixin, remove the two local methods and obsolete top-level wrapper, and ratchet architecture ownership.
3. Run focused direct, downstream WebSocket, HTTP bridge, core egress, static, and OpenSpec checks.
4. Validate an isolated backend on dedicated ports and clean it by explicit PID/port, leaving Caddy and the primary backend untouched.

Rollback is a source-level move of the two methods back into `service.py`; no API, schema, configuration, or persisted-state migration is involved.

## Open Questions

None.

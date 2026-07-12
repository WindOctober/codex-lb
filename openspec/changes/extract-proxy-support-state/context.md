## Scope

The extracted support state includes affinity policy, stream retry exceptions and settlement, websocket request state, HTTP bridge key/session state, websocket control/activity, prepared request state, receive timeout state, and the pure event-type helper.

These objects remain intentionally shared because HTTP bridge, direct streaming, and websocket transports coordinate through the same request state. Splitting them by transport now would duplicate mutable state and create synchronization ambiguity.

## Compatibility

`app.modules.proxy.service` remains the compatibility facade. Existing tests and operational code can continue constructing and patching the same names through that module. Canonical ownership moves to `_service/support.py` so new implementation slices do not import the service monolith.

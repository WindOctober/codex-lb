## Boundary

The upstream-events slice owns receiving one persistent upstream WebSocket, applying timeout and replay decisions, matching upstream events to pending bridge requests, forwarding normalized SSE, and invoking service settlement capabilities. Session registry mutation, durable persistence, account health settlement, request logging, and account selection remain service capabilities.

## Compatibility

`ProxyService` inherits the extracted methods. Compatibility wrappers remain only where tests and integrations patch service-module Prometheus handles; metric implementation and log formatting remain canonical in observability.

The extracted modules never import `app.modules.proxy.service` and do not inspect it dynamically.

## Boundary

Session management owns the in-memory session registry, durable owner/lease synchronization, account-model session leases, pressure reclamation, session creation, and reuse/acquisition decisions. Request submission and upstream event processing remain separate bridge capabilities. Account selection, token refresh, upstream socket construction, and persistence are invoked through typed service capabilities.

## Decomposition

Pure policy functions do not mutate service state. Lifecycle methods own registry indexes and durable aliases. Capacity methods own admission and reclamation. Creation methods construct one upstream-backed session. Acquisition methods coordinate reuse, aliases, in-flight creation, and replacement.

## Compatibility

`ProxyService` inherits extracted methods so existing internal callers keep their names. The extracted modules do not import `app.modules.proxy.service` and do not inspect it dynamically. Service-level wrappers remain only for established replacement points such as model-registry injection and Prometheus handles.

## Failure Modes

The refactor must not weaken fail-closed previous-response ownership, allow duplicate hard-affinity sessions, leak account-model concurrency leases, evict busy sessions, or alter retryable overload and no-account errors.

## Example

When a durable session is recovered after a process restart, acquisition resolves its owner and account binding, creation opens exactly one upstream socket, lifecycle registers all aliases, and capacity retains exactly one account-model lease. A competing hard-affinity request either reuses that session or receives the existing busy/ownership outcome.

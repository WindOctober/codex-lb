# Proxy Compact Runtime Context

## Purpose and Scope

Compact requests coordinate affinity, account selection, refresh, work admission, retry, failover, API-key settlement, service-tier accounting, and terminal logging. The dedicated runtime keeps that state machine outside the `ProxyService` facade while preserving the public service method and wire behavior.

This capability covers internal ownership and behavior preservation. It does not define endpoint schemas, tune retry or budget policy, change account eligibility, or introduce a provider-side compact surrogate.

## Decisions and Constraints

- The orchestration remains one cohesive mixin because its nested account and same-account retry loops share mutable attempt state.
- A structural protocol makes required service capabilities explicit while leaving encryptor, load balancer, admission, repositories, and accounting state on `ProxyService`.
- Settings and the core compact transport continue through thin service adapters so existing runtime and test injection points remain stable.
- Pure affinity, budget, observability, service-tier, support, and upstream-account policy stays in canonical helper modules.
- The runtime must not import the service facade, and extraction must not change public routes, payloads, persistence, deployment, or primary runtime configuration.

## Failure Modes

- A first 401 refreshes and retries the same account once; later authentication failure follows the existing terminal error path.
- Repeated HTTP 500 responses exhaust the same-account retry count before deterministic failover can select another eligible account.
- Providers without the Codex compact wire API fail closed with the existing not-implemented response.
- Local admission overload is surfaced without penalizing the selected upstream account.
- Timeout override ContextVars are attempt-local and must be restored even when provider validation or admission stops work before the upstream call.

## Concrete Example

A compact request pinned by Codex session affinity selects account A. If account A returns 401, the runtime force-refreshes A within the remaining request budget and retries A rather than reallocating. On success it records account success, settles the API-key reservation using the effective service tier, writes the terminal request log, and returns the compact response through the unchanged `ProxyService.compact_responses` surface.

## Operational Notes

Validate compact changes with focused success, budget, provider, refresh, transient failover, sticky-session, API-key settlement, and logging tests. Runtime preflight uses an isolated backend and mock upstream; it must not restart or drain the primary backend or alter the Caddy gateway.

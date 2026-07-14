## Context

The alpha search runtime currently sets one deadline from `proxy_request_budget_seconds` and passes all remaining time to the first upstream HTTP request. Production uses 480 seconds. Since the new search route was deployed, seven failures ended at 480000-480506 ms with `Timeout on reading data from socket`; successful searches normally finish in 1-3 seconds but observed long-tail successes reach 341 seconds.

The latest official Codex `SearchClient` does not attach a request-specific timeout to `alpha/search`. codex-lb still needs a finite bound, but the current generic bound is both shorter than the official behavior and structurally prevents the existing two-account failover after a stalled first attempt.

## Goals / Non-Goals

**Goals:**

- Keep alpha search independent from short general proxy request budgets.
- Allow a legitimate long-tail search to exceed the general budget.
- Preserve enough time for one different-account retry after a stalled upstream attempt.
- Surface the latest upstream error when both bounded attempts fail.
- Keep the total search lifetime finite and configurable.

**Non-Goals:**

- Making alpha search unbounded.
- Retrying account-neutral client errors.
- Adding more than the existing two account attempts.
- Changing search affinity, model policy, payloads, usage accounting, or Responses stream budgets.

## Decisions

### Use dedicated total and per-attempt settings

Add `codex_search_request_budget_seconds` with a 1200-second default and `codex_search_account_attempt_timeout_seconds` with a 600-second default. The request deadline uses the total setting. Each upstream call receives the smaller of the remaining total time and the per-attempt setting.

Alternative: raise `proxy_request_budget_seconds`. Rejected because that would lengthen unrelated selection, refresh, and non-search failure paths.

Alternative: give the first search attempt the full dedicated budget. Rejected because a read timeout would still consume all time and make the second account attempt unreachable.

### Preserve one total deadline

Credential refresh, account selection, forced refresh, both upstream attempts, and backoff all share one absolute deadline. Per-attempt limits do not reset the total deadline.

Each selected account also receives one fixed per-account deadline. A 401-triggered forced refresh and same-account retry share that deadline; refreshing credentials MUST NOT reset the account's attempt window.

### Keep existing failure classification

Transport and account-scoped failures continue through the existing load-balancer error path and may use the second account. Account-neutral invalid requests still return immediately. If the final upstream attempt fails, its structured error is returned; a generic budget error is reserved for work that reaches the total deadline before another bounded operation can begin.

## Risks / Trade-offs

- [Risk] A genuinely stuck search can occupy a worker longer than before. -> Each account attempt is capped at 600 seconds and the entire request at 1200 seconds.
- [Risk] Retrying duplicates a search operation. -> Search is read-only, attempts remain capped at two, and successful responses stop further work.
- [Risk] Provider latency may grow beyond the defaults. -> Both limits are typed environment settings and can be tuned without code changes.

## Migration Plan

Add defaults and example/local configuration, run focused and related regression suites, then validate the combined build on backend-only port 3456. Restart only port 2456 and monitor search request logs for exact 480-second failures. Rollback restores the prior search deadline calculation and settings, then restarts only 2456.

## Open Questions

None.

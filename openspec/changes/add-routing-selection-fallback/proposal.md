# Add Routing Selection Fallback

## Why

During high-concurrency Codex HTTP bridge traffic, local per-account/model bridge-session limits can temporarily exclude every otherwise routable account. Some paths currently surface this as `No active accounts available`, which is misleading when active accounts with quota exist but are locally saturated.

`preferEarlierResetAccounts` can also narrow routing to the earliest reset bucket. That preference should improve quota burn-down, but it should not make selection fail if a wider routable pool still exists.

## What Changes

- Treat local account/model concurrency exhaustion as local proxy overload instead of upstream account absence.
- Preserve the existing account routing strategies, but ensure earlier-reset narrowing remains a preference with fallback to the wider eligible pool when the preferred bucket cannot produce a selected account.
- Add regression coverage for local-budget exclusion and earlier-reset fallback behavior.

## Impact

- Affected code: `app/core/balancer/logic.py`, `app/modules/proxy/service.py`.
- Affected behavior: requests that hit local per-account/model saturation should surface explicit overload/wait behavior instead of `No active accounts available`.
- No database or API schema migration is required.

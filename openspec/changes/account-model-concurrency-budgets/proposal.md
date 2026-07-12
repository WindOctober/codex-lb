# Account Model Concurrency Budgets

## Why

Large bursts through the HTTP responses bridge can select a small set of active
accounts faster than upstream rejects or local usage state can react. When many
Codex turns land on the same account/model at once, the proxy can amplify
upstream 403s instead of spreading load across the available account pool or
failing locally with a retryable overload response.

## What Changes

- Add a local per-account, per-model in-flight request budget with a default
  limit of 48.
- Exclude accounts that are already at the local budget from normal account
  selection.
- Count HTTP bridge queued/pending requests against the same budget and release
  the slot when the request detaches or fails.
- Return a local retryable overload error when all eligible accounts are at the
  local account/model budget.

## Non-Goals

- Distributed cross-replica concurrency accounting.
- Automatic upstream 403 cooldowns or account deactivation changes.
- Changes to the 2455 Caddy gateway.

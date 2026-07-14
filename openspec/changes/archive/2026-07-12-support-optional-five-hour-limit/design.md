## Context

codex-lb stores generic usage windows as independent `primary` and `secondary` history rows. Account selection reads the latest row of each kind and derives `RATE_LIMITED` from a full primary window and `QUOTA_EXCEEDED` from a full secondary window. A successful upstream response that contains only a 604800-second primary window is normalized into the local secondary/weekly slot, but the refresh path currently leaves every older primary row in place. The selector therefore combines the new weekly row with a stale five-hour row from a different snapshot.

The public Codex documentation still describes a five-hour window, while authenticated usage payloads observed for the affected accounts currently return only a weekly generic window. The implementation must therefore adapt to the observed payload and remain reversible rather than assuming a permanent product-wide removal.

## Goals / Non-Goals

**Goals:**

- Prevent stale five-hour snapshots from making all otherwise usable accounts unroutable.
- Let an operator persistently disable five-hour usage-snapshot enforcement from the Accounts page.
- Keep weekly quota, explicit upstream 429 cooldowns, account eligibility, model compatibility, and local admission limits intact.
- Recover automatically if upstream later reports a distinct five-hour window again.

**Non-Goals:**

- Suppress, retry through, or reinterpret an explicit upstream 429 response.
- Disable weekly or model-specific additional quotas.
- Change routing strategy ordering except for removing ignored five-hour inputs.
- Claim that OpenAI has permanently removed five-hour limits for every plan.

## Decisions

1. Persist one `ignore_five_hour_limit` dashboard setting, defaulting to disabled for migration compatibility. The Accounts page exposes the same setting as `Ignore 5h`. After deployment it can be enabled without restarting the service, and disabling it restores normal primary-window enforcement.
2. Pass the setting explicitly from the proxy request settings snapshot into load-balancer state construction. When enabled, primary usage values are omitted from quota derivation and routing pressure. A persisted `RATE_LIMITED` state is cleared only when it has no `blocked_at` marker; states with a marker came from an explicit upstream failure and remain enforced.
3. Keep secondary/weekly values and additional model quotas unchanged. This preserves the hard weekly boundary and model-specific capacity behavior regardless of the toggle.
4. On a successful weekly-only generic usage payload, remove obsolete generic primary history for that account before recording the normalized weekly row. This prevents old primary rows from becoming current again in dashboard or selector reads. If a later payload includes a primary window, new primary history is written normally.
5. Keep the setting in the existing dashboard settings API and cache. The update endpoint invalidates the cache, so new requests observe the change without process restart.

## Risks / Trade-offs

- [The public product documentation and authenticated payload shape disagree] -> Default the override off, label it as an operator override, and derive automatic cleanup only from a successful account-specific payload.
- [Deleting obsolete primary history removes historical five-hour chart samples for an account] -> Limit deletion to the generic primary window and retain weekly, model-specific, request-log, and quota-timeline data; future primary samples resume automatically if upstream restores the window.
- [Ignoring primary usage could accidentally bypass a real 429] -> Preserve `RATE_LIMITED` entries with a `blocked_at` marker and leave the upstream error/cooldown path unchanged.
- [Cached settings could delay activation] -> Reuse the existing settings-cache invalidation on update and test propagation into every account-selection attempt.

## Migration Plan

1. Add the non-null dashboard setting with a false server default.
2. Deploy and validate the new behavior on an isolated backend using the existing database-compatible migration.
3. Restart only the primary backend, enable the setting through the settings API, and verify account selection plus both health endpoints.
4. Roll back operationally by disabling the setting. A code rollback remains compatible with the additive database column.

## Open Questions

None.

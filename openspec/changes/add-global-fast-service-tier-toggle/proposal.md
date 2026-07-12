# Add Global Fast Service Tier Toggle

## Summary

- Add a dashboard control to enable or disable account fast mode for all accounts in one action.
- Add a protected accounts API endpoint that bulk-updates `fast_service_tier_enabled` for every account.
- Keep the existing per-account fast mode behavior as the source of truth for request routing.

## Impact

- Affects dashboard account operations and account list state.
- Does not change API-key-enforced service tier precedence.
- Does not block clients that explicitly send a supported `service_tier`.

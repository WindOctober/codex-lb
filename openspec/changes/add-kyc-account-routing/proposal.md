# Change: Add KYC account routing controls

## Why

Some upstream accounts are backed by KYC-approved credentials. `codex-lb` needs a way to mark those accounts and issue API keys that can access them without allowing those keys to route non-KYC account traffic.

## What Changes

- Add a KYC flag to accounts.
- Add a runtime dashboard toggle for KYC routing enforcement.
- Add a `kyc_only` flag to API keys.
- Enforce that KYC accounts require a KYC-only API key when enforcement is enabled.
- Enforce that KYC-only API keys can only route KYC accounts when enforcement is enabled.
- Expose KYC account and KYC-only key controls in the dashboard.

## Impact

- Requires database migrations for `accounts.kyc_enabled`, `dashboard_settings.kyc_routing_enforcement_enabled`, and `api_keys.kyc_only`.
- When enforcement is enabled, an account marked as KYC is no longer routable without a KYC-only key, even when proxy API-key auth is disabled for local clients.

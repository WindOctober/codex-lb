# Add Account Fast Service Tier

## Summary

- Add a per-account dashboard toggle for fast mode.
- Persist the toggle on accounts.
- When a request is routed to an account with fast mode enabled, forward the upstream request with the priority service tier unless an API key already enforces a service tier.

## Impact

- Affects account CRUD responses and account routing settings updates.
- Affects upstream Responses/compact forwarding for selected accounts.

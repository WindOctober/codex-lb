# Add Account Subscription Renewal Date

## Why

Operators need a manually maintained subscription renewal date for OpenAI OAuth accounts because Codex CLI and app-server payloads do not expose a reliable subscription anchor. Showing this field beside account details lets operators record renewal timing after checking each account.

## What Changes

- Add an optional `subscription_renews_at` field to accounts.
- Include the field in account summary responses and account update requests.
- Add an Accounts detail control to view, save, and clear the manually maintained date.

## Impact

- Adds one nullable timestamp column to `accounts`.
- Extends account summary/update contracts.
- Updates the Accounts dashboard detail panel and test mocks.

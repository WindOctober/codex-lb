## Why

The HTTP Responses bridge retries selected-model capacity and `server_is_overloaded` failures on a fresh WebSocket, but its no-text retry path prefers the same account. A request can therefore reconnect to the account that just failed and terminate after exhausting its replay allowance even when another account supports the same model.

## What Changes

- Keep the originally requested model unchanged across capacity retries.
- Exclude the failing account from the immediate HTTP bridge retry for account limits and model-capacity signals.
- Preserve same-account retries for generic transient transport/server failures where reconnecting the account is still appropriate.
- Preserve the existing rule that replay is only allowed before meaningful downstream output.

## Impact

- Affected code: `app/modules/proxy/service.py`.
- Affected API: `/backend-api/codex/responses` and HTTP-bridged `/v1/responses` requests.
- No schema, model-registry, or public request-contract changes.

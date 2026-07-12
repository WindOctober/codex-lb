## Why

Upstream can return `Selected model is at capacity. Please try a different model.` for one ChatGPT account while other configured accounts still have capacity. The proxy currently treats that message like a terminal client-visible error in some Responses paths, ending the turn instead of trying another eligible account.

## What Changes

- Classify the selected-model capacity message as an account-level rate-limit failure.
- Make Responses requests that receive this error before `response.created` retry on a fresh upstream account before any downstream event is emitted, including HTTP bridge, direct stream, and websocket replay paths.
- Preserve existing behavior once a response has become downstream-visible.

## Impact

- Affected code: `app/modules/proxy/helpers.py`, `app/modules/proxy/service.py`.
- Affected APIs: `/v1/responses` and `/backend-api/codex/responses`.
- Operational impact: the failing account is marked rate-limited and subsequent selection can use another available account.

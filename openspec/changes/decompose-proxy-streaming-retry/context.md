## Boundary

The ordinary-streaming slice owns selecting and refreshing an account for a streamed Responses request, applying same-account transient retry and cross-account failover policy, opening one upstream SSE attempt, interpreting its first and terminal events, emitting downstream SSE, and recording settlement. Repository access, account health mutation, API-key accounting, durable owner lookup, request-log persistence, and work admission remain typed service capabilities.

## Compatibility

`ProxyService` inherits the extracted methods. Runtime settings and dashboard settings are supplied through explicit service adapters so focused tests can still replace service-module settings providers. The upstream stream factory is also supplied through one adapter so existing tests that replace `service.core_stream_responses` retain their behavior.

## Failure Modes

- A model/account rate limit MUST rotate to another eligible account rather than switch models.
- A transient server error MUST retain the existing bounded same-account retry count before account failover.
- A previous-response owner MUST remain a required preferred account until the existing fail-closed policy permits otherwise.
- Cancellation MUST release admission and timeout overrides and settle API-key reservations exactly once.

## Example

For an SSE request whose first upstream event is a transient `server_error`, the extracted flow retries the same account up to the configured in-code limit. If those attempts are exhausted, it records the account failure, excludes that account, selects another account for the same requested model, and continues streaming without substituting a compatibility model.

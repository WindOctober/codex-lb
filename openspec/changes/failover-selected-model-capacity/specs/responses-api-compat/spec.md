## ADDED Requirements

### Requirement: Selected-model capacity errors fail over before downstream visibility

When an upstream Responses request fails before `response.created` with `Selected model is at capacity. Please try a different model.` or `server_is_overloaded`, the proxy MUST treat the failure as an account-level capacity signal and attempt another eligible account when one remains. The proxy MUST NOT emit that upstream error to the downstream client before exhausting eligible account attempts. Once any event for the response is downstream-visible, the proxy MUST continue surfacing later terminal errors instead of replaying the request.

#### Scenario: HTTP bridge switches accounts before surfacing selected-model capacity

- **GIVEN** at least two eligible accounts can serve a Responses request
- **WHEN** the first HTTP bridge upstream returns `Selected model is at capacity. Please try a different model.` before `response.created`
- **THEN** the proxy marks that account rate-limited
- **AND** retries the request through another eligible account
- **AND** the downstream client receives the successful response from the later account instead of the first upstream capacity error

#### Scenario: HTTP bridge switches accounts before surfacing server overload

- **GIVEN** at least two eligible accounts can serve a Responses request
- **WHEN** the first HTTP bridge upstream returns `server_is_overloaded` before `response.created`
- **THEN** the proxy records the first account as having a retryable upstream failure
- **AND** retries the request through another eligible account
- **AND** the downstream client receives the successful response from the later account instead of the first upstream overload error

#### Scenario: Direct stream switches accounts before surfacing selected-model capacity

- **GIVEN** at least two eligible accounts can serve a Responses request
- **WHEN** the first direct stream upstream emits `response.failed` with `Selected model is at capacity. Please try a different model.` as the first event
- **THEN** the proxy marks that account rate-limited
- **AND** retries the request through another eligible account
- **AND** the downstream client receives the successful response from the later account instead of the first upstream capacity error

#### Scenario: Websocket replay switches accounts before surfacing selected-model capacity

- **GIVEN** at least two eligible accounts can serve a websocket-backed Responses request
- **WHEN** the upstream websocket emits `response.failed` with `Selected model is at capacity. Please try a different model.` before `response.created`
- **THEN** the proxy suppresses the upstream failure from downstream visibility
- **AND** replays the pending request through a fresh upstream selection

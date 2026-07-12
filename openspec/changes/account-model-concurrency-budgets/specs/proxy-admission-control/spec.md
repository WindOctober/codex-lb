## ADDED Requirements

### Requirement: Account/model concurrency budgets

The proxy SHALL enforce a local in-flight request budget per account and model.
The default budget SHALL be 48 requests for each account/model pair.

#### Scenario: Full account is skipped during selection

- **GIVEN** an account/model pair has reached the local in-flight request budget
- **WHEN** a new request for that model needs account selection
- **THEN** the proxy excludes that account from the selection attempt.

#### Scenario: All eligible accounts are full

- **GIVEN** every eligible account for a model has reached the local in-flight request budget
- **WHEN** a new request for that model needs account selection
- **THEN** the proxy fails locally with a retryable overload error instead of sending the request upstream.

#### Scenario: HTTP bridge requests hold budget while pending

- **GIVEN** an HTTP bridge request is queued or pending on an upstream websocket
- **WHEN** the request has not detached, completed, or failed
- **THEN** it counts against the selected account/model budget.

#### Scenario: HTTP bridge budget is released

- **GIVEN** an HTTP bridge request holds an account/model budget slot
- **WHEN** the request detaches, submission fails, or pending requests are force-failed
- **THEN** the proxy releases the budget slot exactly once.

#### Scenario: HTTP bridge reuse skips a locally full account

- **GIVEN** a reusable HTTP bridge session belongs to an account/model pair that has reached the local in-flight request budget
- **WHEN** a new request for that model reaches the bridge session cache
- **THEN** the proxy does not submit the request to that cached session
- **AND** it creates or selects another eligible bridge session using normal account selection.

#### Scenario: HTTP bridge session capacity queues locally

- **GIVEN** every eligible account/model pair has reached the local HTTP bridge session budget
- **WHEN** a new HTTP bridge session is needed
- **THEN** the proxy waits locally for a session budget slot within the request budget instead of opening another upstream websocket or immediately returning an overload response.

#### Scenario: HTTP bridge session budget is released

- **GIVEN** an HTTP bridge session holds an account/model session budget slot
- **WHEN** the bridge session closes
- **THEN** the proxy releases the session budget slot exactly once.

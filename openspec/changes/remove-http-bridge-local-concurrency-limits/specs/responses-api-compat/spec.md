## ADDED Requirements

### Requirement: HTTP bridge does not locally cap per-session concurrency by default

HTTP `/v1/responses` and `/backend-api/codex/responses` bridge sessions MUST NOT reject or serialize compatible requests solely because multiple requests share the same prompt-cache, session-header, or turn-state bridge key. A configured positive bridge queue limit MAY still enforce a local queue cap for operators that opt into bounded per-session admission.

#### Scenario: default bridge accepts more than the historical queue cap

- **WHEN** more than eight compatible HTTP Responses requests target the same active bridge session
- **AND** the bridge queue limit is unset or configured as zero
- **THEN** the proxy accepts the requests into the bridge session instead of returning `rate_limit_exceeded` with `HTTP responses session bridge queue is full`

#### Scenario: explicit positive queue cap remains available

- **WHEN** an operator configures a positive HTTP bridge queue limit
- **AND** the number of queued requests for one bridge session reaches that limit
- **THEN** the proxy rejects additional requests locally with `rate_limit_exceeded`

### Requirement: Fresh soft prompt-cache bridge requests are sharded under pending load

HTTP bridge routing MUST spread fresh soft prompt-cache requests across multiple bridge session keys when the current shard already has pending work. The proxy MUST NOT shard requests that carry hard continuity identifiers such as turn-state/session affinity or `previous_response_id`.

#### Scenario: busy prompt-cache shard creates another bridge session

- **WHEN** a fresh HTTP Responses request has only soft prompt-cache affinity
- **AND** the current prompt-cache bridge shard already has at least the configured pending threshold
- **THEN** the proxy routes the request to another prompt-cache bridge shard instead of reusing the busy shard

#### Scenario: hard continuity requests are not sharded

- **WHEN** a request includes a turn-state/session affinity or `previous_response_id`
- **THEN** the proxy preserves the existing hard-continuity bridge route instead of deriving a soft prompt-cache shard

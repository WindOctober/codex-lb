## MODIFIED Requirements

### Requirement: Proven redundant proxy facade symbols remain absent

The proxy facade MUST NOT define or re-export the ratcheted redundant names `_resolve_prompt_cache_key`, `_account_supports_http_bridge_request_model`, `_match_websocket_request_state_for_previous_response_error`, `_TEXT_DELTA_EVENT_TYPES`, `_call_core_compact_responses`, `_request_budget_seconds`, or `_usage_window_row_from_entry`.

#### Scenario: Architecture verification
- **WHEN** proxy architecture checks inspect the service module
- **THEN** every ratcheted redundant name MUST be absent

#### Scenario: Future facade cleanup
- **WHEN** another private facade symbol is proposed for removal
- **THEN** repository callers, monkeypatch/import strings, architecture ownership, and normative OpenSpec contracts MUST be audited before it is added to the absent-name ratchet

### Requirement: Canonical proxy behavior owners are preserved

Removing redundant facade symbols MUST preserve the canonical affinity, request-budget, rate-limit usage-row, HTTP bridge model-support, WebSocket previous-response matching, streaming text-event, and HTTP bridge session-acquisition implementations.

#### Scenario: Canonical helper consumers
- **WHEN** affinity, budget, rate-limit, HTTP bridge, or WebSocket runtimes invoke their canonical helpers
- **THEN** their results and side effects MUST remain unchanged without routing through the removed facade names

#### Scenario: Session acquisition observability
- **WHEN** HTTP bridge session acquisition records structured timing or continuity observations
- **THEN** removing its unused module logger MUST NOT change those existing observability calls

#### Scenario: Budget and rate-limit owner retention
- **WHEN** the service facade aliases are removed
- **THEN** request deadline calculation MUST continue using the canonical budget helper
- **AND** rate-limit header and payload projection MUST continue using the canonical usage-row helper

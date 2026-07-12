# proxy-facade-maintenance Specification

## Purpose

Define the evidence and behavior-preservation constraints for pruning proven-redundant private proxy facade symbols and passthrough layers.

## Requirements

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

### Requirement: Compact transport compatibility uses one facade layer

The compact runtime MUST retain service-module core transport replacement and optional-keyword compatibility without a separate single-caller module-level passthrough.

#### Scenario: Current compact factory
- **WHEN** compact orchestration invokes the current core transport
- **THEN** payload, headers, token, account ID, base URL, and wire API MUST be forwarded with the existing behavior

#### Scenario: Legacy compact factory
- **WHEN** a service-module compact transport replacement omits newer optional keywords
- **THEN** unsupported optional keywords MUST be filtered while required arguments remain forwarded

#### Scenario: Unsupported provider contract
- **WHEN** the selected provider does not support the compact wire contract
- **THEN** the existing provider fail-closed behavior MUST remain unchanged

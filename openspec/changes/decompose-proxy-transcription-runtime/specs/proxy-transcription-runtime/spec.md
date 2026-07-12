## ADDED Requirements

### Requirement: Proxy transcription has a typed orchestration boundary

Audio transcription orchestration MUST be implemented outside the proxy transport facade behind explicit typed service capabilities while remaining callable as `ProxyService.transcribe()`.

#### Scenario: Successful transcription

- **WHEN** an eligible account and fresh credentials are available
- **THEN** the runtime MUST invoke the upstream transcription client with filtered headers, decrypted credentials, account-specific base URL, and the remaining request budget
- **AND** it MUST record account success and a successful HTTP request log

#### Scenario: Selection failure

- **WHEN** no eligible account is selected
- **THEN** the runtime MUST preserve the selection error code and message in the proxy error and terminal request log

#### Scenario: Credential refresh failure

- **WHEN** credential refresh fails permanently
- **THEN** the runtime MUST preserve permanent account failure handling and the existing invalid API key response

### Requirement: Transcription retry and timeout behavior is preserved

The extracted runtime MUST preserve the total request deadline, timeout override scope, and at most one forced-refresh retry after an upstream 401.

#### Scenario: Upstream 401 with budget remaining

- **WHEN** the first upstream transcription attempt returns 401 and request budget remains
- **THEN** the runtime MUST force-refresh the same account and make exactly one additional upstream attempt

#### Scenario: Budget exhausted before retry

- **WHEN** the request budget is exhausted before freshness, upstream invocation, or forced refresh
- **THEN** the runtime MUST return the existing upstream-unavailable budget error and MUST NOT start another upstream attempt

#### Scenario: Generic upstream failure

- **WHEN** the upstream client returns a non-401 proxy error
- **THEN** the runtime MUST preserve account health handling, proxy error propagation, and terminal error logging

#### Scenario: Existing patch point

- **WHEN** a test or diagnostic replaces the settings, remaining-budget, or upstream transcription function through `app.modules.proxy.service`
- **THEN** `ProxyService.transcribe()` MUST continue to use the replacement through compatibility capabilities

# proxy-account-freshness-runtime Specification

## Purpose

Define the typed shared boundary and behavior-preservation contract for provider-aware account credential freshness across proxy transports.

## Requirements

### Requirement: Account freshness has a shared typed runtime boundary

Provider-aware account credential freshness shared by proxy transports MUST be implemented outside the proxy facade behind an explicit typed service capability boundary, while existing `ProxyService` method names and signatures remain callable.

#### Scenario: Direct HTTP consumers
- **WHEN** ordinary streaming, compact, or transcription checks or refreshes selected account credentials
- **THEN** it MUST use the inherited shared freshness method with the existing call contract

#### Scenario: WebSocket-backed consumers
- **WHEN** downstream WebSocket connection, HTTP bridge session creation, or HTTP bridge reconnect checks or refreshes selected account credentials
- **THEN** it MUST use the inherited shared freshness method with the existing call contract

#### Scenario: Dependency direction
- **WHEN** the runtime consumes repositories, refresh admission, or canonical refresh behavior
- **THEN** it MUST use explicit service capabilities or the canonical `AuthManager` without importing the proxy facade

### Requirement: Provider and AuthManager freshness semantics are preserved

The extracted runtime MUST preserve API-key provider bypass and OAuth account freshness, force-refresh, repository, AuthManager singleflight, refresh-admission, persistence, and error behavior.

#### Scenario: API-key provider account
- **WHEN** freshness is requested for an API-key provider account
- **THEN** the runtime MUST return the account unchanged without entering the repository bundle or acquiring OAuth refresh admission

#### Scenario: OAuth account freshness
- **WHEN** freshness is requested for an OAuth account
- **THEN** the runtime MUST invoke canonical AuthManager behavior with the account repository, existing force value, and shared refresh-admission capability

#### Scenario: Concurrent stale account refresh
- **WHEN** equivalent stale account snapshots request refresh concurrently
- **THEN** canonical AuthManager singleflight and admission behavior MUST remain unchanged

### Requirement: Refresh timeout and compatibility scopes are preserved

The extracted runtime MUST preserve request-local refresh timeout scoping, restoration on every terminal path, dynamic service-method replacement, optional-keyword compatibility, and required service facade exports.

#### Scenario: Nested timeout scope
- **WHEN** a caller already has a refresh timeout override and account freshness returns, raises, or is cancelled
- **THEN** the runtime MUST restore the caller's previous timeout override

#### Scenario: Legacy freshness replacement
- **WHEN** an instance or class replacement for `_ensure_fresh` omits the newer optional `timeout_seconds` keyword
- **THEN** `_ensure_fresh_with_budget` MUST omit only that unsupported keyword while forwarding the account and force value

#### Scenario: Service facade compatibility
- **WHEN** existing integrations access `AuthManager`, `ACCOUNT_PROVIDER_API_KEY`, or `RefreshError` through `app.modules.proxy.service`
- **THEN** those names MUST resolve to their canonical implementations

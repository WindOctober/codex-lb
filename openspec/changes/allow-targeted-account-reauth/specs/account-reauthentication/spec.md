## ADDED Requirements

### Requirement: Targeted OAuth re-authentication

The system SHALL allow an OAuth re-authentication flow to target an existing OpenAI OAuth account by account ID.

#### Scenario: Active account starts targeted re-auth

- **GIVEN** an existing OpenAI OAuth account with status `active`
- **WHEN** OAuth start is requested with that account as the target
- **THEN** the OAuth flow starts instead of short-circuiting as already complete

#### Scenario: Targeted re-auth replaces upstream identity

- **GIVEN** an existing OpenAI OAuth account `A`
- **AND** the account owns usage history, request logs, sticky sessions, API-key assignments, or local routing metadata
- **WHEN** a targeted OAuth flow for `A` completes with a different OpenAI email or ChatGPT account ID
- **THEN** account `A` is updated with the new OAuth email, ChatGPT account ID, tokens, plan type, and active status
- **AND** account-owned local data remains attached to `A`

#### Scenario: Targeted re-auth rejects provider accounts

- **GIVEN** an existing account whose provider kind is not OpenAI OAuth
- **WHEN** OAuth start is requested with that account as the target
- **THEN** the request is rejected

#### Scenario: Targeted re-auth rejects identities owned by another local account

- **GIVEN** an existing OpenAI OAuth account `A`
- **AND** another local OpenAI OAuth account `B` already has the incoming OAuth ChatGPT account ID
- **WHEN** a targeted OAuth flow for `A` completes with `B`'s ChatGPT account ID
- **THEN** the flow fails instead of moving or merging `B` automatically

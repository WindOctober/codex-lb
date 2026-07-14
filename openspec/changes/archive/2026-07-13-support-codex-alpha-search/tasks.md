## 1. Contract and upstream client

- [x] 1.1 Add forward-compatible typed alpha search request and response schemas.
- [x] 1.2 Add Codex alpha search URL construction and a bounded JSON upstream client.
- [x] 1.3 Add unit tests for URL, headers, response parsing, and normalized errors.

## 2. Routing runtime and API

- [x] 2.1 Add a focused search runtime that reuses account affinity, freshness, policy, and bounded failover.
- [x] 2.2 Register the authenticated `/backend-api/codex/alpha/search` POST route.
- [x] 2.3 Add integration tests for success, authentication/model enforcement, affinity, and upstream errors.

## 3. Verification and deployment

- [x] 3.1 Run focused tests, lint, type checks, and strict OpenSpec validation.
- [x] 3.2 Validate the official Codex SearchClient request contract against a real upstream through an isolated backend port.
- [x] 3.3 Restart only backend port 2456 and confirm Web Search and production logs are healthy.

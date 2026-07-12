## ADDED Requirements

### Requirement: Proxy rate-limit read model has a typed responsibility boundary

Rate-limit header construction, dashboard payload construction, usage refresh, usage-row lookup, and additional-quota projection MUST be implemented outside the proxy transport orchestration service behind an explicit typed capability boundary.

#### Scenario: Cached response headers

- **WHEN** a proxy endpoint requests rate-limit headers
- **THEN** the runtime MUST preserve existing cache behavior, eligible-account selection, primary and secondary summaries, latest-model weekly substitution, and credit headers

#### Scenario: Dashboard payload

- **WHEN** the dashboard requests current rate-limit status
- **THEN** the runtime MUST refresh usage and preserve plan type, primary and secondary windows, credits, and additional rate limits
- **AND** an empty eligible-account set MUST still produce the existing guest payload

#### Scenario: Additional quota pool availability

- **WHEN** additional primary or secondary quota rows exist for selected accounts
- **THEN** the runtime MUST preserve per-account availability semantics, reset metadata, canonical display labels, and output ordering

### Requirement: Rate-limit runtime remains independent of transport orchestration

The extracted runtime MUST depend only on an explicit repository-factory capability and MUST NOT import or dynamically inspect `app.modules.proxy.service`.

#### Scenario: Existing service caller

- **WHEN** API code or a focused test calls `ProxyService.rate_limit_headers()` or `ProxyService.get_rate_limit_payload()`
- **THEN** method lookup and returned contracts MUST remain compatible through inheritance

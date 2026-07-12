## ADDED Requirements

### Requirement: Proxy defaults Responses service tier to default

The proxy MUST forward Responses-compatible requests using `service_tier=default` when a client omits `service_tier` or explicitly sends `service_tier=auto`. The proxy MUST preserve explicit `priority` and `flex` service tiers, and MUST continue to treat `fast` as an alias for `priority`.

#### Scenario: omitted service tier uses default

- **WHEN** a client sends a Responses-compatible request without `service_tier`
- **THEN** the upstream payload uses `service_tier=default`
- **AND** reservation accounting and request logs use `default` as the requested service tier

#### Scenario: auto service tier uses default

- **WHEN** a client sends `service_tier=auto`
- **THEN** the upstream payload uses `service_tier=default`
- **AND** request logs MUST NOT record `auto` as the requested service tier

#### Scenario: explicit non-default service tiers remain available

- **WHEN** a client sends `service_tier=priority` or `service_tier=flex`
- **THEN** the proxy forwards that explicit tier unchanged

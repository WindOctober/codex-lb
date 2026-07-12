## ADDED Requirements

### Requirement: Automatic upstream egress failover

The proxy SHALL support automatic upstream egress selection for ChatGPT upstream requests without restarting codex-lb.

#### Scenario: switch new requests to proxy after direct failures
- **GIVEN** upstream egress mode is `auto`
- **AND** an upstream proxy URL is configured
- **WHEN** the direct upstream probe fails 3 consecutive times at the configured 5 second interval
- **AND** the proxy upstream probe succeeds 3 consecutive times
- **THEN** subsequent new upstream requests use the configured proxy URL
- **AND** active in-flight upstream streams are not migrated mid-flight

#### Scenario: direct HTTP response is not a route failure
- **GIVEN** upstream egress mode is `auto`
- **WHEN** the direct upstream probe reaches the probe URL and receives any HTTP response status
- **THEN** codex-lb treats the direct route as reachable
- **AND** codex-lb does not switch to proxy because of that response status alone

#### Scenario: switch new requests back to direct after recovery
- **GIVEN** upstream egress mode is `auto`
- **AND** new upstream requests are currently using the proxy route
- **WHEN** the direct upstream probe succeeds 3 consecutive times
- **AND** the 60 second route-switch cooldown has elapsed
- **THEN** subsequent new upstream requests use the direct route

#### Scenario: no automatic replay after transport failure
- **GIVEN** an upstream request has been sent on the selected egress route
- **WHEN** that request fails after it may have reached upstream
- **THEN** codex-lb SHALL NOT automatically replay the same request on another egress route

#### Scenario: expose egress probe state in dashboard runtime
- **GIVEN** upstream egress mode is enabled
- **WHEN** the dashboard requests bridge runtime status
- **THEN** the response includes the selected egress route, direct/proxy reachability, and the latest direct/proxy probe latency

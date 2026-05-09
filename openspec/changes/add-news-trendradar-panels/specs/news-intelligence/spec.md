# news-intelligence Delta

## ADDED Requirements

### Requirement: News refresh includes X AI dynamics and cross-platform hotspots
News refresh MUST expose two additional dashboard panels: latest AI dynamics sourced from X and a multi-platform current-affairs hotspot digest sourced from TrendRadar top items.

#### Scenario: Snapshot includes both panels
- **WHEN** `/api/news` returns a ready snapshot after a successful refresh
- **THEN** the payload includes `x_ai_dynamics` with a generated timestamp, summary, and item list
- **AND** the payload includes `hotspot_digest` with a generated timestamp, summary, clusters, and top items

### Requirement: TrendRadar integration does not use MCP
The cross-platform hotspot digest MUST use TrendRadar as a local CLI/data export source and MUST NOT require TrendRadar MCP.

#### Scenario: TrendRadar is unavailable
- **WHEN** the TrendRadar root or export command is unavailable
- **THEN** News refresh keeps the previous usable hotspot digest when present
- **AND** records a refresh error or empty fallback rather than crashing the dashboard render path

### Requirement: TrendRadar auto-refresh defaults to hourly
The News service SHOULD keep the existing full News/X refresh cadence while refreshing the TrendRadar-backed hotspot digest approximately hourly unless overridden by `CODEX_LB_TRENDRADAR_REFRESH_SECONDS`.

#### Scenario: No refresh interval overrides
- **WHEN** the News service is built without `CODEX_LB_NEWS_REFRESH_SECONDS` or `CODEX_LB_TRENDRADAR_REFRESH_SECONDS`
- **THEN** the full News/X refresh interval is 21600 seconds
- **AND** the TrendRadar hotspot refresh interval is 3600 seconds

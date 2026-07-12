## ADDED Requirements

### Requirement: Bulk account fast mode updates use account fast mode as source of truth
The accounts API SHALL expose a dashboard-authenticated bulk operation that sets `fast_service_tier_enabled` to the requested boolean for every account and returns the number of updated accounts. Subsequent proxy routing MUST use the updated per-account field through the existing account fast-mode behavior.

#### Scenario: Bulk disable account fast mode
- **WHEN** the dashboard calls the bulk account fast-mode API with `enabled=false`
- **THEN** every account row has `fast_service_tier_enabled=false`
- **AND** requests routed to those accounts are no longer promoted to `service_tier=priority` by account fast mode

#### Scenario: Bulk enable account fast mode
- **WHEN** the dashboard calls the bulk account fast-mode API with `enabled=true`
- **THEN** every account row has `fast_service_tier_enabled=true`
- **AND** eligible requests routed to those accounts are promoted by the existing account fast-mode behavior

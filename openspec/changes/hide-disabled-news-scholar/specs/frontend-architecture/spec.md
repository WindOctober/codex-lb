## ADDED Requirements

### Requirement: Optional News and Scholar surfaces follow refresh enablement

The dashboard shell MUST hide optional News and Scholar front-end surfaces when their corresponding refresh feature flag is disabled in server configuration.

#### Scenario: News refresh is disabled
- **WHEN** `/api/settings` returns `newsRefreshEnabled: false`
- **THEN** the dashboard navigation does not render a News entry
- **AND** visiting `/news` redirects to `/dashboard`

#### Scenario: Scholar refresh is disabled
- **WHEN** `/api/settings` returns `scholarRefreshEnabled: false`
- **THEN** the dashboard navigation does not render a Scholar entry
- **AND** visiting `/scholar` redirects to `/dashboard`

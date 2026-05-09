# usage-refresh-policy Delta

## ADDED Requirements
### Requirement: Pro capacity is twenty times Plus capacity

Capacity calculations for dashboard quota totals, account summaries, proxy usage payloads, and capacity-weighted routing MUST treat Pro capacity as twenty times Plus capacity for both 5h and weekly windows.

#### Scenario: Pro capacity is 20x Plus in both windows
- **WHEN** the system resolves plan capacity for Plus and Pro accounts
- **THEN** Pro 5h capacity is 20 times Plus 5h capacity
- **AND** Pro weekly capacity is 20 times Plus weekly capacity

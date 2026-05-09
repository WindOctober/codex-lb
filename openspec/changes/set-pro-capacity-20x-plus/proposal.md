# Change: Set Pro capacity to 20x Plus

## Motivation
Dashboard quota totals and capacity-weighted routing should reflect Pro accounts as twenty times the capacity of Plus accounts. The current capacity constants make Pro approximately 6.67x Plus.

## Scope
- Update Pro latest-model 5h and weekly capacity constants to 20x Plus.
- Keep Enterprise aligned with Pro, matching the existing capacity-tier mapping.
- Update focused tests and inline examples that describe the Pro/Plus capacity relationship.

## Non-Goals
- Change Free, Plus, Business, Team, or Edu capacity values.
- Change upstream usage refresh parsing or additional quota key discovery.

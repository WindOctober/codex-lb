# Tasks

- [x] Add OpenSpec requirements for account drain priority and Accounts-page strategy control.
- [x] Add `accounts.primary_drain_priority_enabled` model field, migration, repository update support, and summary/update schemas.
- [x] Clear drain-priority flags when settings switch routing strategy away from `primary_drain`.
- [x] Restrict `primary_drain` selection to flagged accounts when any scoped flagged account exists.
- [x] Move routing strategy selection UI from Settings to Accounts.
- [x] Add account detail star toggle and account list star indicator with disabled state outside `primary_drain`.
- [x] Update backend/frontend tests and mocks.
- [x] Add regression coverage for drain-priority accounts bypassing soft health-tier prefiltering.
- [x] Preserve primary-drain scores through selection input caching.
- [x] Run focused validation and rebuild frontend assets.

# Tasks

- [x] Add OpenSpec delta requirements for routing and frontend account usage timeline.
- [x] Extend backend routing strategy types, settings validation, and request-time strategy resolution with `primary_drain`.
- [x] Compute per-account primary-drain scores from recent primary usage history and inject them into `AccountState`.
- [x] Implement deterministic primary-drain selection with capacity-weighted fallback.
- [x] Add 7-day, 5h quota timeline fields to account trends responses.
- [x] Update frontend schemas, settings controls, status labels, mocks, and tests for `primary_drain`.
- [x] Render the quota timeline in account usage details and replace known GPT-5.3-Codex-Spark quota rows when timeline data exists.
- [x] Run focused backend and frontend validation.

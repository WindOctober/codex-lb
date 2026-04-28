# Tasks

- [x] Add confirmed-company lane fallback after novelty classification.
- [x] Add same-refresh semantic dedupe for unverified-signal candidates.
- [x] Add unverified-signal fallback after novelty classification to preserve the target display count when enough current-run candidates exist.
- [x] Preserve `is_new=false` for fallback retained old items.
- [x] Add unit coverage for per-lane fallback, same-refresh rumor dedupe, rumor target backfill, and latest-candidate selection.
- [x] Run focused backend validation.
- [x] Run isolated temporary-instance smoke validation without restarting live `2455`.
- [ ] Run `openspec validate --specs` (blocked locally: `openspec` CLI is not installed).

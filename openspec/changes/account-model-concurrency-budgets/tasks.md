# Tasks

- [x] Add configuration for the local per-account/model concurrency limit with a default of 32.
- [x] Implement an in-memory account/model concurrency limiter with idempotent slot release.
- [x] Exclude locally full account/model pairs during account selection.
- [x] Count HTTP bridge queued/pending requests against the limiter and release on completion, failure, and forced pending-request failure.
- [x] Avoid reusing a cached HTTP bridge session when its account/model request budget is already full.
- [x] Add a held HTTP bridge session budget per account/model and wait locally when all eligible bridge slots are full.
- [x] Add focused unit coverage for the limiter and routing exclusion behavior.
- [x] Validate OpenSpec and run the focused tests.

## 1. Implementation

- [x] Add a creation-time HTTP bridge account bias based on remaining-usage waterline.
- [x] Keep strict preferred-account continuity paths unchanged.
- [x] Stop excluding accounts merely because they already own active local bridge sessions.
- [x] Fall back to the configured routing strategy when no account is meaningfully above average.
- [x] Preserve existing local account/model session budget exclusions so high-waterline accounts only receive traffic up to configured parallel capacity.

## 2. Tests

- [x] Cover high-waterline selection choosing a 100%-remaining account above the pool average.
- [x] Cover near-even accounts falling back to the configured routing strategy.
- [x] Cover fresh HTTP bridge creation passing waterline preference without active-session exclusion.

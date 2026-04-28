# Change: Preserve news display fallbacks

## Motivation
The news dashboard should not drop an official company lane or leave the rumor grid underfilled when candidates are classified as already-known history. If a cache rebuild or history migration changes the previous snapshot, users still need the latest available current-run items displayed, while novelty labels continue to distinguish genuinely new items from fallback retained items.

## Scope
- Keep latest candidate per confirmed-company lane when novelty filtering would otherwise remove that lane.
- Deduplicate unverified-signal candidates within the same refresh by semantic claim, not only by URL.
- Keep the unverified-signal section filled up to its target count using latest current-run fallback candidates when semantic novelty filtering removes too many items.
- Preserve novelty semantics: fallback retained items are displayed but not marked as new.

## Non-Goals
- Change the external search prompts or model used for news refresh.

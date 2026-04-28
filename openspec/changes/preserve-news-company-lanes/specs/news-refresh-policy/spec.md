# news-refresh-policy Delta

## ADDED Requirements
### Requirement: Confirmed company lanes retain a display fallback

After semantic novelty filtering, each confirmed-company lane that had at least one current candidate MUST retain one candidate for display if the lane would otherwise be empty. The retained fallback MUST be the latest candidate for that lane according to the normal company sorting rules.

#### Scenario: OpenAI candidate is old but remains visible
- **WHEN** the OpenAI confirmed-company candidate is classified as not new
- **AND** no other OpenAI confirmed-company candidate is classified as new
- **THEN** the latest OpenAI confirmed-company candidate remains in the news payload
- **AND** it is not marked as new

#### Scenario: New candidate takes precedence
- **WHEN** a confirmed-company lane has at least one candidate classified as new
- **THEN** only the new candidates from that lane are retained by novelty filtering

### Requirement: Unverified signals retain target-count display fallbacks

After semantic novelty filtering, the unverified-signal section MUST retain genuinely new current-run candidates first. If fewer than the configured target count remain and the current run produced additional candidates, the section MUST add the latest not-new current-run candidates until it reaches the target count or exhausts current-run candidates.

#### Scenario: Old rumors backfill an underfilled grid
- **WHEN** the current run produces nine unverified-signal candidates
- **AND** semantic novelty filtering classifies only two candidates as new
- **THEN** the news payload retains the two new candidates
- **AND** it adds the latest seven not-new candidates from the current run
- **AND** the fallback retained candidates are not marked as new

### Requirement: Unverified signals are deduplicated within the current refresh

Before semantic novelty filtering and target-count fallback, unverified-signal candidates from the same refresh MUST be deduplicated by the underlying claim, not only by source URL or account.

#### Scenario: Different accounts repeat the same rumor
- **WHEN** two current-run unverified-signal candidates describe the same underlying rumor
- **AND** they use different source URLs or accounts
- **THEN** only one candidate for that rumor is retained for downstream novelty filtering and display fallback

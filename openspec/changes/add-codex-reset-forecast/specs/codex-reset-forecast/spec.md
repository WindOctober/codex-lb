## ADDED Requirements

### Requirement: Codex reset forecast API

The application SHALL expose a dashboard-authenticated Codex reset forecast API that returns a probability for an OpenAI Codex usage-limit reset in the next 24 hours.

#### Scenario: Return transparent forecast details

- **WHEN** an authenticated dashboard user requests the Codex reset forecast
- **THEN** the API returns the probability, confidence label, generated timestamp, forecast horizon, score factors, current evidence, historical precursor-to-reset examples, live collection status, and the latest inspected X items

#### Scenario: Explain stale signal state

- **WHEN** no current precursor signal is active within the scoring window
- **THEN** the API still returns a low probability and explains that no recent public signal is active

#### Scenario: Score only post-reset precursor signals

- **WHEN** the collector or manual signal input contains a deterministic confirmation that Codex usage limits have already been reset
- **THEN** the predictor treats that confirmation as the latest reset-cycle boundary rather than as evidence of another imminent reset
- **AND** precursor signals at or before that boundary are excluded from the next-24-hour probability
- **AND** the API explains that the next reset forecast is based only on signals observed after the latest confirmed reset

### Requirement: Codex reset forecast live collection

The application SHALL periodically inspect recent Tibo and Sam X posts/replies for Codex usage-limit reset precursor signals and cache the detected signals for scoring.

#### Scenario: Refresh recent public signals

- **WHEN** the Codex reset forecast collector is enabled
- **THEN** the backend attempts a background refresh at least once per configured interval, defaulting to 43200 seconds
- **AND** the refresh uses the configured Codex/MCP runtime to inspect recent X posts and replies from `@thsottiaux` and `@sama`
- **AND** the refresh caches the latest inspected posts/replies separately from reset precursor signals
- **AND** detected signals are retained for scoring only while they are within the freshness window
- **AND** completed reset confirmations are cached as non-scoring cycle-boundary context beyond the normal freshness window so later scoring can distinguish already-completed resets from next-reset precursors

#### Scenario: Preserve refresh cadence across restarts

- **WHEN** codex-lb restarts before the configured refresh interval has elapsed since the latest cached collection completion or attempt
- **THEN** the collector MUST wait until the cached next refresh due time instead of starting a new live refresh from the restart time
- **AND** the API exposes the next refresh due time based on the latest persisted collection timestamp

#### Scenario: Surface latest inspected X items

- **WHEN** the collector completes a refresh
- **THEN** the API exposes a bounded list of the latest inspected Tibo/Sam X posts or replies with author, timestamp, text summary, URL, and reset relevance
- **AND** reset-relevant items expose the visible source text needed to verify the signal
- **AND** unrelated inspected items expose short summaries rather than raw post text
- **AND** reply or quote context is exposed only when it is needed to understand a reset-related signal

#### Scenario: Surface collection failures

- **WHEN** a live collection attempt fails or has not completed
- **THEN** the API returns the last refresh timestamps and error state so the page can distinguish "no active signal" from "collector did not run successfully"
- **AND** the exposed error state is a bounded, human-readable summary rather than raw worker stdout, stderr, JSON payloads, or inspected X content

### Requirement: Codex reset forecast page

The SPA SHALL provide a Reset Odds route parallel to Dashboard that presents the next-24-hour reset probability and the evidence behind the score.

#### Scenario: View reset odds

- **WHEN** a dashboard user opens Reset Odds
- **THEN** the page displays the probability prominently, shows the confidence, lists the active evidence and score factors, shows live collection status, renders the latest inspected X posts/replies, and includes the confirmed historical examples used by the predictor

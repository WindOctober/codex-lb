# Astra Model Support

## Purpose
Provide GPT-6 Astra discovery and usage accounting for Codex clients using the bootstrap catalog.

## Requirements

### Requirement: Astra bootstrap discovery
Before a successful dynamic model refresh, the system SHALL expose `gpt-6-astra` through the existing model-list endpoints when permitted by the requesting API key. The catalog SHALL retain existing models and SHALL NOT change the user's default model.

#### Scenario: Static catalog used by Codex IDE
- **WHEN** model refresh is disabled and an unrestricted key requests `/backend-api/codex/models`
- **THEN** the response includes Astra with `visibility: list`, text/image input, medium default reasoning, and low/medium/high/xhigh/max/ultra Codex reasoning levels
- **AND** the Codex context window is 272000 and max_context_window is 872000
- **AND** the OpenAI-style data alias includes `gpt-6-astra`

#### Scenario: API-key restriction remains authoritative
- **WHEN** an API key allows only a different model
- **THEN** Astra is excluded from that key's model catalog

#### Scenario: Refreshed catalog supersedes bootstrap
- **WHEN** a successful upstream model snapshot does not include Astra
- **THEN** the bootstrap entry does not reintroduce Astra into the refreshed catalog

### Requirement: Astra request and cost compatibility
The system SHALL preserve the exact `gpt-6-astra` model identifier in Responses payloads and SHALL recognize Astra pricing for cost accounting. Standard input/cached-input/output rates SHALL be 10/1/50 USD per million tokens; priority rates SHALL be 20/2/100 and flex rates 5/0.5/25. Standard requests over 272000 input tokens SHALL use 20/2/75 rates. Explicit cache writes SHALL use a 1.25 input multiplier.

#### Scenario: Standard Astra request
- **WHEN** a Responses request selects Astra with reasoning and supported prompt-cache controls
- **THEN** the request validates and retains its model identifier
- **AND** 100000 uncached input tokens plus 1000 output tokens at standard tier cost 1.05 USD

#### Scenario: Astra long context
- **WHEN** a standard Astra request reports 300000 uncached input tokens and 1000 output tokens
- **THEN** the estimated cost is 6.075 USD

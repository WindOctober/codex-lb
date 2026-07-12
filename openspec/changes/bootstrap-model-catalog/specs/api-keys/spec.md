## MODIFIED Requirements

### Requirement: Public model list filtering

All model list endpoints SHALL filter models using a single predicate that requires both conditions:
1. `model.supported_in_api` is true
2. If `allowed_models` is configured, the model is in the allowed set

This predicate SHALL be applied consistently across `/api/models`, `/v1/models`, and `/backend-api/codex/models`.

When the upstream model registry has no snapshot, these endpoints SHALL use a bundled bootstrap Codex model catalog instead of returning an empty model list.

`/backend-api/codex/models` SHALL include both its native `models` array and an OpenAI-compatible `data` array containing the list-visible models.

#### Scenario: Unsupported model excluded from /v1/models

- **WHEN** a model snapshot contains a model with `supported_in_api=false`
- **THEN** that model is not included in the `/v1/models` response

#### Scenario: Unsupported model excluded from /backend-api/codex/models

- **WHEN** a model snapshot contains a model with `supported_in_api=false`
- **THEN** that model is not included in the `/backend-api/codex/models` response

#### Scenario: Allowed but unsupported model excluded

- **WHEN** a model is in the `allowed_models` set but has `supported_in_api=false`
- **THEN** that model is not exposed in any model list endpoint

#### Scenario: Consistent model set across endpoints

- **GIVEN** any model registry state
- **THEN** `/api/models`, `/v1/models`, and `/backend-api/codex/models` expose the same set of models

#### Scenario: Bootstrap catalog used before registry refresh

- **GIVEN** the upstream model registry has no snapshot
- **WHEN** a client calls `/v1/models` or `/backend-api/codex/models`
- **THEN** the response includes the bundled bootstrap Codex models

#### Scenario: Codex model catalog has OpenAI-compatible data alias

- **WHEN** a client calls `/backend-api/codex/models`
- **THEN** the response includes `models` entries and a `data` model-list array

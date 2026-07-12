## Why

When the dynamic upstream model registry is disabled or has not refreshed yet, local model endpoints can return empty model lists. That breaks Codex clients that rely on `/backend-api/codex/models` or `/v1/models` during startup.

## What Changes

- Add a bundled Codex bootstrap model catalog used before the first successful upstream refresh.
- Keep `/backend-api/codex/models`, `/v1/models`, and `/api/models` on the same public model visibility predicate.
- Add an OpenAI-style `data` alias to the Codex model catalog response for clients that read model list data from that endpoint.

## Impact

- Codex/IDE clients can discover supported models even when model registry refresh is disabled.
- API-key model filtering and `supported_in_api=false` filtering remain enforced.

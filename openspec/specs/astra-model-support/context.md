# Astra support context

The local runtime explicitly sets CODEX_LB_MODEL_REGISTRY_ENABLED=false. Model discovery therefore depends on the bundled catalog, not an automatic upstream fetch. Origin and upstream main were fetched and inspected on 2026-09-05; neither contained an Astra entry to reuse.

Codex metadata comes from the operator's Windows ~/.codex/models_cache.json observed on 2026-09-05. Its 272K default / 872K maximum context and ultra reasoning option are Codex-specific and are not inferred from the public API's 1.05M limit or API reasoning options.

Published pricing source: https://developers.openai.com/api/docs/models/gpt-6-astra . Existing model pricing and defaults are retained. The bootstrap catalog is discovery metadata, not a grant of upstream account access; the upstream continues to enforce entitlements.

Rollout uses an isolated backend on 3456, then restarts only 2456. Caddy stays on 2455. Use the same authenticated key as the remote Codex client to verify the catalog. Do not log credentials.

## Verified rollout (2026-09-05)

Focused regression suite: 84 passed. After tightening the dashboard assertion, all 13 model-discovery integration tests passed again. Scoped Ruff checks and OpenSpec validation passed (32 main specs).

The isolated backend on 3456 passed liveness, readiness, dashboard, and authenticated model discovery. A real streamed request completed in 4.04 seconds, reported model gpt-6-astra, and returned ASTRA_OK. The isolated backend was stopped before restarting only backend 2456. Both gateway 2455 and backend 2456 subsequently passed liveness/readiness and exposed Astra on /backend-api/codex/models and /v1/models. Caddy was left running.

The remote client's stale model cache, which lacked Astra, was renamed to a timestamped backup in ~/.codex to allow discovery to refresh on reload. The configured default model remains unchanged. Original modified source/test files were backed up under var/backups/astra-support-20260905-122710/.

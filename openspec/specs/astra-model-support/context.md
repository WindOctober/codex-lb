# Astra support context

The local runtime explicitly sets CODEX_LB_MODEL_REGISTRY_ENABLED=false. Model discovery therefore depends on the bundled catalog, not an automatic upstream fetch. Origin and upstream main were fetched and inspected on 2026-09-05; neither contained an Astra entry to reuse.

Codex metadata comes from the operator's Windows ~/.codex/models_cache.json observed on 2026-09-05. Its 272K default / 872K maximum context and ultra reasoning option are Codex-specific and are not inferred from the public API's 1.05M limit or API reasoning options.

Published pricing source: https://developers.openai.com/api/docs/models/gpt-6-astra . Existing model pricing and defaults are retained. The bootstrap catalog is discovery metadata, not a grant of upstream account access; the upstream continues to enforce entitlements.

Rollout uses an isolated backend on 3456, then restarts only 2456. Caddy stays on 2455. Use the same authenticated key as the remote Codex client to verify the catalog. Do not log credentials.

## Verified rollout (2026-09-05)

Focused regression suite: 84 passed. After tightening the dashboard assertion, all 13 model-discovery integration tests passed again. Scoped Ruff checks and OpenSpec validation passed (32 main specs).

The isolated backend on 3456 passed liveness, readiness, dashboard, and authenticated model discovery. A real streamed request completed in 4.04 seconds, reported model gpt-6-astra, and returned ASTRA_OK. The isolated backend was stopped before restarting only backend 2456. Both gateway 2455 and backend 2456 subsequently passed liveness/readiness and exposed Astra on /backend-api/codex/models and /v1/models. Caddy was left running.

The remote client's stale model cache, which lacked Astra, was renamed to a timestamped backup in ~/.codex to allow discovery to refresh on reload. The configured default model remains unchanged. Original modified source/test files were backed up under var/backups/astra-support-20260905-122710/.

## Codex introduction announcement

The Codex extension uses availability_nux (app-server availabilityNux) to decide whether to offer its bundled Introducing dialog. The Astra bootstrap entry now includes the introduction text already present in the installed extension. The API-key user's explicit model_catalog_json file is synchronized separately; updating only the gateway does not update a running app-server's catalog.

Example: while Sol remains selected, reload the remote VS Code window after synchronizing the catalog. Astra can then become eligible for the introduction. The client still suppresses introductions for models it has already recorded as seen or that are currently selected. No dismissal records are cleared and no client frontend files are patched.

Validation: 38 focused tests and all 32 OpenSpec specifications passed. Isolated backend 3456 passed health, dashboard, and announcement discovery. The installed extension app-server returned a visible GPT-6-Astra entry with the same nonempty availabilityNux message.

Production rollout: restarted only backend 2456; after its shutdown grace period expired, the restart script replaced the old backend process. Both 2455 and 2456 passed liveness/readiness and returned the announcement metadata. Caddy remained running. The operator currently has Astra selected, so they must switch to Sol before reloading to satisfy the client announcement condition.

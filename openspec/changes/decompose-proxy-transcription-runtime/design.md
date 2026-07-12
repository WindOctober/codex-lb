## Context

`ProxyService.transcribe()` is a complete audio endpoint workflow: it filters proxy-only headers, establishes a total request deadline, selects an account, refreshes credentials, invokes the upstream transcription client with timeout overrides, retries once after a 401, updates account health, translates failures, and writes a terminal request log. It does not share mutable stream or HTTP bridge session state.

Existing tests patch module-level settings, remaining-budget, and upstream-client functions in `service.py`. The service already exposes compatibility methods for settings and budget access, but transcription needs one additional narrow upstream-client wrapper to preserve that test and diagnostic seam.

## Goals / Non-Goals

**Goals:**

- Move the entire transcription workflow behind a typed mixin boundary.
- Keep `ProxyService.transcribe()` and its signature compatible through inheritance.
- Preserve deadline checks, timeout scopes, freshness behavior, 401 retry count, account health updates, error mapping, and request logging.
- Preserve existing `service.py` monkeypatch seams for settings, budget, and upstream invocation.

**Non-Goals:**

- Change the transcription model or endpoint payloads.
- Add cross-account retry, streaming transcription, or alternative models.
- Change routing strategy, account eligibility, request-budget values, or refresh policy.
- Refactor the shared account-selection/failure state machine in this stage.

## Decisions

### Use a `_TranscriptionRuntimeMixin`

The endpoint is cohesive but needs existing service capabilities for account selection, freshness, health updates, encryption, logging, and configuration. A typed protocol documents those dependencies while keeping the workflow outside the facade.

Alternative considered: create an independently constructed transcription service. That would require additional dependency injection and duplicate proxy request capabilities during a structural extraction.

### Preserve module patch points through capabilities

The mixin calls `_proxy_runtime_settings()`, `_proxy_dashboard_settings()`, `_remaining_budget_seconds_compatible()`, and a new `_core_transcribe_audio_compatible()` wrapper. The wrappers remain thin and let existing tests and operators replace `service.py` functions without the extracted module importing the monolith.

Alternative considered: update tests to patch the new module. That would silently break existing private diagnostic seams and make the extraction harder to distinguish from behavior change.

### Keep the nested upstream call helper local

The helper captures filtered headers, request deadline, request ID, and audio fields for one request. Keeping it inside `transcribe()` avoids a broad argument list and preserves exact timeout push/pop scope.

## Risks / Trade-offs

- [Risk] The 401 path performs more than one retry. -> Preserve the existing single nested retry and run budget-exhaustion coverage.
- [Risk] Timeout overrides leak after an exception. -> Retain the existing `try/finally` push/pop boundary and focused tests.
- [Risk] Account health is updated differently. -> Run success, generic failure, refresh failure, and selection failure tests.
- [Risk] Existing module-level patches stop working. -> Exercise patched `core_transcribe_audio`, settings, and budget paths through `ProxyService`.

## Migration Plan

1. Add the typed transcription module and copy the endpoint workflow without semantic edits.
2. Add the thin upstream-client compatibility method, inherit the mixin, and remove the local method body.
3. Ratchet architecture tests and run focused transcription plus API contract tests.
4. Run static and isolated-backend verification without touching the primary backend or Caddy.

Rollback is a source-level move back into `service.py`; no data or configuration migration is involved.

## Open Questions

None.

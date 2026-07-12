# Tasks

- [x] Add frontend-architecture requirements for hiding disabled News/Scholar surfaces.
- [x] Expose News/Scholar refresh enablement in the dashboard settings response.
- [x] Filter News/Scholar navigation entries from desktop and mobile header.
- [x] Guard direct `/news` and `/scholar` routes when disabled.
- [x] Add focused frontend/backend coverage.
- [x] Run focused validation and record any blocked OpenSpec validation.

## Validation

- `.venv/bin/python -m pytest tests/integration/test_settings_api.py tests/unit/test_external_refresh_switches.py` passed.
- `direnv exec . bash -c 'cd frontend && bun run test -- App.test.tsx components/layout/app-header.test.tsx features/settings/schemas.test.ts features/settings/components/routing-settings.test.tsx features/settings/hooks/use-settings.test.ts'` passed.
- `bun run typecheck` passed.
- `bunx eslint <touched frontend files> -f json` passed with no findings.
- `direnv exec . bash -c 'cd frontend && bun run build'` passed.
- `direnv exec . openspec validate --specs` passed.
- `git diff --check` passed.

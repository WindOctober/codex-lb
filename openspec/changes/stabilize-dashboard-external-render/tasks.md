## Tasks

- [x] Harden dashboard theme initialization against storage/media-query API failures.
- [x] Add a visible frontend error fallback instead of a blank root.
- [x] Set explicit cache headers for SPA HTML and static assets.
- [ ] Validate frontend typecheck/build and targeted tests.

## Validation Notes

- `cd frontend && bun run typecheck`: passed.
- `cd frontend && bun --bun run build`: passed.
- `.venv/bin/python -m ruff check app/main.py`: passed.
- `cd frontend && bun run test -- src/hooks/use-theme.test.ts`: blocked by current Node 20.9/jsdom ESM loader incompatibility before tests loaded.
- `cd frontend && bun --bun run test -- src/hooks/use-theme.test.ts`: blocked by Vitest/Bun SSR dependency import issue before tests loaded.

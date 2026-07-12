# AGENTS

## Environment

- Python: .venv/bin/python (uv, CPython 3.13.3)
- GitHub auth for git/API is available via env vars: `GITHUB_USER`, `GITHUB_TOKEN` (PAT). Do not hardcode or commit tokens.
- For authenticated git over HTTPS in automation, use: `https://x-access-token:${GITHUB_TOKEN}@github.com/<owner>/<repo>.git`

## Code Conventions

The `/project-conventions` skill is auto-activated on code edits (PreToolUse guard).

| Convention | Location | When |
|-----------|----------|------|
| Code Conventions (Full) | `/project-conventions` skill | On code edit (auto-enforced) |
| Git Workflow | `.agents/conventions/git-workflow.md` | Commit / PR |

## Workflow (OpenSpec-first)

This repo uses **OpenSpec as the primary workflow and SSOT** for change-driven development.

### How to work (default)

1) Find the relevant spec(s) in `openspec/specs/**` and treat them as source-of-truth.
2) If the work changes behavior, requirements, contracts, or schema: create an OpenSpec change in `openspec/changes/**` first (proposal -> tasks).
3) Implement the tasks; keep code + specs in sync (update `spec.md` as needed).
4) Validate specs locally: `openspec validate --specs`
5) When done: verify + archive the change (do not archive unverified changes).

### Source of Truth

- **Specs/Design/Tasks (SSOT)**: `openspec/`
  - Active changes: `openspec/changes/<change>/`
  - Main specs: `openspec/specs/<capability>/spec.md`
  - Archived changes: `openspec/changes/archive/YYYY-MM-DD-<change>/`

## Documentation & Release Notes

- **Do not add/update feature or behavior documentation under `docs/`**. Use OpenSpec context docs under `openspec/specs/<capability>/context.md` (or change-level context under `openspec/changes/<change>/context.md`) as the SSOT.
- **Do not edit `CHANGELOG.md` directly.** Leave changelog updates to the release process; record change notes in OpenSpec artifacts instead.

### Documentation Model (Spec + Context)

- `spec.md` is the **normative SSOT** and should contain only testable requirements.
- Use `openspec/specs/<capability>/context.md` for **free-form context** (purpose, rationale, examples, ops notes).
- If context grows, split into `overview.md`, `rationale.md`, `examples.md`, or `ops.md` within the same capability folder.
- Change-level notes live in `openspec/changes/<change>/context.md` or `notes.md`, then **sync stable context** back into the main context docs.

Prompting cue (use when writing docs):
"Keep `spec.md` strictly for requirements. Add/update `context.md` with purpose, decisions, constraints, failure modes, and at least one concrete example."

### Commands (recommended)

- Start a change: `/opsx:new <kebab-case>`
- Create artifacts (step): `/opsx:continue <change>`
- Create artifacts (fast): `/opsx:ff <change>`
- Implement tasks: `/opsx:apply <change>`
- Verify before archive: `/opsx:verify <change>`
- Sync delta specs → main specs: `/opsx:sync <change>`
- Archive: `/opsx:archive <change>`

## Local codex-lb Change Safety

The local Caddy gateway is fixed at `2455 -> 2456` for normal development. Do not start, stop, replace, or retarget Caddy for ordinary app/backend changes.

When modifying codex-lb, validate the change on a separate local codex-lb backend before touching the primary backend:

1) Start an isolated backend-only codex-lb instance on a non-primary port and verify `/health/live`, the dashboard API, and the changed behavior directly against that backend port. Do not use 2455 or 2456 for this preflight.
2) Keep the existing 2455 Caddy gateway and 2456 backend running while the isolated backend is being validated.
3) Stop the isolated backend by its explicit non-primary port/PID only. Do not use Caddy switch scripts for ordinary preflight cleanup.
4) After validation passes, restart only the codex-lb backend on port 2456. Do not restart the 2455 Caddy gateway unless gateway/Caddy configuration changed.
5) Re-check `http://127.0.0.1:2455/health/live` and `http://127.0.0.1:2456/health/live` after the backend restart.

Suggested backend-only preflight shape:

```bash
CODEX_LB_PORT=3456 CODEX_LB_HOST=127.0.0.1 ./restart-codex-lb.sh
curl -fsS http://127.0.0.1:3456/health/live
curl -fsS http://127.0.0.1:3456/api/dashboard/bridge-runtime
CODEX_LB_PORT=3456 CODEX_LB_HOST=127.0.0.1 ./restart-codex-lb.sh --stop-only
```

Suggested backend restart target after validation:

```bash
CODEX_LB_PORT=2456 CODEX_LB_HOST=127.0.0.1 ./restart-codex-lb.sh
```

Use `switch-codex-lb-caddy.sh` only for intentional gateway/Caddy work. It is guarded by explicit confirmation environment variables because accidental Caddy stops can interrupt active Codex sessions.

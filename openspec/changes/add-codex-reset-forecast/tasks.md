# Tasks

- [x] Add a typed Codex reset forecast backend module and dashboard API.
- [x] Seed the predictor with confirmed precursor/reset examples and scoring rationale.
- [x] Add a Reset Odds SPA route and navigation item parallel to Dashboard.
- [x] Render probability, evidence, timeline examples, and scoring factors in a polished page.
- [x] Add backend and frontend contract tests.
- [x] Run Python and frontend validation. OpenSpec validation is blocked locally because the `openspec` CLI is not installed.
- [x] Add hourly Tibo/Sam X collector backed by the configured Codex/MCP runtime.
- [x] Surface collector status in the API and Reset Odds page.
- [x] Validate collector behavior on an isolated backend before restarting the primary backend.
- [x] Add latest inspected Tibo/Sam X posts/replies to the API and page.
- [x] Treat completed reset confirmations as cycle boundaries before scoring next-reset odds.
- [x] Bound and sanitize live collector worker errors before exposing them in the API/page.
- [x] Restrict live collector verbatim X text to reset-relevant evidence and summarize unrelated inspected items.
- [x] Change live collection cadence to 12 hours and preserve the cached refresh window across codex-lb restarts.

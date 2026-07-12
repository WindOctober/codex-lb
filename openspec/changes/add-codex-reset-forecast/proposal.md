# Change Proposal

The dashboard has no dedicated place to track public Codex usage-limit reset signals. Operators currently infer reset odds from X posts and replies manually, which makes timing and confidence hard to compare across reset cycles.

## Changes

- Add a Codex reset forecast surface parallel to Dashboard in the SPA navigation.
- Add a dashboard-authenticated API that returns a next-24-hour reset probability, confidence, score breakdown, known signal examples, and current evidence.
- Seed the predictor with confirmed May 2026 examples that show the observed precursor-to-reset timing.
- Add a narrow hourly background refresh that uses the configured Codex/MCP runtime to inspect recent Tibo/Sam X posts and replies, cache detected reset precursor signals, and feed them into the forecast.
- Distinguish completed reset confirmations from future reset precursors so the next-24-hour forecast starts from the latest confirmed reset boundary.

## Out of Scope

- Persisting social posts to the database.
- Automatically restarting or changing Codex account routing based on the forecast.

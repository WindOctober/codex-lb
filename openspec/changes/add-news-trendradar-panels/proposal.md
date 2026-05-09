# Change: Add TrendRadar-backed news intelligence panels

## Why
The News page currently focuses on confirmed OpenAI/Anthropic updates and high-heat X rumors. The dashboard needs a faster, broader intelligence layer that refreshes about hourly and surfaces both X-based AI dynamics and cross-platform current affairs hotspots.

## What Changes
- Keep the existing full News/X refresh cadence and add an hourly TrendRadar-only refresh cadence.
- Add two snapshot panels: latest AI dynamics from X and a TrendRadar-backed top-20 multi-platform hotspot digest.
- Use TrendRadar as a local CLI/data source, not MCP, and export a Codex-LB-friendly JSON contract.
- Update the News iframe design to present the new panels clearly with a modern visual style.

## Out of Scope
- Running TrendRadar MCP.
- Restarting the live codex-lb instance.
- Persisting the new panels into SQL history beyond the existing cache snapshot.

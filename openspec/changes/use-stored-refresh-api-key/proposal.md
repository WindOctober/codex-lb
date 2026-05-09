# Change: Use stored API key for refresh jobs

## Motivation
News and Scholar refresh jobs invoke the local codex-lb proxy through Codex CLI. When proxy API-key auth is enabled, falling back to `OPENAI_API_KEY` or `~/.codex/auth.toml` can send an upstream OpenAI key to codex-lb and fail with `401 Invalid API key`.

## Scope
- Prefer explicit refresh API key environment variables.
- If no explicit value is configured, load the stored codex-lb API key named `Pro-Only (Spread)` from the local database.
- Apply the same behavior to News and Scholar refresh jobs.
- Stop treating `OPENAI_API_KEY` or `~/.codex/auth.toml` as valid codex-lb refresh API key fallbacks.

## Non-Goals
- Trigger a refresh during deployment.
- Change API-key enforcement semantics.

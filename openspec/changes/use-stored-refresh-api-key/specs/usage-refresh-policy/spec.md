# usage-refresh-policy Delta

## ADDED Requirements
### Requirement: Refresh jobs use codex-lb API keys

News and Scholar refresh jobs that call the local codex-lb proxy MUST use a codex-lb API key, not an upstream OpenAI API key. Explicit refresh API-key environment variables MUST take precedence. When no explicit value is configured, the jobs MUST attempt to load the stored `Pro-Only (Spread)` codex-lb API key from the local database.

#### Scenario: Stored refresh key is used when env is absent
- **WHEN** proxy API-key auth is enabled
- **AND** no explicit refresh API-key environment variable is set
- **AND** an active stored API key named `Pro-Only (Spread)` exists with a decryptable plaintext copy
- **THEN** News and Scholar refresh jobs use that key for local codex-lb proxy calls

#### Scenario: OpenAI key is not used as codex-lb refresh key
- **WHEN** no explicit codex-lb refresh API key is configured
- **AND** only `OPENAI_API_KEY` or `~/.codex/auth.toml` is available
- **THEN** refresh jobs do not use that value as a codex-lb API key

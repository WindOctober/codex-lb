# Change: Add GPT-5.5 pricing

## Motivation
Dashboard and API key cost accounting currently resolve `gpt-5.5` snapshot names through the broad `gpt-5*` alias, which applies stale GPT-5 rates and undercounts latest-model usage.

## Scope
- Add a canonical `gpt-5.5` price table entry for standard, flex, priority, and long-context rates.
- Add a `gpt-5.5*` alias so snapshot model IDs resolve to the canonical `gpt-5.5` entry.
- Cover the corrected alias and rate behavior with focused pricing unit tests.
- Recompute existing local `gpt-5.5*` request log costs after taking a SQLite backup.

## Non-Goals
- Restart or mutate any running backend process.

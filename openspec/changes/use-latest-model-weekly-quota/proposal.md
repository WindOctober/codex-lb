# Change: Use latest-model weekly quota

## Motivation
Weekly and 5h capacity calculations should represent the configured latest model instead of stale generic weekly ratios. Latest models can have dedicated additional quota keys, and operators need a simple manual update path when the latest model changes.

## Scope
- Add a latest-model configuration file and one-command update script.
- Set the current latest model to `gpt-5.5`.
- Adjust plan capacity constants so Plus weekly capacity is approximately ten Free weekly units, with 5h capacity derived from the existing 7d/5h ratio.
- Prefer latest-model additional quota rows for account summaries, dashboard aggregates, proxy usage payloads, and gated model routing decisions when available.
- Fall back to existing generic usage rows when latest-model additional quota data is absent.
- For grouped dashboard account quota bars, aggregate only non-deactivated members whose plan supports the configured latest model; unsupported Free accounts must not dilute latest-model weekly capacity.

## Non-Goals
- Automatically discover the latest model.
- Change API-key provider token accounting.

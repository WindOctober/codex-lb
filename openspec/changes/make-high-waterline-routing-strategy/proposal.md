## Why

High-waterline routing already exists as an internal HTTP bridge bias, but it is not exposed as a first-class routing strategy. That makes the dashboard strategy value misleading: a request can be configured as capacity-weighted while a separate high-waterline branch deterministically selects the account whose remaining usage is meaningfully above the pool average.

Operators need the high-waterline behavior to be explicit, default, and consistently available across normal account selection paths. The old round-robin strategy is no longer part of the desired routing model.

## What Changes

- Promote high-waterline routing to a first-class `routing_strategy` value.
- Make high-waterline the default dashboard routing strategy for fresh installs and pristine existing settings.
- Remove the round-robin routing strategy from backend validation, selection, settings API schema, and dashboard controls.
- Use capacity-weighted routing as the high-waterline fallback when no eligible account is meaningfully above the pool average.
- Keep capacity-weighted and usage-weighted as explicit selectable strategies.

## Impact

- Existing non-pristine settings keep their configured strategy unless they still reference removed round-robin, which is normalized to high-waterline.
- Fresh installs and pristine default rows use high-waterline by default.
- Near-even pools continue to use capacity-weighted behavior under the default strategy.

## Why
The deployed proxy has dynamic model refresh disabled, and its bootstrap catalog omits GPT-6 Astra. Updated Codex IDE clients therefore cannot discover the model through this provider.

## What Changes
- Add `gpt-6-astra` to the bootstrap catalog with metadata observed in the operator's current Codex model cache.
- Add standard, priority, flex, cache-write, and standard long-context pricing using the published model pricing.
- Keep upstream snapshots authoritative and preserve API-key restrictions and existing model defaults.

## Impact
Model discovery and pricing only. No database migration, gateway change, authentication change, or quota-policy change.

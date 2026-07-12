## Why

HTTP bridge sessions stay pinned to the account selected at creation time. When one account has much more remaining usage than the rest of the pool, random capacity-weighted selection can still spread new bridge sessions across lower-waterline accounts. That prevents the pool from converging toward an even usage level and makes a 100%-remaining account underused even though its local parallel budget could absorb the burst.

## What Changes

- Make fresh HTTP bridge session creation waterline-aware.
- For non-preferred bridge creations, bias selection toward accounts whose remaining usage percentage is above the eligible pool average.
- Continue relying on existing local account/model session budgets to exclude a high-waterline account after it reaches configured parallel capacity.
- If no account is meaningfully above the average, use the configured routing strategy so near-even pools keep normal behavior.
- Preserve hard continuity and previous-response owner paths by not applying this bias when a preferred account is required or provided.

## Impact

- No durable schema changes.
- Existing bridge sessions are not moved or restarted.
- New soft shards and busy parallel bridges should concentrate on high-waterline accounts such as a 100%-remaining account until local parallel capacity is reached, then fall back to other eligible accounts.

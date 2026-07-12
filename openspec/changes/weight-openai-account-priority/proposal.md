## Why

Operators can set `upstream_priority` on OpenAI OAuth accounts, but the capacity-weighted route currently treats OpenAI OAuth accounts in the same source tier equally except for remaining capacity. This makes a lower configured priority such as `50` ineffective for biasing traffic toward a preferred OpenAI account.

## What Changes

- Apply configured account priority as a multiplicative weight in capacity-weighted account selection.
- Preserve provider/source-tier hard filtering so lower-priority accounts are biased, not exclusively selected.
- Keep peers eligible so a priority `50` OpenAI account receives more traffic than priority `100` peers without starving them.

## Impact

- A priority `50` account gets roughly double the capacity weight of an otherwise identical priority `100` account.
- With current observed usage where `2908709191@qq.com` also has more remaining weekly capacity, expected selection bias is about 5-7 out of 10 requests.

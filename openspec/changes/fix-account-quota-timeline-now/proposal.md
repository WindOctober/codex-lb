## Why

The account quota timeline can appear stale near the right edge because the backend rounded the seven-day / 5h bucket count down. The chart also relied on the browser runtime timezone for labels, which can show UTC dates even when operators are reasoning in the local Asia/Taipei operating timezone.

## What Changes

- Compute quota timeline bucket count from the aligned timeline start through the current time using ceiling arithmetic.
- Merge reset-preceding primary-window tail buckets shorter than one hour into the previous bucket.
- Format the account quota timeline tick and tooltip timestamps in Asia/Taipei.
- Add focused test coverage for the bucket-count calculation and short-tail merge behavior.

## Impact

- No durable schema changes.
- Existing timeline response fields stay unchanged.
- The quota timeline may return one extra partial bucket at the right edge so the chart covers the current interval.
- Very short reset-preceding primary-window tail buckets are no longer rendered as standalone bars.

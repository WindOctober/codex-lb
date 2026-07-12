## Why

Internal automation needs a small read-only account status API that lists accounts with current occupancy and quota state so callers can choose an account using their own policy. The existing dashboard account list contains most quota data, but it does not provide a focused account quota list with runtime occupancy folded in.

## What Changes

- Add dashboard-authenticated account quota endpoints under `/api/accounts`.
- Return focused account quota payloads with 5h and weekly remaining quota, reset time, window duration, and bridge runtime occupancy.
- Treat active HTTP bridge sessions, pending requests, queued requests, or busy sessions as account occupancy and expose that as `runtime.occupied` for each account.

## Impact

- No durable schema changes.
- No proxy routing behavior changes.
- Runtime occupancy is process-local because HTTP bridge sessions are in memory.

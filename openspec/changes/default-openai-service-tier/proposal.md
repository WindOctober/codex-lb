## Why

Recent high-concurrency runs showed a strong reliability split between `service_tier=default` and `service_tier=auto`: default-tier requests completed while auto-tier requests timed out or ended incomplete. The proxy should avoid forwarding `auto` by default so Project-level or client-default tier selection cannot silently route workload onto an unstable tier.

## What Changes

- Normalize missing `service_tier` values to `default` for Responses and compact Responses payloads.
- Normalize explicit `service_tier=auto` to `default` before upstream forwarding, reservation accounting, and request logging.
- Preserve explicit `priority`, `flex`, and the existing `fast` alias to `priority`.
- Normalize API-key enforced `auto` service tiers to `default` and remove Auto from the dashboard selector.

## Impact

Clients that omit `service_tier` or send `auto` will use standard default-tier processing. Clients that explicitly request `priority` or `flex` continue to receive those tiers.

## Context

The upstream reset-credit bank response includes internal credit IDs plus display metadata such as `title`, `granted_at`, and `expires_at`. codex-lb currently parses only ID, status, and reset type, then exposes only the aggregate count to the dashboard. The existing account-detail reset panel already owns loading, error, refresh, and consume interactions.

## Goals / Non-Goals

**Goals:**

- Preserve upstream timestamps as typed datetimes and expose them as ISO 8601 dashboard fields.
- Return only display-safe metadata for currently available credits.
- Present every known deadline in a compact, earliest-first account detail list.
- Keep older or incomplete upstream payloads compatible.

**Non-Goals:**

- Persist reset-credit metadata in the local database.
- Expose upstream credit IDs or allow selecting a specific credit for consumption.
- Change reset consumption, polling cadence, or account-list routing behavior.

## Decisions

1. Extend the internal upstream Pydantic payload with optional `title`, `granted_at`, and `expires_at` fields. Optional fields preserve compatibility with historical fixtures and upstream responses that omit metadata while retaining strict datetime parsing when values are present.
2. Add a separate dashboard credit schema without `id` or `status`. The service maps only `available_credits`, sorts known expiries first in ascending order, and leaves the upstream `available_count` authoritative for compatibility.
3. Extend the existing reset-credit endpoint instead of adding another request. Account detail already fetches this resource, so the UI gains deadlines without extra latency or a second failure mode.
4. Render exact browser-local date/time plus a coarse remaining-time label. Exact ISO values remain available through semantic `<time>` elements, while urgency styling helps scan the nearest deadline. Credits with no expiry remain visible as `Deadline unavailable` after all dated credits.

## Risks / Trade-offs

- [Upstream count and detailed available-credit rows can temporarily disagree] -> Continue showing the authoritative count and render every detailed row received without inventing missing credits.
- [Client clock or timezone can make relative wording differ from server time] -> Treat the exact ISO deadline as the contract and label it in the browser's local timezone.
- [New upstream fields may be absent] -> Keep metadata optional end-to-end and provide an explicit unavailable state.

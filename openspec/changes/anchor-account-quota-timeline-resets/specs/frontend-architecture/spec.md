## ADDED Requirements

### Requirement: Account quota timeline anchors 5h bars at primary resets

The Accounts quota timeline MUST anchor 5h usage bars to the point where primary quota returns to full. When primary usage history indicates a reset, subsequent 5h buckets MUST start from that reset anchor instead of continuing on a fixed epoch-aligned grid. If `reset_at` and `window_minutes` are available, the reset anchor MUST be calculated as `reset_at - window_minutes`; otherwise, the implementation MAY use the first post-reset sample time.

#### Scenario: Reset inside a fixed grid bucket starts a new 5h bar

- **GIVEN** an account has primary usage history where used percent drops from a higher value to a lower value
- **AND** the post-reset history row includes `reset_at` and `window_minutes`
- **WHEN** the account quota timeline is built
- **THEN** the next 5h usage bar starts at `reset_at - window_minutes`.

#### Scenario: Weekly line follows dynamic buckets

- **GIVEN** account quota timeline buckets have been re-anchored by a primary reset
- **WHEN** weekly remaining points are added to the timeline
- **THEN** weekly remaining values and 7d reset markers are assigned to the dynamic bucket windows.

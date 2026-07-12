## 1. HTTP bridge local concurrency

- [x] 1.1 Change the default HTTP bridge queue limit to unlimited while retaining positive-value enforcement.
- [x] 1.2 Remove the HTTP bridge per-session `response.create` serialization gate.
- [x] 1.3 Add regression coverage for unlimited queue admission and concurrent submit behavior.
- [x] 1.4 Validate targeted tests and an isolated runtime smoke test.
- [x] 1.5 Shard fresh soft prompt-cache bridge requests across multiple bridge sessions under pending load.
- [x] 1.6 Preserve busy hard-continuity bridge sessions when a conflicting recreate arrives.
- [x] 1.7 Align Helm defaults so deployments do not re-enable the legacy per-session queue cap.
- [x] 1.8 Treat connect-phase upstream websocket `403` as failover-eligible and disable Codex bridge prewarm by default in Helm.
- [x] 1.9 Route busy hard-continuity recreates onto isolated parallel bridge keys when capacity is available.
- [x] 1.10 Route fresh prompt-cache traffic around busy shards that were promoted to Codex continuity sessions.
- [x] 1.11 Route busy promoted prompt-cache continuity recreates onto isolated parallel bridge keys when capacity is available.
- [x] 1.12 Treat previous-response and turn-state aliases as prompt-cache continuity evidence for busy parallel routing.
- [x] 1.13 Disable the local bridge session pool cap by default while preserving explicit positive caps.
- [x] 1.14 Reject stale or replaced HTTP bridge sessions before upstream submit.
- [x] 1.15 Reclaim selected-account idle bridge sessions when account/model session slots are full.
- [x] 1.16 Reselect another eligible HTTP bridge account when the selected account's connect slot is full.
- [x] 1.17 Avoid the just-disconnected account first when transparently replaying precreated HTTP bridge requests.

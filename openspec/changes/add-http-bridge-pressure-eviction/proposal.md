# add-http-bridge-pressure-eviction

## Why

Large Codex batch runs can create many one-shot soft `prompt_cache` HTTP bridge sessions. Keeping those idle bridges for the full prompt-cache TTL preserves locality for repeated work, but it also keeps local and upstream websocket sessions open after the batch request has completed.

## What Changes

- Add batch-aware HTTP bridge pressure eviction for idle soft `prompt_cache` Codex bridge sessions.
- Trigger eviction when the bridge pool reaches a configurable pressure threshold.
- Prefer evicting sessions from large creation-time batches while preserving busy and hard-continuity sessions.

## Impact

- Reduces retained websocket sessions during high-concurrency batch workloads.
- Keeps continuity-sensitive hard sessions out of the pressure eviction path.

## 1. Request-Submit Decomposition

- [x] 1.1 Add a typed request-submit mixin and service capability protocol.
- [x] 1.2 Extract response-create gate release, submit interruption cleanup, and request detachment.
- [x] 1.2.a Canonicalize affinity, model-class, and turn-state helpers required by submit and reconnect.
- [x] 1.3 Extract bridge prewarm and primary request submission.
- [x] 1.4 Extract fresh-upstream replay, pre-created retry, and terminal failure retry.
- [x] 1.5 Extract bridge reconnect after its helper dependencies have canonical ownership.

## 2. Verification

- [x] 2.1 Add focused cleanup, detach, inheritance, and compatibility tests.
- [x] 2.2 Run HTTP bridge, streaming, websocket, and durable continuity regressions.
- [x] 2.3 Run formatting, lint, compilation, architecture, and diff checks.
- [x] 2.4 Validate on an isolated backend before restarting the primary backend.
- [x] 2.5 Validate OpenSpec artifacts if the CLI is available.

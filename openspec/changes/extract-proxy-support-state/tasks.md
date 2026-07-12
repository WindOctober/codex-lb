## 1. Shared State Extraction

- [x] 1.1 Move affinity, stream settlement, request, session, and transport-control state to `_service/support.py`.
- [x] 1.2 Move directly associated pure helpers and constants with their owning state.
- [x] 1.3 Preserve service-module compatibility exports.
- [x] 1.4 Extract pure HTTP bridge key, alias, shard, and family operations.

## 2. Verification

- [x] 2.1 Add focused identity, default, and affinity-strength tests.
- [x] 2.2 Run HTTP bridge, websocket, streaming, and proxy utility regressions.
- [x] 2.3 Run formatting, lint, compilation, and diff checks.
- [x] 2.4 Validate on an isolated backend before restarting the primary backend.
- [x] 2.5 Validate OpenSpec artifacts if the CLI is available.

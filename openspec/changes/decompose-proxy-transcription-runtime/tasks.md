## 1. Transcription Runtime Extraction

- [x] 1.1 Add a typed transcription runtime mixin and explicit service capability protocol.
- [x] 1.2 Move selection, freshness, upstream invocation, 401 retry, health handling, and terminal logging without semantic changes.
- [x] 1.3 Add a thin upstream-client compatibility capability and reuse existing settings/budget capabilities.
- [x] 1.4 Inherit the mixin from `ProxyService`, remove the local method body, and remove only proven-unused imports.
- [x] 1.5 Ratchet architecture ownership and dependency-direction tests.

## 2. Verification

- [x] 2.1 Run focused success, selection failure, refresh failure, 401 retry, budget exhaustion, and generic failure tests.
- [x] 2.2 Run transcription API and upstream client contract tests.
- [x] 2.3 Run formatting, lint, compilation, type-boundary, architecture, and diff checks.
- [x] 2.4 Validate the OpenSpec change and main specs.
- [x] 2.5 Validate on an isolated backend without restarting, draining, or retargeting the primary backend or Caddy.

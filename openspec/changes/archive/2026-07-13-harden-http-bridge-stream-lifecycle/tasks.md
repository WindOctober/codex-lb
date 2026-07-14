## 1. Session acquisition lifecycle

- [x] 1.1 Replace shield-wrapped keepalive polling with an owned task polling helper.
- [x] 1.2 Convert post-keepalive startup failures into terminal Responses SSE events while preserving pre-commit HTTP errors.
- [x] 1.3 Add tests for startup success, post-keepalive failure, abandonment, and exception retrieval.

## 2. Upstream reader ownership

- [x] 2.1 Keep the current reader as sole owner when it initiates a reconnect.
- [x] 2.2 Preserve external-reader cancellation and replacement semantics.
- [x] 2.3 Add regression tests that prove one receive owner across both reconnect paths.

## 3. Verification and deployment

- [x] 3.1 Run focused bridge tests, formatting, lint, and OpenSpec validation.
- [x] 3.2 Validate health and changed behavior on an isolated backend port.
- [x] 3.3 Restart only backend port 2456 and confirm production health and clean fresh logs.

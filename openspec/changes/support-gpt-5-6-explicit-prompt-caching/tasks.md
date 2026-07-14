## 1. Request Contract

- [x] 1.1 Add typed `prompt_cache_options` and `prompt_cache_breakpoint` value contracts with GPT-5.6 model gating.
- [x] 1.2 Preserve valid cache policy and breakpoint fields across canonical Responses, `/v1/responses`, WebSocket, and Compact normalization without mapping to `prompt_cache_retention`.
- [x] 1.3 Add unit tests for valid implicit/explicit policy, explicit-without-breakpoint, invalid modes/TTL/locations, older-model isolation, and exact nested serialization.
- [x] 1.4 Preserve breakpoints on routable `input_file` forms while retaining the project-scoped `file_id` rejection until an upload-owner mapping exists.
- [x] 1.5 Evaluate prompt-cache model compatibility after API-key model enforcement so an older requested model may validly route to an enforced GPT-5.6 model while the reverse route remains rejected.

## 2. Affinity and Provider Capability

- [x] 2.1 Namespace internal prompt-cache affinity by tenant, normalized request model, and client key while preserving the raw client key on wire.
- [x] 2.2 Require native Responses provider capability before sticky selection and through Direct, Compact, WebSocket reuse, and HTTP bridge reconnect.
- [x] 2.3 Support native provider `/v1/responses/compact` and add mixed-pool/no-capability/reuse regression coverage.
- [x] 2.4 Derive alpha-search prompt-cache affinity from the same tenant, normalized model, and raw request ID namespace as its parent Responses turn while allowing provider capability to supersede locality.

## 3. Usage, Pricing, and Persistence

- [x] 3.1 Type and preserve `cache_write_tokens` in SSE/JSON, Direct/WebSocket/Compact settlement, request logs, and per-request API output.
- [x] 3.2 Add exact GPT-5.6 Sol/Terra/Luna aliases and standard/flex/priority/long-context pricing with a 25% write uplift only.
- [x] 3.3 Include worst-case write uplift in cost reservation, settle failed terminals with authoritative usage, and keep token quotas single-counted.
- [x] 3.4 Add nullable request-log migration, preserve historical `NULL`, and cover persistence/model-rewrite/account-rollup behavior.
- [x] 3.5 Aggregate authoritative usage across hidden Direct retries, price every attempt at its own model/tier, and fail-settle all-failed requests instead of releasing incurred usage.

## 4. Validation and Measurement

- [x] 4.1 Run focused and full request, pricing, migration, affinity, bridge, WebSocket, and OpenAI-compat suites plus Ruff, `ty`, and `git diff --check`.
- [x] 4.2 Validate all main OpenSpec specs and this change strictly.
- [x] 4.3 Start an isolated backend on port 3456 and run A1/A2/B1/explicit-no-breakpoint requests with cacheable prefixes, recording status, latency, `cached_tokens`, and `cache_write_tokens` against the frozen 2455 baseline.
- [x] 4.4 Stop the isolated backend explicitly and preserve a workspace-local measurement artifact without credentials.

## 5. Hardening Handoff

- [x] 5.1 Re-run the production-gating proxy audit with this change included and resolve any Critical, High, or in-scope Medium findings.
- [x] 5.2 Complete the isolated preflight, backend-only 2456 deployment, production checks, and independent completion verification without restarting Caddy.

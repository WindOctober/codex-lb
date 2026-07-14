## Context

Codex's Web Search extension uses a non-streaming `SearchClient` that posts a typed request to `alpha/search` relative to the configured Codex provider base URL. The request contains a thread/session identifier, model, recent conversation input, search commands, settings, and a token budget. The response contains `output` and optionally `encrypted_output`.

The client points at codex-lb's `/backend-api/codex` base URL. Since only Responses routes are currently registered, the search POST reaches FastAPI but is rejected with 405 before account selection or upstream forwarding.

## Goals / Non-Goals

**Goals:**

- Support the exact Codex alpha search POST contract.
- Keep the caller's LB API key separate from upstream account credentials.
- Prefer the account already associated with the Codex session ID when a live or persisted sticky binding exists.
- Refresh account credentials and fail over on account-scoped upstream failures.
- Return structured JSON or the normalized upstream error status without converting the request into a Responses call.

**Non-Goals:**

- Implement a search engine locally.
- Add a generic catch-all proxy under `/backend-api/codex`.
- Support alpha search for arbitrary OpenAI-compatible API-key providers that do not expose the Codex endpoint.
- Charge search-tool calls as additional model-token usage.

## Decisions

### Add one explicit route

The API registers only `POST /backend-api/codex/alpha/search`. This closes the observed compatibility gap without allowing clients to reach arbitrary ChatGPT backend paths.

### Preserve the official JSON envelope

The request schema validates required `id` and `model` fields while preserving forward-compatible command, settings, input, and response extensions. The upstream client posts JSON and requires a JSON object response containing non-empty `output`.

### Use the session ID as prompt-cache affinity

Account selection uses the request `id` as a `PROMPT_CACHE` sticky key because current Codex clients use the same thread identifier as the parent Responses turn's `prompt_cache_key`. This lets search reuse or update the parent turn's account binding. API-key account and group restrictions and model eligibility remain authoritative.

### Reuse bounded account failover

The search runtime refreshes credentials before calling upstream. A 401 gets one forced-refresh retry. Account-scoped rate-limit, quota, permanent-auth, and transient server failures are classified through the existing proxy error path and may move the request to one other eligible account. Client-invalid requests surface immediately and are not retried across accounts.

### Keep search independent from Responses usage settlement

Alpha search does not return model token usage and is an internal tool operation within a parent Codex turn. The route enforces LB authentication and model access but does not create a second API-key token reservation.

## Risks / Trade-offs

- [Risk] The alpha contract can evolve. -> Schemas preserve unknown JSON fields while validating the stable required identifiers and response output.
- [Risk] Search may use a different account from an active bridge if no sticky binding exists. -> The session ID is used as the canonical sticky key; routing remains correct even when continuity information is absent.
- [Risk] Retrying could duplicate a search operation. -> Search is read-only, retries are bounded, and account failover occurs only before a successful response is returned.

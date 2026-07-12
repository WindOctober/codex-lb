## Context

OAuth persistence currently derives the destination account from the newly authorized identity. That is suitable for importing and refreshing the same identity, but it cannot express an operator intent to replace credentials on a specific existing account while preserving that account's local data.

The Accounts page already has an OAuth dialog and a re-auth action, but the backend start request does not carry a target account and the OAuth state does not remember one through browser/device/manual callback completion.

## Goals / Non-Goals

**Goals:**

- Support targeted re-auth for an existing OpenAI OAuth account in any status.
- Preserve the selected account row and account-owned data while replacing OpenAI OAuth identity and token fields.
- Keep non-targeted OAuth import behavior and conflict checks unchanged.
- Expose targeted re-auth from the Accounts UI for active, paused, and deactivated OpenAI OAuth accounts.

**Non-Goals:**

- Re-auth API-key provider accounts with OAuth.
- Merge two existing local accounts automatically when the new OAuth identity already exists on another local account.
- Change dashboard authentication, token refresh scheduling, or upstream OAuth protocol details.

## Decisions

- Add `target_account_id` to OAuth start requests and store it in `OAuthState`.
  - Rationale: browser callback, manual callback, and device polling complete asynchronously and need a stable target after the initial click.
  - Alternative considered: separate `/api/accounts/{id}/reauth/start` endpoint. That adds routing duplication while still needing shared OAuth state.

- Add a repository method that updates a selected account from an incoming OpenAI OAuth account payload.
  - Rationale: targeted re-auth should explicitly choose the selected row as the data owner even when email or ChatGPT account ID changes.
  - Alternative considered: passing a target override into `upsert_openai_reauth`. Keeping a separate method avoids weakening existing import ambiguity checks.

- Reject targeted re-auth if the selected account is missing or is not an OpenAI OAuth account.
  - Rationale: OAuth re-auth cannot safely replace API-key provider credentials.

- Reject targeted re-auth if the incoming identity already belongs to another local OpenAI OAuth account.
  - Rationale: automatic cross-account consolidation would be a destructive merge decision. Existing manual merge remains the appropriate path.

## Risks / Trade-offs

- Operator can intentionally replace an account with the wrong upstream identity -> UI labels the action as re-auth for the selected account and backend requires an explicit target account ID.
- Existing account caches may keep stale identity briefly -> invalidate account selection caches after successful targeted re-auth.
- Frontend built assets can drift from source -> run the frontend build so `app/static/assets` reflects source changes.

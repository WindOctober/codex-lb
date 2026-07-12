## Why

Re-authenticating an existing ChatGPT account can currently create `__copy` account rows when duplicate imports are enabled. Operators then need to manually merge usage, logs, sticky sessions, and routing metadata even though the auth payload proves the same OpenAI account identity.

## What Changes

- Treat same normalized email plus same ChatGPT account ID as the same account during OpenAI OAuth import/re-auth.
- Collapse legacy `__copy` rows for that same identity into one canonical account and keep the newest auth status/tokens.
- Reject same-email imports with a different ChatGPT account ID so operators can decide whether a manual merge is safe.

## Impact

- Code: `app/modules/accounts/repository.py`, `app/modules/accounts/service.py`, `app/modules/oauth/service.py`
- Tests: account import API and OAuth flow integration coverage
- Specs: new `account-import-lifecycle` capability

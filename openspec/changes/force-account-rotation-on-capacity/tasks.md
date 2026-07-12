## 1. Account Rotation

- [x] 1.1 Identify capacity and account-limit errors that require a different account.
- [x] 1.2 Pass explicit account preference into HTTP bridge fresh-upstream replay.
- [x] 1.3 Preserve the original request payload and model during replay.

## 2. Verification

- [x] 2.1 Add regression coverage for pre-created and no-text capacity account rotation.
- [x] 2.2 Verify generic transient retries retain existing behavior.
- [x] 2.3 Run targeted tests and static checks.
- [x] 2.4 Validate on an isolated backend, then restart only the primary backend and check health.
- [ ] 2.5 Run OpenSpec validation if the CLI is available. (`openspec` is not installed in PATH.)

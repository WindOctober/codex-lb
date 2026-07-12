## 1. Implementation

- [x] 1.1 Add additive port-selection mode to the Caddy switch helper
- [x] 1.2 Reorder replacement startup so the backend is ready before gateway handoff
- [x] 1.3 Keep stop/status behavior scoped to the selected managed ports
- [x] 1.4 Require explicit confirmation before Caddy start/stop/replace actions
- [x] 1.5 Require separate explicit confirmation before stopping primary 2455/2456 ports
- [x] 1.6 Add backend-only restart/stop support with port-scoped runtime files
- [x] 1.7 Update local agent safety instructions to avoid Caddy for ordinary preflight

## 2. Verification

- [x] 2.1 Run shell syntax checks for the updated script
- [ ] 2.2 Run OpenSpec validation

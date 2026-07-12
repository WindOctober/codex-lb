## Why

Local networks can intermittently lose direct access to ChatGPT while a local proxy remains available. Operators currently need to restart or reconfigure codex-lb to move upstream traffic onto the proxy, which interrupts active work and does not handle later direct recovery.

## What Changes

- Add a runtime upstream egress selector with `direct`, `proxy`, and `auto` modes.
- In `auto` mode, probe the direct ChatGPT route every 5 seconds, switch new upstream requests to proxy only after 3 consecutive direct transport failures while the proxy route is also continuously healthy, and switch new requests back to direct after 3 consecutive direct successes.
- Enforce a 60 second cooldown between egress route switches.
- Apply the selected egress route only to new upstream requests; existing streaming or WebSocket requests are not migrated mid-flight.

## Impact

- Affects upstream HTTP/SSE, WebSocket, compact, transcription, usage, reset-credit, and provider model probe clients.
- Adds runtime configuration for upstream proxy URL and egress probe thresholds.
- Adds unit coverage for the egress state machine and request proxy selection.

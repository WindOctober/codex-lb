# Make Caddy Switch Non-Disruptive

## Why

The local Caddy switch helper currently stops the process bound to the gateway port before starting the replacement backend. When long-running Codex CLI sessions are using the existing endpoint, this can interrupt active streams.

## What Changes

- make the Caddy switch helper default to an additive startup mode that chooses alternate free ports when the configured gateway/backend ports are already occupied
- keep an explicit replacement mode for operators who want to move the configured gateway port in place
- in replacement mode, start and health-check the replacement backend before stopping the gateway process

## Impact

- running the helper no longer kills the existing `codex-lb` endpoint by default
- new Caddy-backed traffic can be tested on a separate localhost port while existing Codex processes continue using their current endpoint
- intentional in-place replacement remains available when the operator chooses it

## Overview

The feature adds a small runtime state machine in `app.core.egress`. It owns only upstream egress selection and probing. Request clients ask it for an `EgressSelection` immediately before opening a new upstream connection.

## Modes

- `direct`: always use direct upstream egress.
- `proxy`: always use `CODEX_LB_UPSTREAM_PROXY_URL`; startup validation requires this URL.
- `auto`: start in direct mode, probe direct connectivity, and switch future requests to proxy only after the configured direct failure threshold is met and the proxy route has also met the recovery threshold as a continuous healthy candidate.

## Probe Behavior

The direct probe explicitly disables environment proxies. The proxy probe uses the configured proxy URL. A direct probe is considered successful when the probe URL returns any HTTP response, because the check is only proving transport reachability to the API hostname. A proxy probe requires a non-5xx response, because local proxy 5xx responses commonly represent tunnel or node failure. 401/403 responses still prove transport reachability.

Default timings:

- interval: 5 seconds
- direct failure threshold: 3
- direct recovery threshold: 3
- route switch cooldown: 60 seconds

## Request Behavior

Only new requests use the current selection. Existing SSE streams or WebSocket connections continue on the route they opened with. The proxy client does not automatically replay a request on the other egress after bytes may have reached upstream.

## Configuration

`CODEX_LB_UPSTREAM_PROXY_URL` is the canonical HTTP(S) proxy URL. The egress selector does not infer a proxy from generic `HTTP_PROXY`/`HTTPS_PROXY` variables.

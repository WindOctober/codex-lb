# Traffic Dashboard

`codex-lb` exposes a lightweight localhost-only traffic dashboard at:

```text
http://127.0.0.1:2455/__traffic
```

It combines in-process proxy request metrics with Windows collector heartbeats for SSH reverse tunnels. It is intended for dense operational monitoring, not account usage or billing analytics.

## Configuration

The project accepts the standard `CODEX_LB_*` settings and the unprefixed names used by the collector deployment:

| Setting | Default | Purpose |
| --- | --- | --- |
| `CODEX_LB_TRAFFIC_DASHBOARD_ENABLED` / `TRAFFIC_DASHBOARD_ENABLED` | `true` | Enable `/__traffic/*`. |
| `CODEX_LB_TRAFFIC_DASHBOARD_TOKEN` / `TRAFFIC_DASHBOARD_TOKEN` | empty | Optional bearer/query token for non-local access. |
| `CODEX_LB_TRAFFIC_HEARTBEAT_STALE_SECONDS` / `TRAFFIC_HEARTBEAT_STALE_SECONDS` | `90` | Mark heartbeat data stale after this age. |
| `CODEX_LB_TRAFFIC_REQUEST_RING_SIZE` / `TRAFFIC_REQUEST_RING_SIZE` | `2000` | Number of recent completed requests retained in memory. |

By default, `/__traffic/*` only accepts localhost clients. If a token is configured, clients may also send:

```http
Authorization: Bearer <token>
```

or open:

```text
http://host:2455/__traffic?token=<token>
```

The dashboard JavaScript preserves `?token=` for its follow-up API calls.

## Collector Heartbeat

The Windows collector should POST every 30 seconds:

```text
POST http://127.0.0.1:2455/__traffic/tunnel-heartbeat
```

The collector health check can use the cheap ping endpoint:

```json
{
  "healthPath": "/__traffic/ping"
}
```

Heartbeat payload shape:

```json
{
  "source": "windows-admin",
  "timestamp": "2026-05-09T18:30:00+08:00",
  "collector": {
    "pid": 1234,
    "mode": "watch",
    "config_path": "D:\\Learning\\Agent\\vpn-ssh-jump\\config\\traffic-collector.json",
    "state_dir": "C:\\Users\\Admin\\.codex\\inner-server-reverse-tunnel"
  },
  "local_backend": {
    "host": "127.0.0.1",
    "port": 2455,
    "name": "codex-lb",
    "health_path": "/__traffic/ping",
    "healthy": true,
    "latency_ms": 42,
    "http_status": 204,
    "established_connections": 12,
    "process": {
      "pid": 35964,
      "name": "Code",
      "cpu_seconds": 23.469,
      "working_set_mb": 336.5
    },
    "last_error": null
  },
  "tunnels": [
    {
      "remote_host": "inner-server",
      "remote_port": 32456,
      "name": "batch-1",
      "role": "batch",
      "owner": "batch worker pool",
      "configured": true,
      "enabled": true,
      "used": true,
      "local_target": "127.0.0.1:2455",
      "healthy": true,
      "latency_ms": 120,
      "http_status": 204,
      "ssh_pid": 45304,
      "watchdog_pid": 55796,
      "established_connections": 3,
      "last_error": null
    }
  ]
}
```

`source` is the cache key. Only the latest heartbeat per source is kept in memory. Configured but unused tunnel ports remain visible in `/__traffic/ports` and are greyed in the dashboard.

## API

```text
GET  /__traffic/ping
POST /__traffic/tunnel-heartbeat
GET  /__traffic/summary
GET  /__traffic/requests?limit=200
GET  /__traffic/ports
GET  /__traffic
```

Request metrics include method, path, host, inferred remote port, client IP, status, error fields, request/response byte counts, streaming status, active/completed/aborted/errored state, first-byte latency, duration, and parsed `model` when the request JSON body is small enough to inspect safely.
Only proxy paths are recorded (`/backend-api/*`, `/v1/*`, `/internal/bridge/*`, and `/api/codex/usage`) so dashboard, health, and traffic API polling do not pollute the port view.

Port inference priority is:

1. `Host` header port, for example `127.0.0.1:32455`.
2. `X-Forwarded-Host` port.
3. `X-Codex-LB-Port`.
4. `unknown`.

Streaming responses are not buffered. The middleware only counts chunks while forwarding them.

## Port Naming And Sharding

Use `role` to distinguish interactive/UI tunnels from batch tunnels:

```json
{ "remote_port": 32455, "name": "ui", "role": "interactive", "owner": "VS Code / ChatGPT UI" }
{ "remote_port": 32456, "name": "batch-1", "role": "batch", "owner": "worker pool A" }
{ "remote_port": 32457, "name": "batch-2", "role": "batch", "owner": "worker pool B" }
```

Point batch workers at different reverse-tunnel ports so the dashboard can show whether traffic is actually spread:

```text
http://127.0.0.1:32456/backend-api/codex
http://127.0.0.1:32457/backend-api/codex
```

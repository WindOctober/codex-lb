## ADDED Requirements

### Requirement: Local Caddy switch avoids disrupting active endpoints by default

The local Caddy switch helper MUST refuse gateway-changing `start`, `stop`, and `replace` actions by default unless the operator explicitly confirms Caddy gateway changes.
The helper MUST avoid stopping a process that owns the primary gateway or backend port unless the operator also explicitly confirms primary-port stopping.

#### Scenario: Caddy switch start is run without gateway confirmation

- **WHEN** the operator starts the Caddy switch helper without gateway-change confirmation
- **THEN** the helper exits without starting or stopping Caddy
- **AND** the helper recommends backend-only validation using `restart-codex-lb.sh`

#### Scenario: Caddy switch stop targets primary ports without primary-stop confirmation

- **WHEN** the operator stops the Caddy switch helper with gateway-change confirmation
- **AND** the selected gateway/backend ports include the primary `2455` or `2456` port
- **AND** primary-stop confirmation is absent
- **THEN** the helper exits without stopping the primary Caddy gateway or backend

#### Scenario: Configured gateway port is occupied during additive startup

- **WHEN** the operator starts the Caddy switch helper in additive mode with gateway-change confirmation
- **AND** the configured gateway port is already listening
- **THEN** the helper selects a free alternate gateway port
- **AND** it starts a Caddy-backed stack on that alternate port
- **AND** it does not stop the process listening on the originally configured gateway port

#### Scenario: In-place replacement prepares the backend before gateway handoff

- **WHEN** the operator starts the Caddy switch helper in replacement mode with gateway-change confirmation
- **AND** the configured gateway port is occupied by a managed `codex-lb` process
- **THEN** the helper starts and health-checks the replacement backend first
- **AND** only then stops the gateway-port process and starts Caddy on the configured gateway port

### Requirement: Local backend restart helper supports backend-only validation

The local backend restart helper MUST support starting and stopping a codex-lb backend on a selected port without starting, stopping, or retargeting Caddy.
When no PID or log file override is provided, the helper MUST use port-scoped runtime files so isolated backend validation does not overwrite primary runtime files.

#### Scenario: operator validates an isolated backend

- **WHEN** the operator runs the backend restart helper with a non-primary `CODEX_LB_PORT`
- **THEN** the helper starts codex-lb directly on that backend port
- **AND** the helper writes port-scoped pid and log files
- **AND** the helper does not stop primary backend service managers for non-primary ports
- **AND** Caddy remains untouched

#### Scenario: operator stops an isolated backend

- **WHEN** the operator runs the backend restart helper with `--stop-only`
- **THEN** the helper stops only codex-lb processes listening on the selected backend port
- **AND** the helper does not run Caddy switch logic

### Requirement: Local Caddy gateway accepts forwarded dashboard hosts

The local Caddy gateway MUST route requests by the configured listener port rather than requiring the HTTP `Host` header to match `localhost` or `127.0.0.1`.

#### Scenario: VS Code port forwarding uses an external host

- **WHEN** a request reaches the local Caddy gateway on the configured port
- **AND** the request `Host` header is a forwarded external host
- **THEN** Caddy proxies the request to the configured codex-lb backend

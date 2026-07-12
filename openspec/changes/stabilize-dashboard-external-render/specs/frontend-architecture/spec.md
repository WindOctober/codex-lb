## ADDED Requirements

### Requirement: Dashboard render failures

The dashboard frontend SHALL present a visible recovery surface when client startup or React rendering fails before the normal route content is shown.

#### Scenario: Startup storage APIs are unavailable

- **GIVEN** browser storage APIs throw during theme initialization
- **WHEN** a user opens the dashboard
- **THEN** the frontend continues startup using the default theme preference

#### Scenario: Route rendering fails

- **GIVEN** a client-side render error occurs inside the dashboard app
- **WHEN** the error reaches the app boundary
- **THEN** the page shows a recovery surface instead of an empty background

### Requirement: Dashboard static caching

The dashboard server SHALL return cache headers that prevent stale SPA HTML while allowing immutable hashed assets to be cached.

#### Scenario: Serve SPA HTML

- **WHEN** the dashboard server returns `index.html`
- **THEN** the response includes a header that prevents caching

#### Scenario: Serve hashed assets

- **WHEN** the dashboard server returns a file under `/assets/`
- **THEN** the response marks it as immutable and long-lived

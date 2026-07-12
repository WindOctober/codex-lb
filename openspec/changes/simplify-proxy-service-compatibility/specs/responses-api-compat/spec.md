## ADDED Requirements

### Requirement: Proxy compatibility adapters preserve required request behavior

The proxy MUST use one shared optional-keyword compatibility policy when invoking callables whose optional parameters may differ across supported implementations or test doubles. Required arguments MUST always be forwarded. Unsupported optional arguments MAY be omitted only when callable signature inspection proves they are unsupported.

#### Scenario: Callable supports optional arguments

- **GIVEN** a proxy dependency accepts an optional argument
- **WHEN** the proxy invokes that dependency through the compatibility adapter
- **THEN** the optional argument is forwarded

#### Scenario: Callable omits a newer optional argument

- **GIVEN** a compatible dependency or test double has an older signature
- **WHEN** the proxy invokes it with a newer optional argument
- **THEN** the unsupported optional argument is omitted
- **AND** required arguments are still forwarded

### Requirement: Cleanup preserves proxy semantics

Removing unreferenced legacy helpers MUST NOT change account selection, model selection, bridge pressure eviction, concurrency admission, durable continuity, streaming events, or error contracts.

#### Scenario: Capacity retry remains account-based

- **GIVEN** a capacity failure occurs before meaningful downstream output
- **WHEN** the HTTP bridge retries
- **THEN** the requested model remains unchanged
- **AND** account rotation behavior remains governed by the classified upstream failure

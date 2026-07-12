## ADDED Requirements

### Requirement: Runtime observability contracts have a focused module boundary

HTTP bridge runtime snapshot data contracts and pure health classification MUST be owned by a focused internal observability module rather than the proxy orchestration service.

#### Scenario: Service compatibility import

- **GIVEN** existing code imports a runtime snapshot type from `app.modules.proxy.service`
- **WHEN** the runtime contracts are decomposed
- **THEN** the existing import continues to resolve
- **AND** it resolves to the canonical extracted contract

### Requirement: Runtime decomposition preserves observable behavior

The decomposition MUST preserve dashboard response fields, health classification thresholds, egress status values, session counts, capacity values, and request status semantics.

#### Scenario: Runtime snapshot after decomposition

- **GIVEN** an unchanged set of live HTTP bridge sessions and health samples
- **WHEN** a runtime snapshot is requested after decomposition
- **THEN** its serialized values are equivalent to those produced before decomposition

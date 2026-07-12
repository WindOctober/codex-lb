## ADDED Requirements

### Requirement: Capacity-weighted routing biases by configured account priority

When capacity-weighted account selection evaluates candidates in the same group and source tier, the proxy MUST apply the configured account priority as a weighting factor rather than using it to exclude otherwise eligible peer accounts. Lower configured priority values MUST increase the candidate's effective selection weight.

#### Scenario: Lower configured priority increases selection share

- **WHEN** one eligible account has configured priority `50`
- **AND** three otherwise equivalent eligible peer accounts have configured priority `100`
- **THEN** capacity-weighted routing selects the priority `50` account more often than any individual priority `100` peer
- **AND** the priority `100` peers remain eligible for selection

#### Scenario: Priority combines with remaining capacity

- **WHEN** a priority `50` account also has more remaining secondary capacity than priority `100` peers
- **THEN** capacity-weighted routing combines the priority weight and remaining-capacity weight when selecting the account

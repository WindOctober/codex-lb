## ADDED Requirements

### Requirement: Astra introduction announcement
The Astra bootstrap entry SHALL include a nonempty `availability_nux.message` matching the introduction bundled with the installed Codex extension. The Codex model endpoint SHALL retain this announcement object. Client catalogs used with the API-key provider SHALL carry the same object without changing the model identifier or default selection. The client SHALL retain its normal announcement dismissal and eligibility behavior.

#### Scenario: Astra introduction metadata is available
- **WHEN** an authorized client requests the bootstrap Codex model catalog
- **THEN** the Astra entry contains an availability_nux object with a nonempty message introducing GPT-6
- **AND** the client's model/list result can expose that object as availabilityNux

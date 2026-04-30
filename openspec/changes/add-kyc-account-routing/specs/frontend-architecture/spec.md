## ADDED Requirements

### Requirement: KYC routing controls

The Accounts page SHALL expose a per-account KYC routing toggle, and the Settings page SHALL expose a runtime KYC routing enforcement toggle. API key create and edit dialogs SHALL expose a KYC-only toggle. The created-key dialog SHALL continue to provide copy support for the generated key.

#### Scenario: Mark KYC account

- **WHEN** admin toggles KYC on an account and saves
- **THEN** the app calls `PATCH /api/accounts/{id}` with `kycEnabled`

#### Scenario: Create KYC-only API key

- **WHEN** admin enables the KYC-only toggle in the API key create dialog
- **THEN** the app calls `POST /api/api-keys` with `kycOnly: true`

#### Scenario: Toggle KYC enforcement

- **WHEN** admin toggles KYC routing enforcement in Settings
- **THEN** the app calls `PUT /api/settings` with `kycRoutingEnforcementEnabled`
- **AND** the change takes effect without restarting the backend

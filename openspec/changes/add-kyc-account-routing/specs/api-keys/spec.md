## ADDED Requirements

### Requirement: KYC-only API keys

The system SHALL allow an admin to mark an API key as `kyc_only`. A KYC-only key SHALL route only accounts that are currently marked as KYC when KYC routing enforcement is enabled.

#### Scenario: Create KYC-only key

- **WHEN** admin submits `POST /api/api-keys` with `{ "name": "kyc", "kycOnly": true }`
- **THEN** the created key response includes `kycOnly: true`
- **AND** the plain key is returned exactly once as with other API keys

#### Scenario: Update KYC-only flag

- **WHEN** admin submits `PATCH /api/api-keys/{id}` with `{ "kycOnly": true }`
- **THEN** subsequent proxy requests using that key are restricted to KYC accounts

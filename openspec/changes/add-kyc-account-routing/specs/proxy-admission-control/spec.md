## ADDED Requirements

### Requirement: KYC account routing

The proxy SHALL maintain a per-account KYC flag and a runtime dashboard toggle for KYC routing enforcement. When enforcement is enabled, KYC accounts MUST be excluded unless the authenticated API key is marked `kyc_only`, and requests authenticated with a `kyc_only` API key MUST only select KYC accounts. Existing per-key allowed-model, enforced-model, and assigned-account restrictions SHALL still apply.

When enforcement is disabled, account KYC flags SHALL NOT affect account selection. Existing per-key allowed-model, enforced-model, and assigned-account restrictions SHALL continue to apply.

#### Scenario: Non-KYC key cannot select KYC account

- **WHEN** account `acc_kyc` is marked KYC
- **AND** a normal API key makes a proxy request
- **THEN** account selection excludes `acc_kyc`

#### Scenario: KYC-only key selects only KYC accounts

- **WHEN** account `acc_kyc` is marked KYC
- **AND** account `acc_regular` is not marked KYC
- **AND** a KYC-only API key makes a proxy request
- **THEN** account selection excludes `acc_regular`
- **AND** account selection may select `acc_kyc` subject to other limits

#### Scenario: KYC enforcement disabled restores normal account routing

- **WHEN** account `acc_kyc` is marked KYC
- **AND** KYC routing enforcement is disabled in Settings
- **THEN** normal and KYC-only API keys may select any eligible account subject to other key restrictions

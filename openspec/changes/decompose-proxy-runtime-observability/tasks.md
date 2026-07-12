## 1. Runtime Contract Extraction

- [x] 1.1 Move immutable HTTP bridge runtime snapshot contracts into a focused internal module.
- [x] 1.2 Move pure runtime health classification into the same module.
- [x] 1.3 Preserve service-module compatibility exports.
- [x] 1.4 Extract pure session aggregation, grouping, ordering, and snapshot construction.
- [x] 1.5 Extract account/model capacity mathematics while retaining repository access in the service layer.

## 2. Verification

- [x] 2.1 Add focused contract and health-threshold tests.
- [x] 2.2 Run runtime dashboard and proxy regression tests.
- [x] 2.3 Run formatting, lint, compilation, and diff checks.
- [x] 2.4 Validate on an isolated backend before restarting the primary backend.
- [x] 2.5 Validate OpenSpec artifacts if the CLI is available.

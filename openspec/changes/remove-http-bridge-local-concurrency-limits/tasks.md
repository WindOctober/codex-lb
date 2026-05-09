## 1. HTTP bridge local concurrency

- [x] 1.1 Change the default HTTP bridge queue limit to unlimited while retaining positive-value enforcement.
- [x] 1.2 Remove the HTTP bridge per-session `response.create` serialization gate.
- [x] 1.3 Add regression coverage for unlimited queue admission and concurrent submit behavior.
- [x] 1.4 Validate targeted tests and an isolated runtime smoke test.
- [x] 1.5 Shard fresh soft prompt-cache bridge requests across multiple bridge sessions under pending load.

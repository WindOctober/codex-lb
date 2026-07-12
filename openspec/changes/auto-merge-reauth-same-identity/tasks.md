## 1. Spec

- [x] 1.1 Add re-auth same-identity merge requirements

## 2. Implementation

- [x] 2.1 Add OpenAI OAuth import path that merges only same email plus same ChatGPT account ID
- [x] 2.2 Collapse legacy same-identity duplicate account rows during re-auth
- [x] 2.3 Preserve local routing metadata while letting the new auth payload own status and tokens
- [x] 2.4 Route browser/device OAuth persistence through the same re-auth merge path

## 3. Tests

- [x] 3.1 Cover same-identity re-auth with duplicate-import mode enabled
- [x] 3.2 Cover same-email different-identity conflict
- [x] 3.3 Cover legacy `__copy` collapse
- [x] 3.4 Run targeted tests and `openspec validate --specs`

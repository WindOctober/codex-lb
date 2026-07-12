# allow-omitted-responses-instructions

## Why
Newer Codex clients can omit the optional `instructions` field, especially on the Responses Lite path. The internal request schema currently rejects those valid requests before they reach the upstream service.

## What Changes
- Accept omitted `instructions` on Codex Responses and Responses Compact requests.
- Normalize an omitted value to an empty string so downstream forwarding keeps a stable string field.
- Preserve the existing requirement that `input` is present.

## Impact
- VS Code Codex clients can use models such as `gpt-5.6-sol` without receiving a local `Invalid request payload` error.
- Requests that already send instructions are unchanged.

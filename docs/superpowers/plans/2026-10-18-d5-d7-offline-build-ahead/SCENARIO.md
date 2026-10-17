# Scenario: D5/D7 Offline Build-ahead

## Coverage map

- D1 Auth and session lifecycle: production profile must already be authenticated and its Google identity must match explicit configuration; this wrapper never starts login.
- D6 Data layer: consume a timestamped, schema-shaped catalog snapshot and reject duplicate or ambiguous current approvals.
- D7 Error propagation: report stable reason codes only; do not expose raw browser, network, or storage exception text.
- D8 Cross-platform paths: runtime roots must be absolute, distinct, and disjoint before cleanup is allowed.
- D10 Headed browser: production F5 uses the repository Chrome/Xvfb profile path and remains non-submitting.
- D11 Input validation: reject TEST profiles/accounts, malformed IDs/hashes, stale snapshots, and duplicate idempotency keys with mismatched request digests.
- D12 Observability: bounded status output must not include email, cookie, token, profile path, prompt text, or exception contents.
- D13 MCP surface parity: no gflow CLI or MCP command is added; this is an operator-only offline wrapper and app-layer transaction contract.

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D1 Auth | Production auth profile is absent, unauthenticated, or belongs to another Google identity | Critical | Return HOLD before opening or mutating the Flow UI; never initiate login | Unit/Integration |
| 2 | D6 Data | Snapshot is stale, has no PROD provider account/project, has multiple active approvals, or prompt values differ from fresh Phase E | Critical | Refuse presubmit before browser launch with a stable reason code | Unit |
| 3 | D10 Browser | F5 reaches the verified PROD account/project and Agent-only surface | High | Use repository-owned profile resolution and F5 preparation only; never expose Generate submission API | Integration |
| 4 | D7 Transactions | Same idempotency key and canonical request digest is replayed | Critical | Resolve the existing Production Job and existing terminal/in-flight Scene Job; do not allocate a second attempt | Unit |
| 5 | D7 Transactions | Same idempotency key is reused with a different canonical digest | Critical | Reject as an idempotency conflict; create no rows | Unit |
| 6 | D7 Lifecycle | A transition skips states, mutates a terminal state, or marks COMPLETED without exact provider attribution and remote verification | Critical | Reject transition; retain the last valid state | Unit |
| 7 | D7 Cleanup | TEMP_ROOT overlaps RUNTIME_STATE_ROOT or a protected browser/recovery directory | Critical | Reject configuration and cleanup before filesystem mutation | Unit |
| 8 | D7 Storage | Upload succeeds but independent remote readback/hash verification fails | Critical | Keep local media; mark HOLD/verification failure; never set Remote_Verified or COMPLETED | Integration |
| 9 | D7 Cleanup | Cleanup occurs before remote verification or cleanup fails | High | Retain evidence and never report a clean completed lifecycle | Integration |
| 10 | D12 Reporting | Browser, filesystem, or provider adapter raises an exception containing sensitive paths or session data | High | Emit only a stable code and bounded generic detail | Unit |

## Must-cover before merge (Critical + High)

1. Production identity, environment, project, and approved-prompt mismatches stop before browser launch.
2. Repeated idempotency requests cannot create a second job or attempt; conflicting digests fail closed.
3. Scene Job state changes are validated against the current schema and terminal states are immutable.
4. COMPLETED requires exact output identity, validated media, persistent URI/file ID, successful independent remote verification, and completed local cleanup.
5. Runtime roots are pairwise disjoint and cleanup cannot resolve into browser-state or recovery roots.
6. Operator output never includes secrets, prompt bodies, account email, or raw exception text.

## Deferred

- Live Google Sheets transaction adapter and its atomic compare-and-set semantics require a separately reviewed persistence contract.
- Live Google Drive upload/retrieval requires a legitimate production auth integration.
- D5 live presubmit awaits D4 production authentication and verified project/account/profile records.
- D6/D7 live generation is held while D2 remains UNKNOWN_SUBMISSION_STATE and D4-D6 prerequisites are not PASS.

## Suggested BDD scenarios

No browser-only BDD case is added in this offline implementation; these are pure validation and transaction contracts. Existing F5 browser coverage remains unchanged.

## Known-issues cross-reference

- Phase F5 Agent-only scope and D2/D3/D4/D5/D6/D7 ordering: PHASE_F5_AGENT_ONLY.md.
- No production Sheets/Drive persistence adapter exists; offline implementation must not claim live transaction readiness.

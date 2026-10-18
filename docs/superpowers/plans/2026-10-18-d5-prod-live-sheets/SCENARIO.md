# Scenario: D5 PROD Identity and Live Google Sheets Source

## Coverage map

- **D1 Auth/session:** an existing profile directory does not prove a valid Flow session; require the repository status verifier.
- **D6 Data:** exact 34-table schema/header set, malformed rows, duplicate keys, orphan FKs, and semantic defects must fail before Phase E.
- **D7 Errors:** auth/API failures use stable safe codes; exception bodies and credentials never appear in output.
- **D8 Paths:** credentials and snapshots remain outside Git under runtime-controlled paths; enforce symlink and POSIX permission rules.
- **D12 Observability:** do not log OAuth tokens, session data, raw API exceptions, or full prompt rows.
- **D13 MCP:** no CLI/MCP capability is added; this remains an operator-only D5 script and internal read boundary.

Selector drift and generation are out of scope. D5 remains non-generating; D6 remains unstarted.

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D1 Auth/session | Profile directory exists but repo auth verification fails | Critical | Hold before Flow opens | Unit |
| 2 | D6 Data | Wrong spreadsheet requested or missing tab | Critical | Reject before read/snapshot | Unit |
| 3 | D6 Data | Header order/name changes or values exceed schema width | Critical | Reject before Phase E | Unit |
| 4 | D6 Data | Malformed row, duplicate PK, orphan FK, or semantic validation failure | Critical | Reject before Phase E | Unit |
| 5 | D6 Data | API fails partway through the required table set | High | No partial snapshot; safe failure code | Unit |
| 6 | D6 Data | D5 attempts a Sheet write | Critical | Read-only transport; no mutation request | Unit |
| 7 | D8 Paths | Snapshot escapes runtime root, uses symlink, or has broad mode | High | Refuse; successful POSIX file is atomic and mode 0600 | Unit |
| 8 | D7 Errors | API exception contains a credential or response body | Critical | Return allowlisted code only | Unit |
| 9 | D12 Provenance | Human supplies a fresh-looking timestamp | Critical | Production F5 accepts only an in-process source result with producer-owned read_at | Unit |
| 10 | D6 Prompt data | First PROD approval is made from TEST prompt values | High | Fresh PROD Phase E candidate only; TEST row unchanged | Unit + integration |

## Must-cover before merge

1. Approved spreadsheet restriction, exact headers, full validator, and all-or-nothing read.
2. Producer timestamp, atomic mode-0600 snapshot, path containment, and secret-free errors.
3. No caller-provided JSON can reach production F5.
4. TEST and PROD approvals coexist by Scene/profile without rewriting TEST.
5. Missing V2 credential holds before Flow opens.

## Deferred

- Existing F5 owns live selector checks. D2 credit reconciliation and D6 remain separate authorization gates.

## Suggested BDD scenarios

```gherkin
Feature: Trusted D5 production master-data source
  Scenario: Refuse an operator snapshot at the production entry point
    Given no configured V2 Google Workspace credential
    When an operator attempts to pass a local JSON snapshot to D5
    Then the production entry point refuses it before opening Flow

  Scenario: Reject a changed required Sheet header
    Given fake transport returns a header that differs from the local schema
    When the source assembles the V2 snapshot
    Then it returns a safe validation hold and writes no snapshot

  Scenario: Preserve independent TEST and PROD prompt approvals
    Given an approved TEST prompt and fresh PROD Phase E
    When a first PROD prompt candidate is approved
    Then both profile-scoped approvals remain current and the TEST row is unchanged
```

## D5 recovery continuation — UTC, prompt immutability, and OAuth bootstrap

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 11 | D6 Timestamp | An operator writes a value ending in ` UTCZ` or a naive local datetime | Critical | Shared UTC parser rejects it; schema validator reports a timestamp error before Phase E | Unit |
| 12 | D6 Timestamp | A timestamp producer receives a non-UTC aware datetime | High | Shared formatter normalizes to UTC and emits one terminal `Z` marker | Unit |
| 13 | D1 OAuth | OAuth callback has missing/wrong state, wrong path, duplicate callback, or provider error | Critical | Refuse token exchange and credential install; do not log callback query/code | Unit |
| 14 | D1 OAuth | Picker returns missing/extra IDs or the wrong resource type | Critical | Fail closed before credential install; require exact Master Sheet + existing output folder grants | Unit |
| 15 | D8 Paths | OAuth client JSON is inside repository, symlinked, or malformed | High | Reject before opening consent flow; never copy client config into checkout | Unit |
| 16 | D12 Secrets | Token exchange/API failure contains client secret, code, or token response | Critical | Emit only stable safe error; no token/client secret appears in logs or report | Unit |
| 17 | D6 Prompt integrity | Fresh PROD Phase E differs from previous source-context hash while prompt text remains the same | High | Append a distinct VALIDATED version with the fresh source hash; preserve the APPROVED row | Unit + live Sheet |
| 18 | D6 Prompt integrity | Approval switch is attempted after source row changed since read | Critical | Abort before any write; require exact read-before-write conflict check | Unit + live Sheet |
| 19 | D6 Prompt integrity | Exact approval switch applies to old and new rows | Critical | One atomic batch leaves exactly one APPROVED prompt per Scene/profile; TEST approval remains unchanged | Unit + live Sheet |
| 20 | D6 Historical metadata | Old row contains exact `YYYY-MM-DDTHH:MM:SS UTCZ` after it is SUPERSEDED | High | Normalize only that exact reversible pattern to the same `...Z` instant; preserve semantic columns and status | Unit + live Sheet |

OAuth bootstrap is a one-time manual-consent operator tool, not a CLI/MCP feature. Password and MFA entry remain human-only. Flow, Generate, and Phase G remain outside this operation.

### Continuation must-cover scenarios

1. One canonical UTC formatter/parser is reused by V2 timestamp writers and rejects malformed `UTCZ` and naive values.
2. OAuth uses a loopback-only callback, random state, PKCE, the sole reviewed `drive.file` scope, exact Picker IDs, and redacted errors.
3. Credentials are not installed until the spreadsheet and existing Drive output folder are verified by API.
4. The old APPROVED prompt is immutable; the new row is appended VALIDATED, freshly checked, then switched atomically with the old row.
5. Full 34-schema validation passes before deployment; missing OAuth consent keeps D5 before Flow.

### BDD additions

```gherkin
Feature: D5 recovery and dedicated Google Workspace credential
  Scenario: Reject malformed UTC timestamp metadata
    Given a populated TIMESTAMP field contains a value ending in UTCZ
    When the full V2 schema validator checks the live snapshot
    Then it reports a timestamp validation error before Phase E

  Scenario: Grant only the approved Sheet and output folder
    Given a human completes OAuth consent and the Picker returns selected IDs
    When the bootstrap validates the Master spreadsheet and existing output folder
    Then it installs a 0600 service-owned credential only if both exact IDs are readable

  Scenario: Switch a PROD prompt approval transactionally
    Given the APPROVED PROD row is unchanged and a distinct VALIDATED candidate matches fresh Phase E
    When one Sheets batch supersedes the old row and approves the candidate
    Then exactly one PROD approval remains and the TEST approval is unchanged
```

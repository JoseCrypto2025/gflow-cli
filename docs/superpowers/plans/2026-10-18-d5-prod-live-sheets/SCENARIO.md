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

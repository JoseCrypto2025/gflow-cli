# Scenario: SCENE_PROMPT_VERSION Lifecycle Persistence

## Coverage map

Active: D6 (Sheet writes and locking), D7 (typed failures and uncertain write acknowledgement), D8 (Windows/Linux lock and timestamp behavior), D12 (redacted diagnostics). D1–D5, D9–D11 have no browser, auth, generation, transport-to-Flow, concurrency hot-path, or free-form input surface. D13 is not applicable because this is an operator-only operation with no gflow CLI/MCP leaf.

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D6 | Candidate is stale relative to fresh Phase E | Critical | Reject before append or approval; old approval unchanged | Unit |
| 2 | D6 | Duplicate version or prompt ID exists | Critical | Fail closed before any write | Unit |
| 3 | D6 | Expected approved row changes after preflight | Critical | Abort on digest/status drift | Integration |
| 4 | D6 | Two status changes are sent together | Critical | One bounded batch mutation, followed by exact readback | Integration |
| 5 | D6 | Candidate append acknowledgement is lost | High | Read by stable ID; accept only exact single match, otherwise HOLD | Integration |
| 6 | D7 | Approval batch acknowledgement/readback is uncertain | Critical | Stop; no blind retry; return safe conflict without prompt text | Integration |
| 7 | D7 | Fresh Phase E hash/text mismatch occurs before approval | Critical | Do not supersede prior approval | Unit |
| 8 | D8 | Writer runs on Windows and POSIX | High | Lock path remains absolute, private, and platform-safe | Unit |
| 9 | D12 | Error occurs while writing prompt text | High | Diagnostics expose IDs/status/codes only, never prompt or auth data | Unit |
| 10 | D6 | Google schema has no Is_Current field | High | Use APPROVED status as the canonical current marker; do not add columns | Unit |

Severity: Critical = data loss or unsafe approval; High = operation blocked with a safe recovery path.

## Must-cover before merge

1. Allocate the next version and ID from live rows; reject duplicate identity/version.
2. Revalidate exact Phase E prompt and source hashes before validation and again before approval.
3. Preserve immutable payload/hash fields on every lifecycle transition.
4. Abort on expected-current-row drift and uncertain write readback.
5. Approval state changes are one bounded batch request; verify both rows and exactly one APPROVED row afterward.
6. The current Google Sheet and local schema both have 14 columns and no `Is_Current`; current is derived from `Prompt_Status=APPROVED`.
7. Keep logs and errors free of prompt text and credentials.

## Deferred

- Cross-host compare-and-swap is unavailable in Google Sheets; operationally require one writer and treat any concurrent ambiguity as HOLD.
- No browser or paid-generation BDD is relevant; the operation never opens Flow.

## Suggested BDD scenarios

```gherkin
Feature: Immutable prompt version approval
  Scenario: A stale candidate cannot supersede the approved prompt
    Given a current approved prompt and a stale validated candidate
    When prompt approval is attempted against fresh Phase E
    Then approval stops and the previous row remains unchanged

  Scenario: A fresh candidate replaces the current approval
    Given a persisted candidate that exactly matches fresh Phase E
    When the candidate is approved
    Then the prior approval is superseded and exactly one prompt remains approved
```

## Known-issues cross-reference

No direct Google Flow auth or selector known issue applies. The persistence boundary is newly approved by this task; it must remain isolated from provider execution.

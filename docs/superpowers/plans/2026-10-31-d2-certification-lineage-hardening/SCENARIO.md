# Scenario: D2 Certification Run Lineage

## Coverage map

Active: D1 (profile root identity), D5 (concurrent claims), D6 (Sheets/runtime ledger identity), D7 (fail-closed quote errors), D12 (diagnostic state), and D13 (not applicable: this is an internal operations runner with no CLI/MCP product surface). Browser-specific Flow behavior is unchanged; a paid Phase G live run is explicitly out of scope.

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D5 | Same explicit run intent is submitted concurrently | Critical | Request key uniqueness returns one Production Job and one Scene Job | Integration |
| 2 | D5/D6 | Distinct explicit run intents race for the next run number | Critical | Unique Script_ID + Production_Run_No constraint permits one allocation; the loser fails closed and can replay its same intent after a fresh read | Integration |
| 3 | D6 | A new explicit run uses unchanged scene/profile/prompt | High | New PRD/SJB/D2 identity; workload fingerprint and Scene_Set_SHA256 remain stable | Unit |
| 4 | D6 | Same intent is replayed after failure/timeout | Critical | Existing job is returned; retry does not allocate another Production_Run_No | Unit |
| 5 | D6 | Historical quarantined or ambiguous job exists | Critical | New intent cannot reuse or mutate either historical job | Integration |
| 6 | D7 | Canonical cost quote is missing or ambiguous | Critical | Operator stops before job/attempt/reservation creation | Unit |
| 7 | D7 | Flow quote is greater than fresh canonical per-run quote | Critical | Credit guard refuses reservation and approval | Unit |
| 8 | D7 | Daily ceiling is 50 but canonical run quote is 10 | Critical | Reserved amount cannot exceed 10 | Unit |
| 9 | D1 | GFLOW_CLI_HOME is absent | High | Fixed PROD operator resolves only `/var/lib/gflow/browser-state/profile_gflow-prod` | Unit |
| 10 | D1 | GFLOW_CLI_HOME points elsewhere | Critical | Operator fails closed without login, profile creation, or account switch | Unit |

## Must-cover before merge (Critical + High)

1. Cover scenarios 1–8 in focused validation; keep all production data untouched.
2. Cover the two profile-root cases without opening or modifying profile contents.
3. Do not run the paid/live generation E2E. The explicit no-Generate boundary is the named blocker; record the live surface as unverified and keep the actual certification run separate.

## Deferred

None. Live Flow generation verification remains blocked by the user’s explicit zero-Generate instruction.

## Suggested BDD scenarios

```gherkin
Feature: Explicit D2 certification run identity
  Scenario: Replaying one explicit certification intent returns the same jobs
    Given an explicit certification run intent has been claimed
    When the same intent is submitted again
    Then the same Production Job and Scene Job are returned
    And no additional run number is allocated

  Scenario: A fresh intent creates a new run without changing the workload
    Given prior runs exist for the same workload
    When a separately authorized explicit run intent is claimed
    Then a new Production Job and Scene Job are allocated
    And the Scene_Set_SHA256 remains unchanged

  Scenario: The validated quote caps one run reservation
    Given a valid canonical quote of 10 credits
    When the provider confirmation quotes 50 credits
    Then reservation is rejected before confirmation approval
```

## Known-issues cross-reference

No selector, transport, or auth protocol change. Existing PROD same-attempt recovery contracts remain independent and unchanged.

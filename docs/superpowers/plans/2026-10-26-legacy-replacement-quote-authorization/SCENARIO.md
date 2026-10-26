# Scenario: Legacy Replacement Quote and Authorization

## Coverage map

- **D6 Data layer:** quote lookup, ledger migration, durable one-time authorization, and atomic
  consumption are the core behavior.
- **D7 Error propagation:** absent or ambiguous price and every invalid authorization condition
  must fail closed with stable internal outcomes.
- **D8 Cross-platform paths:** the D2 ledger lock and atomic replacement must continue to work on
  Windows and POSIX.
- **D12 Observability:** decisions and authorization records must bind source digests without
  storing prompt text, credentials, or secrets.
- **D13 MCP parity:** no CLI/MCP/queue surface is added; confirm this remains internal.

D1/D2/D3/D4/D5/D9/D10/D11 and real-Flow E2E are out of scope because the change does not touch
authentication, browser selectors, Flow, transport, user command surfaces, or provider generation.

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D6 Data | One active verified credit row exactly matches the quote configuration | Critical | Return a quote bound to source-row and input digests | Unit |
| 2 | D6 Data | Cost row is absent, expired, future-dated, inactive, unverified, ambiguous, or monetary | Critical | Return `QUOTE_UNAVAILABLE`; no attempt, authorization, or reservation is written | Unit |
| 3 | D6 Data | Provider/account/model/mode/profile or populated cost parameter differs | Critical | Reject the row; never borrow another profile's or model's cost | Unit |
| 4 | D6 Data | Duration/aspect/audio/output dimensions are not represented by verified pricing scope | Critical | Fail closed instead of assuming those dimensions do not affect cost | Unit |
| 5 | D6 Data | Historical `accounted_credit_cost=50` exists but quote is absent | Critical | Historical accounting is not used as a quote | Unit |
| 6 | D6 Data | Existing schema-2 D2 ledger is opened by the new code | High | Read and migrate compatibly on write; existing attempts and TEST behavior remain unchanged | Integration |
| 7 | D6 Data | Two authorization requests race for one parent | Critical | Ledger lock permits at most one active authorization | Integration |
| 8 | D6 Data | Authorization consumption is replayed with the same replacement ID | Critical | Exact replay returns the same consumed link; no duplicate attempt is appended | Integration |
| 9 | D6 Data | Authorization is replayed with a different replacement ID or parent | Critical | Reject without mutating the ledger | Integration |
| 10 | D6 Data | Parent is no longer quarantined or reservation changed before consumption | Critical | Reject consumption; preserve parent and reservation | Integration |
| 11 | D6 Data | Policy/reconciliation digest or quote source row changes | Critical | Reject authorization/consumption after revalidation | Integration |
| 12 | D6 Data | Ledger is corrupt, symlinked, locked, or atomic write fails | Critical | Fail closed; do not create a partial authorization or replacement attempt | Integration |
| 13 | D7 Errors | Caller asks ordinary Phase G, scheduler, or retry path to create a replacement | Critical | No replacement authorization is inferred or automatically consumed | Integration |
| 14 | D12 Audit | Authorization is created in a sanitized APPROVE fixture | High | Persist exact parent, job, policy, reconciliation, exposure, quote, decision digest, status, and software identity; no Generate permission | Integration |
| 15 | D13 MCP | Capability remains internal | Medium | No CLI/MCP/queued-payload change; parity is explicitly recorded as not applicable | Static/review |

## Must-cover before merge (Critical + High)

1. Valid source-backed quote and all unavailable/mismatch/ambiguity cases.
2. No quote call creates an attempt, reservation, approval, or Generate action.
3. One active authorization per parent and at most one consumed replacement attempt.
4. Parent quarantine, 50-credit unknown reservation, and exact linked audit preconditions are
   revalidated.
5. Authorization consumption and attempt linkage are atomic and idempotent in the D2 ledger.
6. Existing schema-2 ledger, explicit retry authorization, modern PROD, and frozen TEST regressions
   remain unchanged.
7. No scheduler, retry worker, or ordinary orchestrator path automatically creates a replacement.

## Deferred

1. A numeric quote for this live parent remains unavailable until an authoritative credit rate and
   applicable pricing scope are added to canonical master data by its owner.
2. No live replacement authorization or attempt will be created by these tests.

## Suggested BDD scenarios

These scenarios exercise our own code, not Flow, so they are bound from `tests/features/` without
`@e2e` tags.

```gherkin
Feature: Legacy replacement quote and one-time authorization
  Scenario: Missing authoritative credit price keeps the decision on hold
    Given a quarantined ambiguous legacy attempt with a 50-credit unresolved reservation
    And no applicable verified credit-price row
    When the replacement decision is evaluated
    Then the quote is unavailable and the decision is HOLD
    And no authorization, replacement attempt, or reservation is created

  Scenario: A consumed authorization links exactly one replacement attempt
    Given an approved authorization bound to one quarantined parent and quote
    When the controlled replacement creation consumes it with one attempt ID
    Then the attempt records its parent and authorization IDs
    And the authorization is consumed
    And a second or different consumption fails
```

## Known-issues cross-reference

- `KNOWN_ISSUES.md` documents that `UNKNOWN_SUBMISSION_STATE` must not be retried automatically;
  this feature preserves that rule and adds no automatic replacement path.
- The prior ambiguous-recovery plan explicitly deferred authorization within its scope. The current
  user request supersedes that scope; modern PROD and frozen TEST contracts remain unchanged.

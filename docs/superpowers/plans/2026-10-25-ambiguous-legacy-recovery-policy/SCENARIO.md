# Scenario: Ambiguous Legacy PROD Recovery Policy

## Coverage map

- **D6 — Data layer:** extend existing transaction status validation and preserve immutable attempt/credit records; add an append-only policy audit and replacement-lineage uniqueness.
- **D7 — Error propagation:** fail closed on stale identity, changed expected state, missing evidence audit, partial write, or conflicting prior policy event.
- **D12 — Observability:** report provider fate as `UNKNOWN` separately from local job quarantine and unresolved credit exposure.
- **D13 — MCP surface parity:** no CLI/MCP/queue capability is added; this is an internal operations policy path. Verify no public surface or queued payload changes.
- Other dimensions are not touched: no auth/session, Flow selectors, provider transport, browser pool, network retry, or platform-specific path behavior.

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D6 | Exact legacy attempt has valid unique identity, matching reconciliation audit, durable evidence-exhausted result, no provider receipt, and exact held reservation | Critical | Transition only the linked Production Job and Scene Job to a non-runnable quarantine state; preserve D2 `UNKNOWN_SUBMISSION_STATE`, reservation amount/state, and unknown billing; persist policy audit | Integration + BDD |
| 2 | D6/D7 | Reconciliation audit or persisted evidence-recovery result is absent, invalid, or names another attempt | Critical | Fail closed before any job-row mutation; chat or prompt values cannot satisfy the persisted-result prerequisite | Integration |
| 3 | D6/D7 | A late submit, confirmation, execution, media, refusal, duplicate candidate, or identity mismatch is present on a fresh read | Critical | Refuse this policy and return to same-attempt reconciliation; never overwrite evidence or infer exhaustion | Integration |
| 4 | D6 | Exact reservation is absent, duplicated, changed, or bound to another attempt/account/project | Critical | No quarantine application is reported complete; no reservation or billing mutation is attempted | Integration |
| 5 | D6/D7 | One Sheets write acknowledgement is lost or a row changed between pre-read and readback | Critical | Apply only with exact expected-state guards; read back both rows; fail closed or resume idempotently without declaring partial success | Integration |
| 6 | D6 | Policy is applied twice with the same evidence digest | High | Second application is a no-op: no repeated status transition, duplicate harmful event, reservation change, or lineage creation | Integration |
| 7 | D6 | A prior policy event exists with a different evidence digest or conflicting job state | Critical | Refuse overwrite and require operator review | Integration |
| 8 | D6 | Orchestrator receives an idempotent claim for a quarantined Scene Job | Critical | Stop before workspace/request setup, adapter construction, `recover_existing`, or `generate_once` | Integration + BDD |
| 9 | D6 | Retry/append-attempt logic sees a quarantined attempt | Critical | Reject; do not increment attempt number, create a D2 row, or promote the job to runnable state | Integration |
| 10 | D6 | Ordinary non-policy `HOLD` job with valid recovery evidence is processed | High | Existing HOLD recovery behaviour remains intact; only the exact quarantine reason/state is non-resumable | Integration |
| 11 | D6/D12 | Exposure report sees the preserved 50-credit reservation but no quote for a potential replacement | High | Report existing unresolved exposure as 50; potential new reservation and combined maximum as unknown/unquoted; create no reservation | Unit + Integration |
| 12 | D6 | Policy marks the attempt replacement-eligible but no replacement authorization exists | High | Record eligibility and lineage reference only; no replacement attempt or Generate action is created; future authorization uniqueness is enforced by the policy lineage store | Integration |
| 13 | D6 | A new status is written but local validators or transaction schema do not recognize it | Critical | Validation rejects the write before Sheets mutation; status enum and data dictionary stay aligned | Unit + Integration |
| 14 | D12 | Provider fate is unknown while local orchestration is quarantined | High | Audit/report keeps provider submission and billing `UNKNOWN`; quarantine is described only as a local automation stop | Unit + BDD |
| 15 | D13 | Internal operator policy has no user-facing CLI/MCP twin | Medium | No MCP, codec, public CLI, or worker payload changes are introduced; parity suite remains green | Unit |
| 16 | D6 | Modern PROD or frozen TEST recovery receives the new state/policy data | Critical | Existing modern PROD and frozen TEST contracts/behaviour remain unchanged | Integration |

## Must-cover before merge (Critical + High)

1. Persisted preconditions and exact identity must all match before state changes.
2. Quarantine must block all orchestrator recovery and retry paths before an adapter can run.
3. Partial/uncertain Sheets writes must be read back and safely replayable; never report success with only one job row changed.
4. The exact reservation, D2 row, and billing outcome remain unchanged/unknown.
5. Audit/idempotency conflicts fail closed and do not create duplicate replacement lineage.
6. Replacement eligibility must not authorize or create a replacement attempt.
7. Existing ordinary HOLD, modern PROD, and frozen TEST paths remain unchanged.

## Deferred (Medium + Low — log as issues, not blockers)

1. Public CLI/MCP control surfaces are out of scope; the policy remains an internal operations capability.
2. No Flow/browser E2E is applicable because this feature performs no provider or Flow operation.

## Suggested BDD scenarios

```gherkin
Feature: Quarantine an exhausted ambiguous legacy PROD attempt
  Scenario: Stop local automation while preserving unknown provider fate and credits
    Given an exact legacy PROD attempt with validated identity
    And a persisted ambiguous reconciliation audit
    And a persisted evidence-exhausted result for that attempt
    And its exact reservation is still held
    When the recovery policy is applied
    Then both linked jobs enter a non-runnable quarantine state
    And the D2 submission state remains unknown
    And the reservation amount and state remain unchanged
    And no replacement attempt is created

  Scenario: Replayed claim cannot resume the quarantined attempt
    Given a Scene Job in the exhausted-ambiguity quarantine state
    When the production orchestrator replays its idempotent claim
    Then it stops before adapter construction or recovery
    And it does not Generate or create another attempt
```

## Known-issues cross-reference

`KNOWN_ISSUES.md` documents interrupted post-submit queue work as indeterminate and never auto-resubmitted. This policy applies only to the isolated V2 legacy PROD job/attempt path and keeps that conservative principle; it does not change the worker queue or claim provider fate is resolved.
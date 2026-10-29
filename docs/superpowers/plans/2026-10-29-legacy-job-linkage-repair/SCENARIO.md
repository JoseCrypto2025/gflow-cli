# Scenario: Legacy D2 Job Linkage Repair

## Coverage map

- **D6 Data layer:** schema migration, atomic mutation, idempotency, and preservation of legacy rows.
- **D7 Error handling:** ambiguous, conflicting, stale, or malformed proofs fail closed with a stable error code.
- **D12 Observability:** the separate repair audit binds source digests, prior audits, before/after row hashes, timestamp, and software commit.
- **D13 MCP parity:** not applicable; this is an operator-only maintenance operation with no CLI or MCP product surface.
- **Browser/Flow E2E:** not applicable; repair changes two local linkage fields and one local audit, and performs no provider action.
- **Other dimensions:** auth, WAF, selectors, batch resume, Page pool, transport, UI environment, and user-input boundaries are not touched.

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D6 | A unique canonical join proves both missing job IDs | Critical | Backfill only the two linkage fields and append one repair audit atomically | Unit / BDD |
| 2 | D6 | Exact repair is rerun with unchanged source evidence | Critical | Return the existing audit; do not append a second event or change any bytes in the attempt row | Unit / BDD |
| 3 | D7 | Either linkage field already conflicts | Critical | Reject before writing; attempt and audit remain unchanged | Unit |
| 4 | D7 | More than one D2, production-job, scene-job, or reservation candidate exists | Critical | Reject as ambiguous before writing | Unit |
| 5 | D7 | Prompt hash, account, project, environment, reservation, or audit linkage disagrees | Critical | Reject as conflicting; preserve all source records | Unit |
| 6 | D6 | Schema v1, v2, or v3 is read and then mutated | High | Migrate to v4 while preserving all prior attempts, test sessions, and authorizations | Unit |
| 7 | D6 | A concurrent writer changes the target attempt after proof capture | Critical | Detect the stale target digest under the ledger lock and reject without writing | Unit |
| 8 | D12 | Audit is missing, malformed, duplicated, or fails its digest check | Critical | Reject; never infer or reconstruct the audit | Unit |
| 9 | D6 | Unrelated legacy and modern attempts share the ledger | Critical | Leave every non-target attempt byte-for-byte equivalent | Unit |

## Must-cover before merge

1. Proven linkage, idempotent replay, candidate ambiguity, and each authoritative-field conflict.
2. Preserve reservation, state, Generate count, provider evidence, timestamps, other attempts, and existing replacement records.
3. Validate the exact `POLICY_APPLIED` and `AMBIGUOUS_AFTER_RECONCILIATION` audit links.
4. Operator refuses when either parent linkage is missing after repair or when the source proof is stale.

## Deferred

None. The repair is intentionally limited to this exact legacy linkage case.

## Suggested BDD scenarios

```gherkin
Feature: Repair missing legacy D2 job linkage
  Scenario: Backfill a unique proven parent join without changing attempt history
    Given a legacy D2 attempt with missing job linkage and authoritative matching sources
    When the versioned linkage repair is applied
    Then only the production and scene job IDs are added
    And one durable audit binds the evidence and before/after digests
    And the reservation, submission state, Generate count, and provider evidence are unchanged

  Scenario: Replaying the same proven repair is idempotent
    Given an attempt already repaired under the exact linkage audit
    When the same repair proof is applied again
    Then the existing audit is returned without another ledger write

  Scenario: Ambiguous or conflicting sources stop the repair
    Given duplicate candidates or a mismatched authoritative correlation field
    When the linkage repair is requested
    Then the repair is rejected and no ledger field changes
```

## Known-issues cross-reference

No Flow UI or provider issue is implicated. The frozen legacy recovery contract remains read-only and continues to classify the job IDs as `DERIVED_CANONICAL_JOIN`; the new repair contract is an explicitly separate, audited compatibility operation.

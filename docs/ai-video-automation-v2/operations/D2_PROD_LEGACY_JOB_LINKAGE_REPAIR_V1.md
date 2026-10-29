# D2 PROD Legacy Job Linkage Repair V1

**Contract ID:** `D2_PROD_LEGACY_JOB_LINKAGE_REPAIR_V1`
**Status:** Versioned compatibility repair
**Scope:** One exact persisted legacy D2 row whose parent job IDs were not stored by its original writer.

## Relationship to legacy recovery V1

This is a separate, narrowly scoped repair contract. It does not change the frozen
`D2_PROD_LEGACY_ATTEMPT_RECOVERY_V1` read-only reconciliation or evidence-recovery behavior. The
legacy envelope continues to derive `production_job_id` and `scene_job_id` from canonical rows and
report both as `DERIVED_CANONICAL_JOIN`; it never treats the repair as provider evidence.

The supported operator is
`scripts/operations/repair_legacy_parent_job_linkage.py`. It is restricted to the exact D2,
Production Job, and Scene Job IDs named by its source constants and command-line guard. It reads
canonical Google Sheets, the D2 and shared-credit ledgers, the already-applied recovery-policy
audit, and its linked prior reconciliation audit. It has no Flow browser, provider submission,
pricing write, reservation write, or Generate capability.

## Proof required before mutation

The operator must build a fresh `IDENTITY_VALIDATED` legacy envelope and require all of the
following to agree:

- Exactly one D2 row, deterministic Scene Job candidate, Production Job row, and shared reservation.
- Scene, Prompt Version, approved prompt hash, Generation Profile, Provider Account, Flow project,
  and PROD environment correlations.
- The legacy reservation remains exactly 50 credits in `UNKNOWN_SUBMISSION_STATE`, with one
  historical Generate click; provider billing remains unknown.
- Persisted job/attempt times are monotonic and compatible. Timestamps corroborate the canonical
  identity but never select a candidate by proximity.
- The exact `POLICY_APPLIED` event remains hash-chain valid and binds both quarantined parent jobs,
  the unchanged reservation, prohibited automatic retry/resume, and Generate unauthorized.
- The exact prior legacy reconciliation event remains digest-valid, reports
  `AMBIGUOUS_AFTER_RECONCILIATION`, performed no mutation, preserved the reservation, and records
  the two job IDs as `DERIVED_CANONICAL_JOIN`.

Any duplicate candidate, conflicting field, missing audit, stale target digest, incompatible time,
or non-PROVEN result stops before a write. The proof records match counts, all correlation results,
selected-source digests, complete source-snapshot hashes and observation times, both prior audit
hashes, verification time, and the running software commit.

## Atomic ledger operation

The D2 ledger migrates from schema v1, v2, or v3 to schema v4 on its next mutation. Migration
preserves attempts, test sessions, and replacement authorizations and initializes an append-only
`legacy_job_linkage_repairs` list. V4 rejects a malformed repair audit chain.

Under the existing D2 ledger file lock, the repair method re-reads the target row, verifies the
proof digest and the target-attempt compare-and-set hash, then sets only:

- `production_job_id`
- `scene_job_id`

It appends one hash-chained event to `legacy_job_linkage_repairs` in the same atomic file replace.
The event records the contract and audit IDs, parent and job IDs, `DERIVED_CANONICAL_JOIN`
provenance, before/after values and attempt-row hashes, correlation results, candidate counts,
source snapshot hashes/times, policy and reconciliation audit hashes, verification time, and
software commit. A same-proof replay validates the existing event and returns it without writing.
Conflicting or partial audit state fails closed.

No other attempt field is rewritten: provider evidence, submission and billing state, reservation,
Generate/approval counts, and historical timestamps remain intact. Unrelated and modern attempts
are not selected by the operation.

## Operator invocation

Run only after deploying and verifying the exact source commit:

```bash
python scripts/operations/repair_legacy_parent_job_linkage.py \
  --d2-run-id D2_13c62112d9881710019049d95cb6da57 \
  --production-job-id PRD_000001 \
  --scene-job-id SJB_000001
```

The operator reports `PASS` with before/after linkage and `linkage_audit_id`, or a stable `HOLD`
blocker. After `PASS`, read the row back and verify all historical invariants before any separate
replacement task. This contract itself never authorizes replacement creation, reservation, or
Generate.

## Required regression coverage

Tests cover successful proof and backfill, idempotent replay, either linkage conflict, ambiguous
candidates, prompt/account/project/environment and audit mismatches, stale target proof, schema
migration, target and unrelated-row preservation, and the replacement operator's continued refusal
when either persisted job ID is missing. No browser E2E applies because this is a local ledger
maintenance path that does not touch Flow.

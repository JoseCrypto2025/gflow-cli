# Legacy D2 Job Linkage Repair Implementation Plan

> **For agentic workers:** Run `/gflow:status --feature legacy-job-linkage-repair` to find the next unchecked task. Run `/gflow:check` before every commit.

**Goal:** Repair the exact missing parent job linkage only after a unique proof from canonical rows, persisted reservation, and existing policy/reconciliation audits, with an atomic and idempotent audit trail.

**Architecture:** Add a versioned proof builder for the existing legacy envelope and a narrow `D2DailyCreditLedger` repair method. Migrate the append-only D2 ledger to schema v4 with a separate repair-audit list; keep legacy reconciliation V1 read-only and preserve its `DERIVED_CANONICAL_JOIN` provenance. Add one exact-target operator and allowlist it in the documented deployment archive.

**Predict verdict:** CAUTION — confidence 8/10

**Risk register:**

| Severity | Risk | Mitigation |
|---|---|---|
| Critical | Backfill could misattribute a legacy attempt | Require a unique deterministic scene-job join, full canonical identity agreement, reservation agreement, and both prior audit links; reject all ambiguity/conflict |
| Critical | Atomic ledger migration could alter history | Use existing file lock and atomic writer; mutate only two target fields and append one separate audit; assert all other target fields and rows unchanged |
| High | Frozen V1 contract could be reinterpreted | Add separate `D2_PROD_LEGACY_JOB_LINKAGE_REPAIR_V1`; leave V1 reader/reconciler semantics intact |
| High | Replayed or stale proof could duplicate or misapply repair | Deterministic audit ID, stable proof digest, idempotent exact replay, and target-row compare under lock |

---

## File structure

### New files

```text
src/gflow_cli/v2_runtime/phase_g_prod_legacy_job_linkage.py
  Build and validate deterministic proof from the legacy recovery envelope and prior audits.
scripts/operations/repair_legacy_parent_job_linkage.py
  Exact-target read/verify/repair operator; no provider calls.
docs/ai-video-automation-v2/operations/D2_PROD_LEGACY_JOB_LINKAGE_REPAIR_V1.md
  Versioned operator contract, provenance, failure behavior, and audit format.
tests/validation/test_legacy_job_linkage_repair.py
  Proof, ledger migration, atomicity, idempotency, and preservation tests.
tests/features/legacy_job_linkage_repair.feature
tests/features/test_legacy_job_linkage_repair_steps.py
  BDD-bound regression scenarios for the local repair operation.
```

### Modified files

```text
src/gflow_cli/v2_runtime/phase_g.py
  Schema v4 migration and atomic narrow repair method.
scripts/operations/deploy_v2_runtime.py
  Include the new maintenance operator in the safe deployment archive allowlist.
tests/validation/test_legacy_replacement_operator.py
  Confirm repaired lineage is accepted and missing lineage still fails closed.
CHANGELOG.md
  Record the operator-only compatibility repair.
```

## Task 1 — Proof and ledger regression scaffold (test scaffold)

**What:** Add failing tests for proof validity, backfill, replay, conflict rejection, schema migration, and preservation.

**Steps:**
- [x] Build reusable fixtures from the canonical legacy source envelope.
- [x] Assert all requested mismatch and ambiguity cases fail before any write.
- [x] Assert exact target and unrelated rows remain unchanged except for the two linkage keys and audit list.

## Task 2 — Versioned contract and proof builder

**What:** Produce a deterministic proof only when all independent source joins and linked audits agree.

**Steps:**
- [x] Require unique exact D2, scene-job, production-job, and reservation rows.
- [x] Require all eleven required correlations and timestamps to agree.
- [x] Validate the applied recovery-policy audit and prior ambiguous reconciliation event.
- [x] Bind normalized source snapshots and target-attempt digest in the repair proof.

## Task 3 — Atomic schema-v4 ledger repair

**What:** Add one narrow, locked, idempotent D2 ledger operation.

**Steps:**
- [x] Migrate schemas v1–v3 while preserving all existing data.
- [x] Reject conflicting or partially populated linkage, stale proof, and corrupt/duplicate audit.
- [x] Add only the two linkage fields and one append-only repair audit event.
- [x] Verify exact same-proof replay returns the existing audit.

## Task 4 — Exact-target operator and deployment manifest

**What:** Integrate fresh canonical reads, validated prior audits, and the narrow repair method in one supported operator.

**Steps:**
- [x] Restrict the operator to the named D2 / PRD / SJB identities.
- [x] Require production runtime and the connected canonical snapshot source.
- [x] Do not call Flow, Generate, pricing mutation, or reservation APIs.
- [x] Add the operator to the deployment archive allowlist and required-file checks.

## Task 5 — Replacement-operator and BDD regression

**What:** Prove repaired lineage unblocks only the previously failing identity gate and leaves all billing/provider gates enforced.

**Steps:**
- [x] Test repaired lineage acceptance and each missing-field refusal.
- [x] Run the BDD-bound local scenarios; no browser E2E is applicable because no Flow surface is touched.

## Task 6 — Focused validation, commit, push, deploy, and exact repair

**What:** Run the requested focused gates, ship by normal fast-forward to `automation-v2`, deploy the exact commit, then apply and verify the repair.

**Steps:**
- [x] Run focused tests, repo-wide Ruff/format, and Pyright; full coverage and Flow E2E do not apply to this local ledger operation.
- [ ] Recheck open PR overlap and remote head, then commit one coherent change.
- [ ] Push only `automation-v2` if it is a normal fast-forward; stop on any unexpected remote advance.
- [ ] Create rollback backup, deploy exact source commit, run runtime doctor, and verify manifest/head parity.
- [ ] Back up the exact D2 ledger; apply the proof-bound repair and verify historical invariants.
- [ ] Continue only after fresh quote, late-parent evidence, formal replacement decision, and all pre-Generate checkpoints pass.

## Definition of done

- [ ] Linkage confidence is `PROVEN`; the audit shows `DERIVED_CANONICAL_JOIN` and exact source digests.
- [ ] The only attempt-row fields added are `production_job_id` and `scene_job_id`; all other historical fields and unrelated attempts remain unchanged.
- [ ] Required focused tests, Ruff, format, Pyright, runtime doctor, and head parity pass.
- [ ] Replacement execution occurs only under the existing one-time direct approval and only if every fresh gate passes; stop before D7.

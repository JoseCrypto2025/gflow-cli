# PROD Same-Attempt Recovery Implementation Plan

> **For agentic workers:** Implement one task at a time. Keep the named production attempt untouched. Do not open Flow or invoke any provider action. Run the repository check gate before committing.

**Goal:** Add an internal, idempotent PROD same-attempt reconciliation capability that distinguishes proven provider evidence from a UI action while preserving the existing TEST recovery contract.

**Architecture:** Add a separately versioned PROD contract and a durable receipt/evidence model in the V2 runtime. The reconciler writes the existing D2 attempt audit and, for positive refusal only, the shared production credit guard; the orchestrator owns QUEUED/HOLD/RUNNING job transitions. Do not expose a public CLI or MCP tool in this change.

**Predict verdict:** CAUTION — confidence 5/10. Five-persona range: 4–8/10. The no-Generate boundary and TEST-only separation are clear; post-click negative proof and atomic consistency across three persisted stores remain the main risks.

**Risk register:**

| Severity | Risk | Mitigation |
|---|---|---|
| Critical | UI click or missing output is mistaken for accepted/rejected provider work | Require exact provider evidence; absence and timeout remain ambiguous |
| Critical | D2 ledger and shared production credit guard diverge | Persist the D2 audit first; have the guard independently re-read the same refusal and audit before one-time release; keep jobs held on partial failure |
| Critical | TEST behavior broadens into PROD accidentally | Keep V1 contract and TEST operator unchanged; add separate PROD contract and environment-specific tests |
| High | Production sheets reject new lifecycle states | Prefer schema-compatible `QUEUED`/`HOLD` where possible; version dictionary/schema changes and validators together if new values are needed |
| High | `PROVEN_NOT_SUBMITTED` could be inferred from absence or untrusted input | Require the persisted, single-request correlated 403 refusal, exact code, completed window, no request failure, no generation record, and no execution/media; credit guard independently verifies the D2 audit |
| Medium | An internal feature accidentally creates CLI/MCP parity obligations | Keep the API internal; no CLI or MCP signature changes |

## File structure

### New files

```text
docs/ai-video-automation-v2/operations/D2_PROD_SAME_ATTEMPT_RECOVERY_V1.md
  Versioned PROD-only recovery contract.
docs/superpowers/plans/2026-10-25-prod-same-attempt-recovery/SCENARIO.md
  Pre-implementation scenario matrix.
docs/superpowers/plans/2026-10-25-prod-same-attempt-recovery/PLAN.md
  This implementation plan.
tests/validation/test_phase_g_prod_reconciliation.py
  Deterministic PROD reconciliation and receipt tests.
src/gflow_cli/v2_runtime/phase_g_prod_reconciliation.py
  Offline evidence model, identity checks, and deterministic result classifier.
```

### Modified files

```text
AGENTS.md
  Point PROD recovery work to the new contract while preserving the frozen TEST contract.
src/gflow_cli/v2_runtime/phase_g.py
  Persist PROD-only attempt lineage, refusal evidence, and idempotent reconciliation audit.
src/gflow_cli/v2_runtime/phase_g_live_flow.py
  Persist the exact correlated refusal code observed on the tracked submit response.
src/gflow_cli/v2_runtime/phase_g_run.py
  Add PROD-specific internal reconciliation entrypoint with no Generate or approval capability.
src/gflow_cli/v2_runtime/production_execution.py
  Add safe idempotent reconciliation of existing PROD reservations.
src/gflow_cli/v2_runtime/production_jobs.py
  Correct pre-execution job state transitions and validate any added states.
docs/ai-video-automation-v2/architecture/PHASE_G_D2_CONTROLLED_EXECUTION.md
  Document PROD reconciliation and state semantics; existing QUEUED/HOLD statuses require no sheet schema change.
  Document the PROD contract and state semantics without changing the frozen TEST contract.
tests/validation/test_production_orchestrator.py
tests/validation/test_production_execution.py
tests/validation/test_production_jobs.py
  Cover state and credit behavior.
```

## Task 1 — Contract and evidence audit

**What:** Define exactly which captured PROD evidence supports each result and write the new versioned contract before implementation.

**Files:**
- `docs/ai-video-automation-v2/operations/D2_PROD_SAME_ATTEMPT_RECOVERY_V1.md`
- `AGENTS.md`
- Plan review record in this directory

**Steps:**
- [x] Separate authoritative provider receipt fields from diagnostic transport/UI evidence.
- [x] Define exact identity and freshness predicates for pending, execution, output, late confirmation, and ambiguous outcomes.
- [x] Define `PROVEN_NOT_SUBMITTED` only from the exact persisted single-request provider refusal; otherwise preserve ambiguity and prohibit reservation release.
- [x] Define idempotency and fail-closed behavior across the Phase G ledger and production credit guard; job schemas keep existing statuses.
- [x] Preserve `D2_SAME_ATTEMPT_RECOVERY_V1` unchanged and add a separate PROD contract pointer.

**Tests/checks:**
- [ ] Review contract claims against current `PhaseGSubmitTransportEvidence`, `PhaseGAttemptEvidence`, ledger fields, and credit guard behavior.

## Task 2 — Red reconciliation and receipt tests

**What:** Add deterministic tests for the 23 required outcomes before production code changes.

**Files:**
- `tests/validation/test_phase_g_prod_reconciliation.py`
- Existing production job/credit/orchestrator tests as needed

**Steps:**
- [x] Cover click-vs-submission separation, exact receipt matching, output identity, duplicate candidates, and stale evidence.
- [x] Cover credit no-double-reserve, one-time release only for supported positive proof, and retained UNKNOWN billing.
- [x] Cover no RUNNING state before execution proof and ambiguous entrypoint audit behavior.
- [x] Cover environment isolation and preserve TEST recovery code paths.
- [x] Keep the PROD entrypoint free of Generate/approval/attempt-creation dependencies.

## Task 3 — Durable receipt model and PROD reconciler

**What:** Add the internal deterministic PROD reconciliation function and persist only evidence that the runtime can actually observe.

**Files:**
- `src/gflow_cli/v2_runtime/phase_g.py`
- `src/gflow_cli/v2_runtime/phase_g_run.py`
- `tests/validation/test_phase_g_prod_reconciliation.py`

**Steps:**
- [x] Keep UI action, submit transport observation, provider submission proof, execution proof, and output proof distinct.
- [x] Bind receipts to existing D2/production/scene IDs, account, project, scene, prompt hash, and generation profile.
- [x] Return exactly one of the six required result values.
- [x] Keep ambiguous results held; no provider/browser call, approval, Generate, or attempt creation.
- [x] Keep the TEST recovery entrypoint and contract source unchanged.

## Task 4 — Credit reconciliation

**What:** Make PROD reservation updates exact, idempotent, auditable, and independent from visible balance.

**Files:**
- `src/gflow_cli/v2_runtime/production_execution.py`
- `src/gflow_cli/v2_runtime/phase_g.py`
- `tests/validation/test_production_execution.py`
- `tests/validation/test_phase_g_prod_reconciliation.py`

**Steps:**
- [x] Confirm late evidence against the existing attempt without reserving again.
- [x] Release an exact reservation at most once only when the persisted D2 attempt independently proves refusal.
- [x] Keep billing UNKNOWN separate from submission and visible balance.
- [x] Make repeated D2 audit and guard release idempotent; failures remain fail-closed.

## Task 5 — Job state semantics and schema alignment

**What:** Ensure production and scene jobs do not claim provider execution before execution proof.

**Files:**
- `src/gflow_cli/v2_runtime/production_jobs.py`
- `src/gflow_cli/v2_runtime/google_sheets_production_jobs.py`
- `src/gflow_cli/v2_runtime/production_orchestrator.py`
- `docs/ai-video-automation-v2/database/PRODUCTION_EXECUTION_DATA_DICTIONARY.md`
- Related schema fixtures and job/orchestrator tests

**Steps:**
- [x] Keep a job in `QUEUED` before provider execution proof; hold unresolved acknowledgements with a stable reason.
- [x] Transition to `RUNNING` only after the Phase G adapter returns validated execution evidence.
- [x] Keep same-attempt HOLD resume on the no-Generate exact-media recovery adapter.
- [x] Reuse existing schema statuses; no transaction schema or dictionary field change is required.

## Task 6 — Docs and final validation

**What:** Complete the internal operator documentation and run offline checks without opening Flow.

**Files:**
- `docs/ai-video-automation-v2/architecture/PHASE_G_D2_CONTROLLED_EXECUTION.md`
- `docs/ai-video-automation-v2/operations/PRODUCTION_SCENE_ORCHESTRATOR_V2.md`
- `KNOWN_ISSUES.md` only if this implementation changes its deferred-reconciliation claim

**Steps:**
- [x] Document the internal PROD entrypoint, evidence requirements, refusal proof, no-Generate boundary, and job/billing state semantics.
- [x] State explicitly that no public CLI/MCP surface was added, so no MCP twin applies.
- [x] Run targeted tests, schema-compatible job tests, Ruff, Pyright, docs, and repo hygiene gates. Targeted validation: 149 passed; Ruff and Pyright pass. Runtime doctor ran and reported local catalog/database DataStoreErrors plus no auth profiles; no Flow or provider calls were made.
- [x] Do not run Flow/E2E or the separate current-attempt reconciliation task.
- [x] Commit once after all required validation passed (`4174607f`, finalized as the single task commit).

## Definition of done

- [x] PROD contract is versioned and explicit; TEST V1 contract and recovery function remain unchanged.
- [x] PROD reconciliation is deterministic, idempotent, and cannot generate or approve.
- [x] `PROVEN_NOT_SUBMITTED` releases credits only with exact persisted refusal proof; other evidence stays ambiguous and reserved.
- [x] Neither job can be `RUNNING` before Phase G execution proof.
- [x] No `CONTENT_ID` dependency or guessed field is introduced.
- [x] Targeted tests (149), Ruff, Pyright, docs, hygiene, and website mirror checks pass; doctor findings are recorded above.
- [x] Current PROD attempt is untouched; reconciliation remains a separate task.

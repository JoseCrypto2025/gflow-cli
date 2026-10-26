# Legacy Replacement Quote and Authorization Implementation Plan

> **For agentic workers:** Use `/gflow:status --feature legacy-replacement-quote-authorization`
> to find the next unchecked task. Run `/gflow:check` before every commit.

**Goal:** Provide a pre-attempt quote that is either source-backed or explicitly unavailable, and a
durable, one-time authorization that can be atomically linked to at most one future replacement
attempt for a quarantined ambiguous PROD parent.

**Architecture:** Keep the capability internal to `v2_runtime`. Reuse the existing
`PROVIDER_COST_MASTER` snapshot and the D2 ledger; add versioned quote/decision values and a
legacy-specific authorization lifecycle. Do not change the policy audit schema, public CLI/MCP,
Flow, the generic credit guard, or the frozen TEST retry contract.

**Predict verdict:** GO — confidence 8/10

**Risk register:**

| Severity | Risk | Mitigation |
|---|---|---|
| Critical | No authoritative credit-price row exists for the current profile | Return `QUOTE_UNAVAILABLE`; no price, authorization, or live mutation is manufactured |
| Critical | A partial cost key could underquote a different configuration | Bind complete quote inputs; require a uniquely applicable source-verified CREDIT row; fail closed on unsupported dimensions |
| Critical | Concurrent or replayed authorization could create duplicate attempts | Store authorization and consumption in the lock-protected D2 ledger and atomically append the linked attempt |
| High | D2 ledger schema changes could affect historical PROD/TEST records | Migrate schema 2 compatibly and test preserved behavior and data |
| High | Generic retry/orchestrator paths could bypass replacement safeguards | Keep new creation behind an explicit legacy-specific authorization API; add negative tests for ordinary paths |

---

## File structure

### New files

```text
src/gflow_cli/v2_runtime/legacy_replacement.py
  Quote, decision, and authorization validation for ambiguous legacy replacements
tests/validation/test_legacy_replacement.py
  Quote, decision, lineage, and no-mutation regressions
tests/features/legacy_replacement.feature
  Internal behavior scenarios
tests/features/test_legacy_replacement_steps.py
  BDD binding for code-only scenarios
docs/ai-video-automation-v2/operations/D2_PROD_LEGACY_REPLACEMENT_AUTHORIZATION_V1.md
  Versioned operator contract
```

### Modified files

```text
src/gflow_cli/v2_runtime/phase_g.py
  Backward-compatible ledger migration and atomic authorization consumption with one linked D2 row
tests/validation/test_phase_g.py
  Schema migration, retry, and frozen TEST regressions
docs/ai-video-automation-v2/operations/AMBIGUOUS_LEGACY_RECOVERY_POLICY_V1.md
  Link the separate replacement authorization contract
docs/ai-video-automation-v2/database/MASTER_DATA_DICTIONARY.md
  Clarify when a credit-cost record can serve as a direct pre-attempt reservation quote
CHANGELOG.md
  Document internal quote and authorization behavior
```

## Task 1 — Scenario and failing test scaffolds

**What:** Capture the safety contract with offline tests before implementation.

**Steps:**
- [x] Add quote, decision, authorization, replay, concurrency, and existing-attempt no-mutation
  tests from `SCENARIO.md`.
- [x] Bind the two code-only Gherkin scenarios once from `tests/features/`.
- [x] Confirm the current empty-cost fixture returns HOLD and no generated IDs or ledger writes.

**Tests created (red):**
- [x] Valid and unavailable quote cases, including currency, dates, source verification, uniqueness,
  and configuration mismatch.
- [x] Authorization exact-parent, audit, quarantine, exposure, digest, replay, and second-consume
  cases.
- [x] Existing schema-2 and frozen TEST / modern PROD regression cases.

## Task 2 — Quote and decision domain

**What:** Add a deterministic resolver over validated canonical cost/profile/model rows and a pure
replacement decision evaluator.

**Steps:**
- [x] Bind provider, account, model, profile, mode, duration, aspect ratio, resolution, audio,
  output count, and other supplied cost inputs into a canonical digest.
- [x] Accept only one active, effective, source-verified direct CREDIT quote with exact applicable
  identity; reject monetary rates, missing pricing scope, ambiguity, and stale inputs.
- [x] Return `QUOTE_UNAVAILABLE` without a numeric fallback when price cannot be proven.
- [x] Calculate combined exposure only when both the current exact reservation and quote are known.
- [x] Return APPROVE only when every gate is proven; otherwise HOLD. Do not create authorization here.

## Task 3 — Durable authorization and atomic attempt lineage

**What:** Add one legacy-specific authorization lifecycle and consume it atomically with at most
one replacement D2 row.

**Steps:**
- [x] Add a backward-compatible D2 ledger schema migration preserving schema-2 rows and TEST
  behavior.
- [x] Persist authorization identity, parent/job/policy/reconciliation links, decision digest,
  legacy exposure, quote, combined exposure, count limit, status, timestamp, and software commit.
- [x] Require a valid APPROVE decision, current parent quarantine, unchanged unresolved reservation,
  valid linked audits, and revalidated quote to create authorization.
- [x] Under one ledger lock, consume one exact authorization, persist parent/auth IDs on one new D2
  attempt, and reject any second or different replacement.
- [x] Keep ordinary orchestration, retry, scheduler, and recovery paths unable to create a
  replacement automatically.
- [x] Do not execute these live authorization or attempt paths in this task.

## Task 4 — Contract and documentation

**What:** Document the exact quote scope, HOLD conditions, authorization lifecycle, replay handling,
and no-Generate boundary.

**Steps:**
- [x] Add `D2_PROD_LEGACY_REPLACEMENT_AUTHORIZATION_V1.md`.
- [x] Link it from the ambiguous recovery policy contract and explain direct credit unit semantics
  in the master-data dictionary.
- [x] Record that no CLI, MCP, queue, or Flow surface changed.
- [x] Update `CHANGELOG.md`.

## Task 5 — Targeted validation

**What:** Run the newly added quote, authorization, lineage, decision, and compatibility tests.

**Steps:**
- [x] Run focused `legacy_replacement`, Phase G, legacy recovery, production orchestrator, credit
  guard, modern PROD, and frozen TEST test groups.
- [x] Run lint, format, and Pyright for changed code.
- [x] Verify there is no live authorization, replacement attempt, reservation, Generate, or approval
  action.

## Task 6 — Canonical validation

**What:** Run the requested repository checks and canonical test suite.

**Steps:**
- [x] Run repository hygiene, docs/PII/link/site-doc checks, council-memory check, Ruff, format,
  Pyright, and the scoped canonical regression suite (291 passed, 1 skipped).
- [ ] Run the full-repository coverage sweep in CI; local scope follows the repository's guidance
  to avoid the known full-suite OOM on small development machines.
- [ ] Run the repository runtime doctor only in a configured implementation environment; do not
  treat the unrelated deployed build as proof that local un-deployed code is active.
- [x] Confirm no Flow surface was changed; real-Flow E2E is not applicable.
- [x] Report the current live quote as unavailable unless fresh authoritative cost data exists.

## Definition of done

- [x] `QUOTE_UNAVAILABLE` is fail-closed and never becomes a fabricated number.
- [x] A sanitized valid quote permits only a future explicit authorization, never Generate.
- [x] Authorization consumption creates at most one parent-linked D2 row atomically.
- [x] Scheduler/retry/orchestrator paths cannot create replacements automatically.
- [x] Existing legacy quarantine/reservation, modern PROD, and frozen TEST behavior pass regressions.
- [x] Documentation and requested validation are complete.
- [x] No live authorization, attempt, reservation, approval, or provider action occurred.

## Task 7 — Push and deploy

**What:** Publish only the validated source commit to `automation-v2`, then deploy it through the
documented V2 runtime operator with rollback and parity verification.

**Steps:**
- [ ] Confirm refreshed `origin/automation-v2` remains the expected parent and push is fast-forward.
- [ ] Commit the reviewed source and documentation changes without runtime/venv/unrelated files.
- [ ] Push only to `automation-v2`; verify remote head equals the commit.
- [ ] Update the VPS source checkout to that exact commit and run deployment preflight.
- [ ] Apply canonical deploy with rollback backup, verify manifest, runtime doctor, and all heads.
- [ ] Keep the legacy parent, reservation, and provider state untouched.

# D6 Standard Lite Replacement Generation Plan

> **For agentic workers:** Run `/gflow:status --feature d6-standard-lite-replacement-generation`
> to find the next unchecked task. Run the scoped repository checks before committing.

**Goal:** Quote the official Standard Veo 3.1 Lite NON_ULTRA rate, then use one durable,
parent-linked authorization and one pre-reserved Phase G attempt to perform at most one
controlled replacement generation.

**Architecture:** Keep quote semantics in `v2_runtime/legacy_replacement.py`; keep generation on
the existing Phase G browser and `ProfileLease` path. Add one exact-ID internal replacement
operator because the generic production operator rejects a prior Scene Job and cannot consume the
replacement authorization. Add a replacement-only pre-Generate reservation transition and preserve
the ordinary credit guard, retry, and orchestration paths. Keep V1 contract history immutable and
document the new behavior in V2.

**Predict verdict:** CAUTION — 6/10 (five-persona expanded review)

**Risk register:**

| Severity | Risk | Mitigation |
|---|---|---|
| Critical | A partial Sheets/D2/credit-ledger write could leave mismatched child state | Stable authorization-bound request key, exact replay, no delete/rollback, and fail-closed readbacks |
| Critical | Existing pre-Generate credit guard does not support an approved quote | Add a replacement-bound pre-reservation transition; preserve daily and live-balance limits |
| Critical | Generic orchestrator must not bypass quarantined-parent protections | Dedicated exact-ID operator; Phase G verifies consumed authorization and persisted child lineage |
| Critical | Flow confirmation amount may differ from the official quote | Approve at most once only on exact one-video amount match; otherwise stop with the reservation intact |
| High | Deployment rollback cannot undo live Sheet/ledger/provider actions | Complete source validation and rollback backup before any live write; perform each live phase once |

## File structure

### New files

```text
docs/superpowers/plans/2026-10-28-d6-standard-lite-replacement-generation/SCENARIO.md
  Edge cases and acceptance criteria for the controlled replacement operation
docs/ai-video-automation-v2/operations/D2_PROD_LEGACY_REPLACEMENT_AUTHORIZATION_V2.md
  Revised official-rate, child-lineage, and pre-Generate execution contract
scripts/operations/run_legacy_replacement.py
  Exact-ID internal operator for quote, authorization, one Phase G execution, and validation
tests/e2e/test_legacy_replacement_e2e.py
  Opt-in one-shot real-runtime verification for the exact controlled parent
```

### Modified files

```text
src/gflow_cli/v2_runtime/legacy_replacement.py
  Model-aware Standard Lite quote matching and V2 authorization semantics
src/gflow_cli/v2_runtime/production_execution.py
  Replacement-only exact-quote reservation before submission
src/gflow_cli/v2_runtime/phase_g.py
  Consumed replacement-capability validation and confirmation-price equality
scripts/operations/deploy_v2_runtime.py
  Allowlist the controlled replacement operator
schemas/master/PROVIDER_COST_MASTER.schema.json
  Clarify optional dimensions that are not universal pricing keys
tests/validation/test_legacy_replacement.py
tests/validation/test_production_execution.py
tests/validation/test_phase_g.py
tests/features/legacy_replacement.feature
tests/features/test_legacy_replacement_steps.py
  Targeted quote, reservation, lineage, replay, and confirmation tests
docs/ai-video-automation-v2/operations/D2_PROD_LEGACY_REPLACEMENT_AUTHORIZATION_V2.md
docs/ai-video-automation-v2/operations/VPS_BROWSER_RUNTIME.md
docs/ai-video-automation-v2/database/MASTER_DATA_DICTIONARY.md
CHANGELOG.md
  Operator contract and pricing-scope documentation
```

## Task 1 — Scenario and red tests

**What:** Bind the model-aware quote and one-shot confirmation rules before changing production
code.

**Files:** `SCENARIO.md`, `tests/features/legacy_replacement.feature`,
`tests/features/test_legacy_replacement_steps.py`, and focused validation tests.

**Steps:**
- [x] Record the five-persona CAUTION findings and mitigations.
- [x] Add red tests for 18-column Standard Lite NON_ULTRA scope, multiplier, non-pricing dimensions,
  wrong scope/source, and zero quote side effects.
- [x] Add red tests for replacement-bound pre-reservation, day-cap refusal, exact amount confirmation,
  one-time authorization, exact replay, and no second attempt.

## Task 2 — Model-aware quote and V2 contract

**What:** Quote only a unique official effective `CREDIT` row for the exact account/model/class/mode.

**Steps:**
- [x] Add explicit account pricing class and Standard Lite identity to quote config and digest.
- [x] Validate the provider account, active PROD profile, active model, provider, mode, and exact
  `Billing_Plan_Name=NON_ULTRA` without generic default-dimension matching.
- [x] Match the official Google Flow credits page and effective timestamps; reject duplicates,
  stale/future rates, non-CREDIT units, and non-official sources.
- [x] Multiply the per-generation rate by output count; do not require resolution/audio/quality or
  duration/aspect as rate keys.
- [x] Version authorization semantics as V2; preserve V1 behavior documentation.
- [x] Keep the live table's existing 18-column layout; use blank/null Quality_Tier and Resolution.

## Task 3 — Pre-Generate reservation and Phase G capability

**What:** Let only a consumed, exact replacement capability reserve its quote before Generate.

**Steps:**
- [x] Add a locked, replacement-only reservation transition from `PREPARED` to
  `PRE_GENERATE_RESERVED` before Generate; keep ordinary reservations unchanged.
- [x] Enforce the 50-credit daily cap, current free balance, exact attempt/account/project/job IDs,
  and no unresolved same-day provider attempt.
- [x] Permit exactly one subsequent Generate click; require Flow's one-video confirmation amount
  to equal the pre-reserved quote before one confirmation approval.
- [x] On a pre-click failure, release only the new quote reservation when click count is provably
  zero. After a click, preserve quote reservation for same-attempt reconciliation.
- [x] Require Phase G to verify the consumed authorization and D2 child lineage before any UI
  mutation; normal attempts and retries remain unable to use replacement capability.

## Task 4 — Dedicated operator and saga-safe lineage

**What:** Provide the only supported path for creating one replacement job pair and one linked D2
attempt without entering D7.

**Steps:**
- [x] Read the canonical 18-column snapshot, exact parent reservation, policy/reconciliation audits,
  fresh account/profile/model/Scene/Prompt identities, and current provider balance.
- [x] Use an authorization-bound stable idempotency key for one new Production/Scene Job pair; never
  modify the quarantined parent's rows.
- [x] Persist one V2 authorization binding parent jobs, replacement jobs, quote and digests; consume
  it atomically into exactly one D2 attempt before Phase G.
- [x] Reuse the existing Phase G settings/readback, baseline, submit observer, confirmation, and
  same-attempt output-validation path.
- [x] Define exact replay for each persisted boundary; a partial state must not allocate a new
  authorization, job pair, or attempt.
- [x] Stop after local exact-output validation; do not upload or write FINAL_OUTPUT/D7 state.

## Task 5 — Targeted validation

**Steps:**
- [x] Run replacement quote/decision, production credit guard, relevant Phase G, and legacy recovery
  regressions.
- [x] Run Ruff check, Ruff format check, and focused Pyright; do not install extra local dependencies or run
  full coverage.
- [x] Run the new pre-mutation Phase G async regression on Linux Python 3.11: 1 passed. The Windows Python 3.14 runner hung during asyncio Proactor socketpair setup before the test body.
- [x] Confirm `.test-tmp/` is pre-existing and excluded from staged changes.

## Task 6 — Commit, push, and deploy

**Steps:**
- [x] Run required repository hygiene/docs checks plus repo-wide Ruff and format verification.
- [ ] Commit only implementation, tests, contract, plan, and documentation changes.
- [ ] Verify `origin/automation-v2` is still exactly the expected parent and fast-forward push the
  validated commit only to that branch.
- [ ] Deploy the exact commit using `deploy_v2_runtime.py`; record rollback backup, run doctor, and
  prove local/origin/VPS/deployed head parity.

## Task 7 — Live pricing row, quote, and exposure

**Steps:**
- [x] Reconfirm the official Google price from
  `https://support.google.com/flow/answer/16526234?hl=en`; do not use Lower Priority pricing.
- [ ] Re-read the live cost tab. If no applicable row exists, create exactly one 18-column
  `PCT_######` row for `PRV_0001` / `PVA_0002` / `MDL_0001` / `NON_ULTRA` /
  `TEXT_TO_VIDEO` at the verified rate.
- [ ] Obtain a fresh deployed quote and validate the matched row, source version, input digest, and
  positive amount at or below 50.
- [ ] Re-read the 50-credit parent reservation and calculate combined exposure without changing it.
- [ ] Run the supported late parent evidence check once. If authoritative evidence appears, stop.

## Task 8 — Formal decision and one-shot execution

**Steps:**
- [ ] Run the deployed formal Replacement Attempt Decision from fresh state; stop unless every
  gate returns APPROVE.
- [ ] Create exactly one durable authorization, one replacement job pair, one D2 attempt, and one
  exact quote reservation with readbacks. Reuse the same identities for any safe exact replay.
- [ ] Pass Phase G readbacks for model, 9:16, one output, 8-second intent, project, Scene, Prompt,
  profile, baseline, evidence capture, and duplicate-click guard.
- [ ] Run the opt-in live verification once: one Generate maximum and one exact-quote confirmation
  approval maximum. Stop on mismatch, refusal, or ambiguity; never retry.
- [ ] Reconcile only the replacement attempt and validate its exact local output. Stop before D7.

## Definition of done

- [ ] Official Standard Lite NON_ULTRA rate is represented by one canonical 18-column row and a
  source-bound deployed quote.
- [ ] Targeted tests, Ruff, formatting, and Pyright pass.
- [ ] Local/origin/VPS/deployed heads match; runtime doctor and manifest pass.
- [ ] The parent remains quarantined with its 50-credit UNKNOWN reservation unchanged.
- [ ] At most one replacement authorization, job pair, attempt, reservation, Generate, and
  confirmation approval exist.
- [ ] The exact replacement attempt is reconciled and its output is validated, or the attempt is
  held with reservation intact and no retry.
- [ ] D7 and D8 have not started.

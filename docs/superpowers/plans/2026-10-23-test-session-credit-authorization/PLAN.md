# TEST D2 Session Credit Authorization Implementation Plan

> **For agentic workers:** Use this plan as the implementation checklist. Run
> the canonical repository checks before every commit.

**Goal:** Permit the explicitly authorized exact TEST session to reserve and
spend at most 40 new provider credits without changing historical rows or
weakening normal Phase G and PROD budget enforcement.

**Architecture:** Add a versioned TEST-only authorization record to the
lock-protected Phase G ledger, with a backwards-compatible migration that
preserves historical rows. Add explicit coordinator/ledger hooks at attempt
creation, before Generate, and at quote approval; persist quote reservations and
balance-delta reconciliation, and stop the session on unknown outcomes. No CLI
or MCP surface is added.

**Predict verdict:** CAUTION — all five reviews require a distinct, bounded,
auditable policy. Two STOP signals were for a generic bypass under the current
contract; this plan implements the separately versioned TEST-only exception
explicitly requested by the CEO authorization. The historical 50-credit
reservation remains untouched; potential historical billing remains UNKNOWN.

**Risk register:**

| Severity | Risk | Mitigation |
|---|---|---|
| Critical | Replaying or concurrently consuming the $40 authorization | One immutable session record, exact identity/root/expiry binding, and atomic consumption under the existing ledger lock |
| Critical | Unknown generation/billing followed by another attempt | Stop session, retain remaining allowance, require same-attempt reconciliation |
| Critical | Test-only exception leaking into PROD/default runs | Require exact test profile/account/project and explicit typed authorization at every enforcement point |
| Critical | Historical reservation being removed or rewritten | Schema migration preserves attempt rows and values; tests compare old rows before/after |
| High | Quote or actual balance delta differs | Record quote and delta separately; refuse quote above remaining allowance/balance; stop on unexplained delta |
| High | WAF or artifact recovery mistaken for a retryable failure | Preserve existing WAF/success/artifact-recovery hard stops |

---

## File structure

### New files

```text
docs/superpowers/plans/2026-10-23-test-session-credit-authorization/SCENARIO.md
  Scenario review and acceptance matrix
docs/ai-video-automation-v2/operations/D2_TEST_SESSION_BUDGET_V1.md
  Versioned exact-lane operator authorization contract
tests/validation/test_phase_g.py
  Budget, migration, replay, failure, and concurrency tests
tests/features/d2_test_session_budget.feature
tests/features/test_d2_test_session_budget_steps.py
  Bound offline policy scenarios
```

### Modified files

```text
src/gflow_cli/v2_runtime/phase_g.py
  Versioned ledger session data, authorization and execution guards
src/gflow_cli/v2_runtime/phase_g_run.py
  Pass the explicit internal TEST-session authorization through the coordinator
src/gflow_cli/v2_runtime/phase_g_live_flow.py
  Expose the adapter's already-selected auth profile for exact lane checks
tests/e2e/test_phase_g_d2_live.py
  Accept a complete session authorization pair for the controlled TEST path
docs/ai-video-automation-v2/architecture/PHASE_G_D2_CONTROLLED_EXECUTION.md
  Document the exact versioned TEST-only exception and unchanged default
```

## Task 1 — Ledger policy tests (test scaffold)

**What:** Add failing tests for session binding, historical-row preservation,
budget boundaries, replay, concurrent reservations, and unknown-stop behavior.

**Files:** `tests/validation/test_phase_g_test_session_budget.py`

**Steps:**

- [x] Create legacy ledger fixtures from the current schema and prior attempts.
- [x] Assert legacy rows and 50-credit daily budget remain unchanged after
  authorized schema upgrade.
- [x] Assert wrong account/profile/project/root, stale or reused authorization,
  PROD context, and malformed evidence fail before Generate.
- [x] Assert one in-flight attempt, quote <= session remainder and visible
  balance, separate quote/delta accounting, and no overrun under concurrent
  callers.
- [x] Assert unknown outcome retains the session hold and blocks later attempts.
- [x] Assert success, WAF, and artifact-recovery states close further execution.

**Tests created (red):**

- [x] Exact TEST authorization binding and expiry.
- [x] Migration with immutable historical rows.
- [x] Atomic cap reservation and concurrency.
- [x] Quote and provider balance reconciliation.
- [x] Unknown outcome, success, WAF, replay, and PROD isolation.

## Task 2 — BDD scaffold

**What:** Bind the offline authorization scenarios to the repository BDD
feature file; this policy has no new browser/UI surface, while the later D2 run
remains separately subject to the existing live E2E gate.

**Files:** `tests/features/d2_test_session_budget.feature`,
`tests/features/test_d2_test_session_budget_steps.py`

**Steps:**

- [x] Add scenarios for preserved historical reservation, bounded quote, and
  unknown-stop behavior.
- [x] Bind once from the feature step module and run the binding guard.

## Task 3 — Versioned authorization contract

**What:** Specify the narrow test-session exception without changing frozen
same-attempt recovery or forensic closure contracts.

**Files:** `docs/ai-video-automation-v2/operations/D2_TEST_SESSION_BUDGET_V1.md`

**Steps:**

- [x] Bind one session to the exact TEST profile, provider account, project,
  runtime root, starting ledger digest, expiry, and 40-credit ceiling.
- [x] Define one in-flight attempt, maximum attempt sequence, quote/balance
  reconciliation, and fail-closed stop states.
- [x] State that the old 50-credit unknown reservation remains intact and
  actual historical billing remains unknown.
- [x] State that ordinary TEST and all PROD executions keep the existing
  Bangkok daily 50-credit policy.

## Task 4 — Atomic ledger and coordinator implementation

**What:** Implement the smallest internal path that uses the new contract and
leaves existing callers on the current daily gate.

**Files:** `src/gflow_cli/v2_runtime/phase_g.py`,
`src/gflow_cli/v2_runtime/phase_g_run.py`

**Steps:**

- [x] Add backward-compatible schema reading/migration with exact preservation
  of the historical `attempts` values.
- [x] Add an immutable session authorization record, one-time consumption,
  in-flight binding, attempt sequencing, and exact TEST identity/root checks.
- [x] Enforce session budget at creation, immediately before Generate, and
  before approval using the actual observed one-video quote.
- [x] Record pre/post provider balances and reconciled delta separately from
  `accounted_credit_cost`.
- [x] Stop on unknown, WAF, success, artifact recovery, inconsistent balance,
  concurrency, or any authorization mismatch.
- [x] Keep non-authorized and PROD paths under the fixed daily 50-credit guard.

## Task 5 — Documentation

**What:** Document behavior and the operator boundary.

**Files:**
`docs/ai-video-automation-v2/architecture/PHASE_G_D2_CONTROLLED_EXECUTION.md`

**Steps:**

- [x] Link the versioned TEST-session policy.
- [x] Explain the only exception and preserved historical accounting.
- [x] Describe how unknown outcomes stop the session.

## Task 6 — Offline and canonical validation

**What:** Prove policy behavior without Flow or ledger mutation outside test
fixtures.

**Steps:**

- [x] Run new focused tests and affected Phase G/retry/forensic suites.
- [x] Run the applicable canonical `/gflow:check` gates, doc checks, and secret
  review.
- [ ] Review tracked diff and confirm runtime state, credentials, and `.runtime/`
  are excluded.
- [ ] Commit and push only after all required gates pass.

## Task 7 — Fresh paid TEST execution

**What:** After pushed code and fresh gates, run the user-authorized bounded
loop using current canonical Phase E/F5/Phase G flows.

**Steps:**

- [ ] Read live credits before each attempt; require the available quote to fit
  the remaining $40 session allowance.
- [ ] Export a fresh 34/34-table snapshot, rerun Phase E/F5, verify exact TEST
  identity/project, zero pending, and clean journal.
- [ ] Run one D2 with one Generate, at most one approval, output count one.
- [ ] Read credits after and durably reconcile quote and balance delta.
- [ ] Stop on D2 success, unknown outcome, WAF, cap exhaustion, or any failed
  safety gate; recover same media rather than generating again.

## Definition of done

- [ ] Scenario and plan acceptance criteria pass.
- [ ] Existing non-authorized 50-credit behavior and PROD behavior are unchanged.
- [ ] No historical ledger row/reservation is changed.
- [ ] `/gflow:check` and affected tests pass; commit is pushed.
- [ ] Live D2 result is reported as verified success, known failure, or unknown;
  no unverified generation is called done.

# Flow Pricing Profile Lease Fallback Implementation Plan

> **For agentic workers:** Run `/gflow:status --feature flow-pricing-profile-lease-fallback` to find the next unchecked task. Implement one task at a time. Run `/gflow:check` before the final commit.

**Goal:** Let the read-only Flow pricing inspector verify the existing authenticated profile when cookie decryption requires the Playwright fallback under the inspector's already-acquired profile lease.

**Architecture:** Thread the inspector-owned `ProfileLease` through `GoogleFlowAgentOnlyPort.open`, `verify_flow_profile`, the session fetch, and cookie snapshot. The fallback borrows that lease without releasing it; standalone callers continue to acquire their own lease. Validate that any borrowed lease is acquired for the same canonical profile before reading cookies or opening Playwright.

**Predict verdict:** CAUTION — 8/10. The auth boundary change is localized, but lease ownership and lifetime must be explicit and covered by regression tests.

**Confirmed bug assessment:** `CONFIRMED-BUG`, based on line-level call flow and a live same-profile verifier result. The standalone verifier returned `AUTHENTICATED` for the expected account after Playwright cookie fallback and migrated-host authentication; the inspector's existing outer lease makes that fallback's second lease acquisition fail.

**Risk register:**

| Severity | Risk | Mitigation |
|---|---|---|
| High | An unacquired, released, or different-profile lease is mistaken for ownership. | Add and test `ProfileLease.assert_held_for`; validate before cookie access and fallback launch. |
| High | Borrowed lease is released while the Playwright fallback still has the profile open. | The fallback closes its context before returning; only the inspector owns/releases the lease, in its existing `finally` after browser teardown. |
| Medium | Other standalone callers regress. | Keep the default path self-acquiring; pass a borrowed lease only from the pricing inspector. |
| Medium | Real Flow behaviour changes or the report leaks auth data. | Run the existing BDD-bound live E2E and assert account/project parity, sanitized output, and zero mutation counters. |

---

## File structure

### New files

```text
docs/superpowers/plans/2026-10-27-flow-pricing-profile-lease-fallback/SCENARIO.md
  Confirmed root cause and BDD scenario.
docs/superpowers/plans/2026-10-27-flow-pricing-profile-lease-fallback/PLAN.md
  This implementation plan and risk mitigations.
```

### Modified files

```text
src/gflow_cli/profile_lease.py
  Expose a safe check that a lease is held for a canonical profile.
src/gflow_cli/auth/cookies.py
  Borrow the caller's lease in the Playwright cookie fallback when supplied.
src/gflow_cli/auth/verification.py
  Propagate and validate an optional caller-owned lease.
src/gflow_cli/v2_runtime/live_flow.py
  Pass the optional lease through agent-port session verification.
src/gflow_cli/v2_runtime/flow_pricing_inspector.py
  Pass its already-held lease into the agent-only port.
tests/test_profile_lease.py
  Cover held-profile validation.
tests/auth/test_cookies.py
  Cover borrowed and self-acquired fallback behavior.
tests/auth/test_verification.py
  Cover lease propagation into the session fetch.
tests/validation/test_flow_pricing_inspector.py
  Cover propagation of the acquired inspector lease.
tests/features/vps_flow_pricing_inspection.feature
  Name the regression scenario at the lease/fallback root cause.
```

## Task 1 — Add failing regression coverage

**What:** Prove that fallback can borrow a valid same-profile lease and rejects a stale or unrelated one.

**Steps:**
- [x] Add `ProfileLease` ownership assertions for acquired, released, and wrong-profile cases.
- [x] Add cookie-fallback test asserting no second lease is acquired when the caller already owns the profile.
- [x] Add propagation tests through verifier, agent port, and inspector.
- [x] Rename the existing BDD scenario to bind the live inspection to this failure mode.

**Tests created (red):**
- [x] Borrowed fallback uses the caller's lease and closes the context before returning.
- [x] Standalone fallback retains its own lease acquisition/release.
- [x] Wrong or released borrowed lease is rejected before cookie access/browser launch.
- [x] Inspector passes its acquired lease into the agent port, which passes it to verification.

## Task 2 — Implement the minimal lease handoff

**What:** Thread and validate the lease; preserve all existing standalone auth paths and fail-fast contention behaviour.

**Steps:**
- [x] Implement `ProfileLease.assert_held_for` using canonical path identity and active FD state.
- [x] Add optional borrowed-lease parameters to the internal cookie/session verification path.
- [x] Keep ownership in the inspector; the fallback must not release a borrowed lease.
- [x] Preserve the Chrome marker gate, cookie redaction, HTTP origin scope, and context cleanup.

**Tests:**
- [x] Targeted lease, auth verifier, cookie fallback, and inspector unit suites (133 passed).

## Task 3 — Validate the production path

**What:** Run canonical local checks and the existing live BDD test against the same deployed profile.

**Steps:**
- [x] Run relevant repository checks and targeted tests (full offline coverage suite: 5,937 passed, 30 skipped; one uncached-wheel integration case deselected).
- [ ] Push the single validated commit to `origin/automation-v2` with normal fast-forward semantics.
- [ ] Deploy through `scripts/operations/deploy_v2_runtime.py`; retain and verify its rollback backup.
- [ ] Run the canonical application runtime doctor and verify local/origin/VPS/deployed SHA parity.
- [ ] Run `tests/e2e/test_flow_pricing_inspector_e2e.py` against the existing `gflow-prod` profile.
- [ ] Stop if identity/project mismatch or any unexpected provider mutation is reported.

**Tests:**
- [ ] BDD-bound live E2E confirms exact account and project and zero provider mutations.

## Definition of done

- [ ] All regression tests pass and the existing production BDD scenario is green.
- [ ] `/gflow:check` is green; the BDD live run is recorded separately.
- [ ] One commit is pushed and deployed using the canonical workflow.
- [ ] Runtime doctor passes and local/origin/VPS/deployed heads match.
- [ ] The pricing inspector reports the expected account/project and no provider mutation.
- [ ] No credentials, profile resets, new identities, settings changes, or pricing-master writes occur.

**Commit constraint:** Keep all implementation and validation artifacts in one final commit, as required by the current deployment task.

# Explicit D2 Retry Authorization Implementation Plan

> **For agentic workers:** Run `/gflow:status --feature explicit-d2-retry` to
> find the next unchecked task. Run `/gflow:check` before every commit.

**Goal:** Permit one exact, explicitly authorized new D2 attempt after a
terminal `UNEXPECTED_MULTI_OUTPUT` failure while retaining default denial and
all credit/provider safety controls.

**Architecture:** Add a typed authorization path at the existing Phase G ledger
creation boundary. Persist one-time consumption on the new row under the
existing cross-process lock; keep normal creation unchanged. Document the
versioned retry contract and plumb an optional paired authorization through the
existing paid E2E harness without enabling its opt-in.

**Predict verdict:** CAUTION — 8.8/10. Addressed by exact predecessor binding,
durable consumption, strict predecessor state checks, and regression tests.

**Risk register:**

| Severity | Risk | Mitigation |
|---|---|---|
| Critical | Replay could enable repeated paid attempts | Atomic consumed marker on new row; ledger-wide replay rejection |
| Critical | Authorization could bypass unresolved/WAF/success stops | Validate exact terminal state and Generate count under ledger lock |
| High | Retry could exceed daily budget | Check current Bangkok-day headroom and retain existing quote approval gate |

---

## File structure

### New files

```text
docs/ai-video-automation-v2/operations/D2_EXPLICIT_RETRY_AUTHORIZATION_V1.md
  Versioned operational and safety contract
docs/superpowers/plans/2026-10-22-explicit-d2-retry/SCENARIO.md
  Risk-ranked scenario coverage
```

### Modified files

```text
src/gflow_cli/v2_runtime/phase_g.py
  Add ledger-locked exact one-time authorization path
src/gflow_cli/v2_runtime/phase_g_run.py
  Pass typed authorization through the existing executor API
tests/validation/test_phase_g.py
  Cover default deny, exact binding, stops, replay, uniqueness and budget
tests/e2e/test_phase_g_d2_live.py
  Accept paired optional operator authorization inputs; paid opt-in unchanged
AGENTS.md, architecture docs, CHANGELOG.md
  Route operators to the versioned contract and explain the narrow exception
```

## Task 1 — Offline regression coverage

**What:** Exercise exact authorization and all fail-closed predecessor cases.

**Steps:**

- [x] Add focused ledger tests for default denial, exact predecessor, mismatches,
      WAF/success/unresolved stops, duplicate IDs, replay, and credit accounting.
- [ ] Run focused tests and relevant Phase G offline tests.

## Task 2 — Ledger and E2E authorization seam

**What:** Add a typed exact-predecessor path and durable one-use consumption.

**Steps:**

- [x] Keep `create_attempt()` default behavior unchanged.
- [x] Validate the predecessor and budget under the existing ledger lock.
- [x] Persist consumption only on the new row; reject replay.
- [x] Pass paired optional authorization inputs through the existing E2E path.

## Task 3 — Contract and checks

**What:** Document the exception and validate the implementation without running
the paid/live D2 path.

**Steps:**

- [x] Add a versioned retry contract and architecture/AGENTS routing.
- [x] Update the unreleased changelog.
- [ ] Run Ruff, format, Pyright, relevant tests and diff/secret checks.
- [ ] Commit and push the focused change.

## Definition of done

- [ ] Default second D2 remains denied.
- [ ] Exactly one authorized distinct row may be created per ledger.
- [ ] Existing one-Generate, one-approval, output-count, WAF and daily budget
      controls remain intact.
- [ ] Offline checks pass; paid/live D2 remains unrun.
- [ ] Local and origin branch heads match after push.

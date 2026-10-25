# Legacy PROD D2 Attempt Recovery Compatibility Plan

> **For agentic workers:** This task is constrained to one coherent implementation commit on top
> of `67bbdf63ee6244bac0e8a300d889e1a22c99d0cf`. Never inspect or reconcile the live attempt.

**Goal:** Let a pre-contract PROD D2 attempt reach a separate evidence classifier through a
provenance-bound canonical join, without rewriting historical evidence or weakening modern PROD or
TEST recovery.

**Architecture:** A pure legacy module builds an immutable envelope from already-read canonical
snapshots, classifies evidence conservatively, and emits an idempotent audit event to a separate
runtime file. Existing PROD/TEST classifiers and historical D2/credit/Sheets rows remain unchanged.

**Predict verdict:** CAUTION — confidence 6/10. Multi-source snapshots are not atomic; each source
must retain its own digest and observation time, and any identity or event-order uncertainty stays
ambiguous.

**Risk register:**

| Severity | Risk | Mitigation |
|---|---|---|
| Critical | A plausible join is treated as historical proof | Unique canonical keys and explicit per-field provenance; conflicts fail closed |
| Critical | Missing Generate time is inferred from an update timestamp | Keep it `UNAVAILABLE`; require direct action time for timestamp-dependent evidence |
| Critical | Missing provider evidence releases a reservation | Legacy ambiguity leaves reservation unchanged; only existing positive modern refusal proof can classify as not submitted |
| High | Separate sources represent different observations | Carry a digest and observation time for each source; do not claim an atomic snapshot |
| High | Audit changes old evidence | Write a separate locked, atomic, idempotent audit file |
| Medium | Internal API creates public surface drift | No CLI, MCP, queue, or worker-codec changes |

## File structure

### New files

```text
src/gflow_cli/v2_runtime/phase_g_prod_legacy_reconciliation.py
  Legacy identity envelope, provenance model, conservative classifier, and separate audit store.
tests/validation/test_phase_g_prod_legacy_reconciliation.py
  Sanitized deterministic legacy fixture and adversarial join/evidence matrix.
tests/features/prod_legacy_reconciliation.feature
tests/features/test_prod_legacy_reconciliation_steps.py
  Offline BDD scenarios binding the legacy recovery contract.
docs/ai-video-automation-v2/operations/D2_PROD_LEGACY_ATTEMPT_RECOVERY_V1.md
  Versioned compatibility contract.
```

### Modified files

```text
AGENTS.md
  Route legacy PROD reconciliation to its separate contract.
docs/ai-video-automation-v2/architecture/PHASE_G_D2_CONTROLLED_EXECUTION.md
docs/ai-video-automation-v2/operations/PRODUCTION_SCENE_ORCHESTRATOR_V2.md
  Explain the read-only legacy envelope and HOLD interpretation.
CHANGELOG.md
  Record the new internal compatibility path.
```

## Implementation and validation tasks

- [ ] Add the red deterministic matrix and offline Gherkin binding.
- [ ] Implement source snapshot validation, strict unique joins, per-field provenance, and the
  legacy-only classifier.
- [ ] Add the separate append-only audit store; prove it does not write the old D2 or credit files.
- [ ] Publish the versioned contract and update runtime/operator pointers.
- [ ] Run targeted legacy, existing PROD reconciliation, orchestrator, credit guard, and Phase G
  tests; run full canonical checks before creating the one requested commit.
- [ ] Verify the exact deployment branch before pushing; deploy only the validated commit and run
  runtime doctor. Do not reconcile the live attempt.

## Definition of done

- [ ] Identity is reconstructed only through exact canonical joins and provenance is retained for
  each field.
- [ ] No historical field is fabricated or written back.
- [ ] Missing action time never becomes `PROVEN_NOT_SUBMITTED`; ambiguity preserves the reservation.
- [ ] The sanitized fixture produces `AMBIGUOUS_AFTER_RECONCILIATION` and reports premature
  `RUNNING` without changing job rows.
- [ ] Modern PROD and frozen TEST behavior remain unchanged.
- [ ] Required validation passes before the single commit and deployment.

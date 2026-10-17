# D5/D7 Offline Build-ahead Implementation Plan

> D5 and D7 live execution remain gated by the overnight mission. This plan implements only safe offline preparation.

**Goal:** Prepare a production-only non-submitting F5 invocation contract and a durable-shaped, idempotent Scene Job lifecycle core without live Sheets, Drive, or Flow submission.

**Architecture:** D5 resolves a fresh, validated production snapshot and calls the existing prepare_live_flow() F5 entrypoint directly with the verified production auth profile, expected Google identity, and dedicated project. D7 uses a distinct transaction coordinator and injected atomic store; its in-memory store is test-only. No D7 code calls Phase G or Generate. Runtime configuration rejects overlapping state/temp/log roots before any cleanup.

**Predict verdict:** CAUTION — Architect 7/10, Security CAUTION, UX review unavailable without implementation, Performance 8/10, Devil's Advocate CAUTION. All reviews support offline work with the boundaries below.

**Risk register:**

| Severity | Risk | Mitigation |
|---|---|---|
| Critical | D5 accidentally resolves TEST identity or executes Phase G | Require production runtime, active PROD account/profile, explicit expected email, fresh exact Phase E/approval match; call F5 only |
| Critical | Sheets snapshots are stale or ambiguous | Require a recent, spreadsheet-identified snapshot and exactly one current APPROVED prompt matching fresh Phase E |
| Critical | D7 replay duplicates production or scene jobs | Store atomically claims request key + canonical digest; mismatched digest is a conflict |
| Critical | Premature COMPLETED or cleanup loses media | Explicit state guards require verified persistent output before cleanup/completion |
| Critical | Temporary cleanup can delete browser/recovery state | Require pairwise disjoint roots and reject reserved-root descendants |
| High | Live adapters are absent and offline fixtures could be mistaken for production | Provide no production persistence implementation; label in-memory store test-only and report live gates waiting |

---

## File structure

### New files

    scripts/operations/vps_flow_presubmit.py
      Operator-only production F5 wrapper over a recent data snapshot.
    src/gflow_cli/v2_runtime/production_jobs.py
      Idempotent transaction coordinator, state guards, and test-only in-memory store.
    tests/validation/test_vps_flow_presubmit.py
      Snapshot, production identity, approval, redaction, and non-submit boundary tests.
    tests/validation/test_production_jobs.py
      Idempotency, append-only attempts, state transitions, and remote-verification gates.
    docs/superpowers/plans/2026-10-18-d5-d7-offline-build-ahead/SCENARIO.md
      Pre-implementation risk scenarios.
    docs/superpowers/plans/2026-10-18-d5-d7-offline-build-ahead/PLAN.md
      This implementation plan.

### Modified files

    src/gflow_cli/v2_runtime/config.py
      Reject overlapping runtime, temporary, and log roots.
    scripts/validation/v2_runtime_doctor.py
      Require production runtime configuration, valid expected deployment SHA, writable non-symlink roots, and functional uv.
    tests/validation/test_v2_runtime.py
      Cover disjoint-root configuration.
    tests/validation/test_v2_runtime_doctor.py
      Cover production and root/command checks.
    docs/ai-video-automation-v2/operations/VPS_BROWSER_RUNTIME.md
      Document D5 wrapper invocation and current live blockers.
    docs/ai-video-automation-v2/operations/PHASE_F5_AGENT_ONLY.md
      Record offline D5/D7 implementation boundaries.
    CHANGELOG.md
      Record the V2 operator and transaction foundation.

## Task 1 — Guard runtime roots and doctor claims

**What:** Make cleanup roots pairwise disjoint and ensure the readiness doctor cannot report a non-production or unverifiable runtime as production-ready.

**Steps:**
- [ ] Add failing tests for equal/nested state, temp, and log roots.
- [ ] Add failing doctor tests for non-production environment, invalid/missing expected SHA, symlink/unwritable roots, and failed uv.
- [ ] Implement the smallest root-overlap guard and truthful doctor checks.
- [ ] Confirm the pinned deployed doctor remains separate from the app source.

## Task 2 — Production F5 resolver and operator wrapper

**What:** Resolve a current PROD snapshot, rerun Phase E, require the exact single approved prompt, and call only F5 with explicit verified Google identity.

**Steps:**
- [ ] Define a bounded JSON snapshot format with spreadsheet ID and UTC read timestamp.
- [ ] Require explicit scene/profile IDs and reject stale, malformed, non-PROD, inactive, or TEST-linked records.
- [ ] Verify exactly one current APPROVED prompt and exact equality with all Phase E fields/hashes.
- [ ] Require nonempty expected email from environment without printing it.
- [ ] Call prepare_live_flow() only and always report Generate clicked = 0 and credits used = 0.
- [ ] Add fake-catalog tests proving all pre-browser failures occur before the F5 call.

## Task 3 — Idempotent D7 transaction contracts

**What:** Add a separate job coordinator with an atomic-store interface and a test-only in-memory implementation.

**Steps:**
- [ ] Add canonical request digest and same-key replay/conflict behavior.
- [ ] Claim Production Job IDs and append-only Scene Job attempt numbers using current schema constraints.
- [ ] Enforce only schema-supported states and valid transitions.
- [ ] Require exact provider execution identity and verified persistent media evidence before COMPLETED.
- [ ] Require remote hash equality before cleanup; retain local evidence on any remote-verification failure.
- [ ] Add replay, conflict, lifecycle, cleanup, and fake storage/provider contract tests.
- [ ] Do not implement live Sheets or Drive adapters and do not call Flow/Phase G.

## Task 4 — Docs and verification

**What:** Document the wrapper, offline transaction boundary, human handoff prerequisites, and current blockers.

**Steps:**
- [ ] Record D5 implemented offline; live D5 waits for D4 account/profile/project verification.
- [ ] Record D6/D7 live holds accurately; do not change Sheets or create job rows.
- [ ] Run focused tests, schema checks, Ruff, formatting, Pyright, build, hygiene, and full /gflow:check.
- [ ] Inspect diff/status and commit only after all gates pass.

## Definition of done

- [ ] D5 wrapper has no Generate/Phase G method and rejects absent or mismatched production identity before browser launch.
- [ ] D7 same-key replay creates no duplicate jobs or attempts; a changed request digest fails closed.
- [ ] A Scene Job cannot become COMPLETED without exact output attribution, persistent storage verification, matching hashes, and successful cleanup.
- [ ] Root cleanup cannot target browser/recovery data.
- [ ] No live Sheets rows, Drive files, Flow submissions, production service changes, or protected systems are modified.
- [ ] Tests and /gflow:check pass; working tree is clean after the focused commit.

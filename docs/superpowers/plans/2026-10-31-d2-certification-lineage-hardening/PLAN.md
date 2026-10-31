# D2 Certification Lineage Hardening Implementation Plan

> **For agentic workers:** Keep production state read-only. Run focused tests and the repository check workflow before any commit.

**Goal:** Support an explicitly identified fresh D2 certification cycle for the same workload, with concurrency-safe job allocation and a validated canonical per-run credit cap.

**Architecture:** Keep workload identity in the existing Phase E/D2→D6 fingerprint and `Scene_Set_SHA256`. Add explicit run intent to the production request idempotency key; retain `Production_Run_No` as the persisted cycle number, allocated as `max + 1` under the existing Sheets writer lock and uniqueness rules. Bind a fresh canonical quote to the PROD execution context and enforce it in the shared credit guard. Require the fixed PROD browser-state root in the production operations path.

**Predict verdict:** CAUTION — source and store paths are identified; real paid Phase G verification is prohibited by this task.

**Risk register:**
| Severity | Risk | Mitigation |
|---|---|---|
| Critical | Duplicate concurrent requests could create duplicate jobs | Enforce request-key and run-number uniqueness during append; replay exact same intent |
| Critical | Generic daily ceiling could exceed canonical per-run quote | Persist quote capability on the PROD ledger row and cap the observed confirmation quote |
| High | Default profile home resolves a different profile | Pin/validate exact `GFLOW_CLI_HOME` before any production browser read |
| High | Paid live verification cannot run | Keep Generate disabled in this task and report the named blocker |

## File structure

### Modified files

- `scripts/operations/run_production_scene.py` — explicit run intent, canonical quote, profile root guard.
- `src/gflow_cli/v2_runtime/google_sheets_production_jobs.py` — append uniqueness for request key, run number, and Scene Job attempt.
- `src/gflow_cli/v2_runtime/production_execution.py` — persist and enforce PROD reservation cap.
- `src/gflow_cli/v2_runtime/phase_g.py` — pass the cap through the existing reservation checkpoint.
- `scripts/operations/inspect_flow_pricing_read_only.py` — pin the configured PROD profile root.
- `scripts/validation/v2_runtime_doctor.py`, `docs/ai-video-automation-v2/operations/VPS_BROWSER_RUNTIME.md` — make source-aware doctor invocation and import diagnostics explicit.
- Focused tests under `tests/validation/`.

## Task 1 — Focused regression tests (test scaffold)

**What:** Add failing tests for explicit run intent, concurrent-safe uniqueness, unchanged workload identity, quote cap, and profile root validation.

**Steps:**
- [ ] Same intent replays one job; new intent creates a new Production Job and Scene Job.
- [ ] Run number and Scene_Set_SHA256 are stable/monotonic as required.
- [ ] Concurrent allocation uniqueness conflicts fail closed.
- [ ] Missing quote prevents mutation; quote above 10 is rejected; the 50-credit daily ceiling cannot enlarge the run cap.
- [ ] Wrong profile root fails before browser/profile access.

## Task 2 — Minimal implementation

**What:** Implement only the identity and guardrail changes covered by Task 1.

**Steps:**
- [ ] Use `Production_Run_No` for persisted certification-cycle identity.
- [ ] Include a validated explicit run-intent token in the request key.
- [ ] Enforce uniqueness at the Sheets append boundary.
- [ ] Quote from the current schema-validated snapshot before job allocation and bind its digest/version/amount to PROD execution context.
- [ ] Keep the D2→D6 compatibility fingerprint untouched.
- [ ] Pin the fixed gflow-prod profile root and improve doctor source-path diagnostics/docs.

## Task 3 — Validation and rollout

**Steps:**
- [ ] Run targeted tests, Ruff, and Pyright for affected paths.
- [ ] Run `/gflow:check` before the commit.
- [ ] Do not run the paid Flow E2E; the explicit no-Generate instruction is the blocker.
- [ ] Commit, push to `origin/automation-v2` only if all required checks pass and remote ancestry is safe.
- [ ] Deploy only if deployment doctor, profile identity, account/project pinning, and read-only asset baseline pass.
- [ ] Verify exact head parity and keep all records untouched.

## Definition of done

- [ ] Focused identity/quote/profile tests pass.
- [ ] Ruff and Pyright pass on touched source.
- [ ] No Sheets, job, D2, reservation, or Flow mutation occurred.
- [ ] Runtime doctor and all deployment/readiness gates pass before reporting deployment PASS.
- [ ] Report `ALLOCATE_AT_MUTATION_TIME` for next Production/Scene Job IDs; no next run is created.

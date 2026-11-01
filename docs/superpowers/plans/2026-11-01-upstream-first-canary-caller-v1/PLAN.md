# Upstream-first Canary Caller V1 Implementation Plan

> **For agentic workers:** Offline implementation only. Do not invoke Google Flow or a real
> upstream subprocess. Run the repository check before every commit.

**Goal:** Add a canary-only durable caller that binds existing Production/Scene Jobs, canonical
prompt and quote policy, an exact reservation, and a one-shot upstream adapter invocation, then
validates and stores a successful artifact without claiming lineage the provider did not return.

**Architecture:** Add a small caller around existing canonical sources, job coordinator, shared
credit guard, adapter, media validator, and Drive storage. Extend the shared credit guard only with
upstream-specific one-shot/reservation transitions; keep `ProductionOrchestrator`, Phase G default
routing, D2, D6, and n8n unchanged. Persist a canary-specific result state; never fabricate a
provider execution ID or mark an ordinary Production Job complete from upstream JSON alone.

**Predict verdict:** CAUTION — 6.8/10. See `PREDICT.md` and `SCENARIO.md`.

**Risk register:**

| Severity | Risk | Mitigation |
|---|---|---|
| Critical | Replay after process death could spend twice | Atomically persist one invocation-start in the shared locked ledger before child launch; replay returns HOLD |
| Critical | Reservation and one-shot ledger can drift | Put the upstream invocation identity and reservation transitions in `ProductionCreditGuard`'s existing ledger; do not add a second credit ledger |
| Critical | Upstream result lacks provider execution/account/project readback | Keep canary result distinct; do not fabricate `GeneratedSceneAsset` or complete ordinary Production Jobs |
| High | Parent and child contend for the same profile lease | Caller accepts verified identity evidence and starts the child only after parent verification resources are closed |
| High | Long blocking child occupies async worker | Keep caller synchronous and isolated from the asyncio production orchestrator |
| High | Unknown post-launch result loses billing exposure | Persist UNKNOWN/HOLD and retain quote reservation for same-invocation reconciliation |
| High | Installed CLI version does not prove pinned source | Persist upstream source pin; require artifact hash verification before the later live canary |

## File structure

### New files

```text
src/gflow_cli/v2_runtime/upstream_canary.py
  Narrow typed caller and canary-only outcome mapping.
tests/validation/test_upstream_canary.py
  Durable-state, quote/reservation, replay, outcome, validation and storage tests with fakes.
tests/features/upstream-first-canary-caller-v1.feature
  Offline BDD scenarios for at-most-once replay and unknown/success handling.
tests/features/test_upstream_first_canary_caller_v1_steps.py
  Binding for the offline canary caller scenarios.
docs/superpowers/plans/2026-11-01-upstream-first-canary-caller-v1/PREDICT.md
docs/superpowers/plans/2026-11-01-upstream-first-canary-caller-v1/SCENARIO.md
docs/superpowers/plans/2026-11-01-upstream-first-canary-caller-v1/PLAN.md
  Adversarial review, scenario matrix, and implementation checklist.
```

### Modified files

```text
src/gflow_cli/v2_runtime/production_execution.py
  Add isolated upstream-canary reservation and one-shot transitions to the existing shared ledger.
docs/ai-video-automation-v2/operations/UPSTREAM_GFLOW_ADAPTER_V1.md
  Document the caller boundary, replay semantics, and non-finalization limitation.
CHANGELOG.md
  Record the offline canary caller without changing production routing.
```

No public CLI, MCP, schema, n8n, production deployment, browser, or provider surface is added.

## Task 1 — Persist the adversarial review and BDD scenarios

**What:** Add offline behavioral scenarios before production code.

**Files:** `PREDICT.md`, `SCENARIO.md`, feature file, BDD binding.

**Steps:**
- [x] Record five-persona CAUTION findings and named mitigations.
- [x] Cover mismatched prompt/profile/project, quote ceilings, duplicate/concurrent replay,
  checkpoint ordering, each normalized outcome, and artifact storage without fabricated lineage.
- [x] Run the BDD binding against the fake-only caller; the scenario passes after implementation.

## Task 2 — Add the durable upstream reservation lifecycle

**What:** Extend `ProductionCreditGuard` with upstream-specific states while leaving Phase G
transition behavior unchanged.

**Files:** `production_execution.py`, targeted tests.

**Steps:**
- [x] Atomically bind canary invocation ID, request key, job IDs, prompt SHA, profile/account/project,
  exact config, quote/reservation, upstream pins, and unique output path.
- [x] Require fresh positive quote no greater than the CEO per-run ceiling, provider remaining quota,
  or shared daily budget; reserve exactly the quote.
- [x] Provide a one-time durable READY → INVOCATION_STARTED transition before subprocess launch.
- [x] Persist allowlisted result fields/digests only; retain reservations for unknown or generated-but-
  undownloaded outcomes; release only on a result proving pre-submit failure.
- [x] Make same-key replay return the same row and concurrent same-key callers fail closed at launch.
- [x] Add tests for concurrent callers, crash/replay, exact reservation, daily budget,
  and compatibility with existing Phase G guard operations.

## Task 3 — Implement the narrow caller bridge

**What:** Compose canonical input and injected side-effect ports without adding browser logic or
connecting the caller to routine production routing.

**Files:** `upstream_canary.py`, tests.

**Steps:**
- [x] Fresh-read and bind one approved prompt/hash, locked scene, generation profile, exact account
  and project policy, effective model/aspect/duration/count, and validated quote.
- [x] Claim/reuse exactly one Production Job and Scene Job from the caller's stable request key.
- [x] Persist canary invocation identity/checkpoint and exact quote reservation before invocation.
- [x] Require evidence that the parent released the child's profile lease; invoke the injected adapter at most
  once synchronously, outside an asyncio event loop.
- [x] Map adapter outcomes to pre-submit failure, canary artifact pending, or UNKNOWN/HOLD; never
  retry or allocate another invocation on replay.
- [x] Test fake adapter outcomes, malformed results, and replay after durable transitions.

## Task 4 — Reuse existing media validation and Drive persistence

**What:** Continue only a successful adapter result through the existing validator and storage
contract; never overstate provider lineage.

**Files:** `upstream_canary.py`, tests.

**Steps:**
- [x] Require the exact invocation's staged MP4 and validated expected aspect/duration.
- [x] Call the existing full MP4 validator and `persist_and_verify`/Drive adapter with a stable
  invocation-derived logical key.
- [x] Persist remote file ID, verification state, and local/remote SHA-256 equality.
- [x] Mark the canary artifact result as persisted, but keep normal Production Job completion blocked
  pending separately defined provider execution lineage; do not synthesize that identity.
- [x] Test same-invocation artifact recovery after the success result is durably recorded, plus
  existing upload acknowledgement and remote hash failure behavior.
  recovery without another adapter call.

## Task 5 — Document the operator and lineage boundary

**What:** Explain one-shot state, reservation semantics, artifact handling, and what the result does
not prove.

**Files:** adapter operations doc, `CHANGELOG.md`, plan.

**Steps:**
- [x] Document explicit invocation authorization, replay behavior, unknown hold, lease lifecycle, and
  artifact integrity requirements.
- [x] State that no public CLI/MCP surface or default engine change exists.
- [x] State that Canary 01 must verify the installed upstream artifact source pin before real use.

## Task 6 — Validate, commit, and push

**What:** Run offline regression and canonical checks on Linux, commit once, and normal-push the
feature branch.

**Steps:**
- [x] Run caller/adapter/job/idempotency/credit/orchestrator/artifact/Drive targeted tests.
- [x] Run broad offline suite, Ruff, format, Pyright, repo hygiene, and docs checks in the isolated
  VPS validation checkout.
- [x] Confirm no live browser, provider call, n8n change, D2/D6 change, or default-engine switch.
- [x] Review the exact diff and complete `/gflow:check` before commit.
- [ ] Refresh the remote and push by normal fast-forward to the same-named feature branch; do not
  deploy.
- [ ] Refresh the remote and push by normal fast-forward to the same-named feature branch; do not
  deploy.

## Definition of done

- [x] Durable exact quote reservation and one-shot invocation state survive restart/replay.
- [x] All requested fake outcomes and concurrent/replay cases pass offline.
- [x] Artifact validation, Drive persistence, re-download, and SHA equality use existing components.
- [x] Normal production completion is not fabricated from incomplete upstream lineage.
- [x] Broad offline suite, Ruff, format, Pyright and documentation gates pass on Linux.
- [ ] One local commit is pushed by normal fast-forward; production runtime remains unchanged.
- [ ] Google Flow Generate = 0 and provider credits spent = 0 for this implementation task.

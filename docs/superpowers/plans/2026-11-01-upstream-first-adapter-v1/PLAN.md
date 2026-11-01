# Upstream-first Video Adapter V1 Implementation Plan

> **For agentic workers:** This plan is offline-only. Do not connect it to the PROD orchestration path. Run `/gflow:check` before every commit.

**Goal:** Add a pinned, offline-testable subprocess adapter for upstream `gflow video t2v` without changing production routing or the existing Phase G/D2/D6 controls.

**Architecture:** Place a narrow adapter and normalized V1 result contract in `v2_runtime`. The upstream `v0.92.1` CLI JSON does not expose account/project/execution/D2 identity, so the adapter will remain a separate experiment and will not implement `SceneGenerationPort` or return `GeneratedSceneAsset`. The caller retains durable authorization, Production/Scene Job idempotency, credit accounting, reconciliation, artifact validation, and Drive finalization.

**Predict verdict:** CAUTION — the upstream release contract is pinned and inspectable, but its result schema cannot safely satisfy production lineage; production wiring and live verification remain prohibited.

**Risk register:**

| Severity | Risk | Mitigation |
|---|---|---|
| Critical | A timeout after child launch may follow a billed submit | Map uncertain outcomes to `POST_SUBMIT_UNKNOWN`; never retry in the adapter |
| Critical | Upstream JSON lacks production lineage IDs | Keep a distinct result type; never synthesize provider execution/account/project/D2 evidence |
| Critical | Ambient executable or caller data could change the command | Require absolute executable, strict typed inputs, argv array, and `shell=False` |
| High | JSON schema or upstream version drifts | Pin release/tag commit and strictly validate version and result fields |
| High | Production profile/account/project may be mismatched | Require identifiers and retain identity checks in the external production guard; adapter stays disconnected |
| High | Local output may be absent, outside staging, or overwritten | Require output containment, nonexistence before launch, then verify the result file |

## File structure

### New files

```text
src/gflow_cli/v2_runtime/upstream_gflow_adapter.py
  Typed invocation, pinned argv builder, subprocess boundary, and stable result model.
tests/validation/test_upstream_gflow_adapter.py
  Deterministic fake-runner coverage for command construction and failure normalization.
tests/features/upstream-first-adapter-v1.feature
  Offline BDD scenarios for authorization, ambiguity, and success normalization.
tests/features/test_upstream_first_adapter_v1_steps.py
  BDD binding for adapter-only scenarios; no Google Flow access.
docs/superpowers/plans/2026-11-01-upstream-first-adapter-v1/PREDICT.md
docs/superpowers/plans/2026-11-01-upstream-first-adapter-v1/SCENARIO.md
docs/superpowers/plans/2026-11-01-upstream-first-adapter-v1/PLAN.md
docs/ai-video-automation-v2/operations/UPSTREAM_GFLOW_ADAPTER_V1.md
  Pinned CLI contract, normalized output, policy boundary, and future canary steps.
```

### Modified files

```text
CHANGELOG.md
  Record the offline adapter addition and explicitly state production routing is unchanged.
```

## Task 1 — Contract and BDD scaffolding

**What:** Pin the actual upstream CLI contract and create offline scenarios before implementation.

**Files:** `PREDICT.md`, `SCENARIO.md`, `PLAN.md`, the BDD feature and binding.

**Steps:**
- [x] Resolve upstream release `v0.92.1` to commit `63dd0081e117dc081ed7fc774d3dd19ac91ec9aa`.
- [x] Inspect source for `t2v` flags and JSON schema; record absent timeout and project/account readback fields.
- [x] Add BDD tests for unauthorized, timeout-unknown, and normalized success.

**Tests created (red):**
- [x] Unauthorized request does not invoke the command runner.
- [x] Post-spawn timeout becomes non-retryable `POST_SUBMIT_UNKNOWN`.
- [x] Success does not claim production finalization or provider identities absent from upstream JSON.

## Task 2 — Adapter and fake CLI boundary

**What:** Implement strict typed input, argv construction, one-command execution, and normalized result/error mapping.

**Files:** `src/gflow_cli/v2_runtime/upstream_gflow_adapter.py`, `tests/validation/test_upstream_gflow_adapter.py`.

**Steps:**
- [x] Require authorization, profile ID/name, account expectation, exact project, approved config, staging path, job IDs, and idempotency key.
- [x] Require absolute pinned executable and verify reported CLI version is `0.92.1` before a generation command.
- [x] Build argv with `--project`, `--profile`, `--model`, `--aspect`, supported `--duration`, `--count 1`, `--output`, and `--json`; no shell string.
- [x] Call the subprocess once; use bounded timeout; any uncertainty after process launch is non-retryable.
- [x] Normalize strict success JSON and known errors into `UPSTREAM_GFLOW_ADAPTER_RESULT_V1`; store digests only.
- [x] Keep the adapter separate from `SceneGenerationPort` because upstream JSON lacks required exact production lineage.
- [x] Add deterministic fake-runner cases for the 14 scenarios in `SCENARIO.md`.

**Tests created (red):**
- [x] Exact argv, prompt preservation, shell-injection text, profile/project/config/count/output assertions.
- [x] Invalid project, unauthorized invocation, duplicate caller key, and staging-path escape.
- [x] Success, missing output, auth, project navigation, insufficient credits, pre-submit CLI error, post-submit timeout, agent UI, download failure, malformed JSON, unexpected exit, termination, stderr-only error, and secret redaction.

## Task 3 — Documentation and caller boundary

**What:** Document the pinned contract, local normalized schema, no-retry policy, current non-integration, and future canary.

**Files:** `docs/ai-video-automation-v2/operations/UPSTREAM_GFLOW_ADAPTER_V1.md`, `CHANGELOG.md`.

**Steps:**
- [x] Document upstream v0.92.1 and tag commit, exact flags, JSON fields, error codes, and missing identity evidence.
- [x] Document local authorization/profile/project/job/credit responsibilities and output validation/Drive boundaries.
- [x] Document `POST_SUBMIT_UNKNOWN`, download recovery, rollback to existing Phase G, and the no-live-test constraint.
- [x] State no established engine feature switch exists; default remains local Phase G and adapter is not called by PROD.

## Task 4 — Validation

**What:** Run adapter/production/artifact regressions and repository gates without live Flow access.

**Files:** no additional files unless a test exposes a defect.

**Steps:**
- [x] Run adapter tests and the targeted production execution/job/orchestrator/artifact/storage/reconciliation regressions.
- [x] Run Ruff and formatting over `src tests`, then Pyright over all `src`.
- [x] Run repo hygiene/doc-link/PII/website mirror/council checks.
- [x] Record live e2e as blocked by the user prohibition on Flow/credits; do not claim it passed.
- [x] Verify no production engine switch, n8n, Master Data, D2/D6 behavior, or runtime changed.

**Validation results:** adapter + BDD 30 passed; production execution/jobs 40 passed; orchestrator 5 passed with one existing Phase G adapter composition test excluded because the Windows sandbox blocks asyncio Proactor self-pipe creation; artifact 7 passed; Drive storage 4 passed; reconciliation 34 passed. Ruff, format, full-source Pyright, repository hygiene, doc links, PII, website mirror, and council checks passed. The broad offline suite was attempted serially and with four xdist workers; both stalled without completing in this sandbox (the xdist run reported 18 passing items before progress stopped). No live Flow test was run because this task is offline-only.

## Task 5 — Commit

**What:** Create one coherent commit only after offline gates pass.

**Steps:**
- [x] Review complete diff and ensure only adapter, tests, docs, and changelog are staged.
- [x] Commit on `feature/upstream-first-adapter-v1`; do not push or deploy unless repository policy and a separate scope authorize it.
- [x] Report the future canary as a separate authorized task; keep Phase G until canary/parity passes.

## Definition of done

- [x] Adapter fake-runner and targeted offline regression tests pass.
- [x] Ruff, format, and Pyright pass.
- [x] No Flow/browser/provider action, production routing, D2/D6 behavior, n8n, Master Data, or deployment changed.
- [x] Local result schema is documented and does not claim lineage absent from upstream JSON.
- [x] Commit contains only intended implementation/test/docs files.
- [x] Live generation E2E remains a separate canary gate, not reported as verified.

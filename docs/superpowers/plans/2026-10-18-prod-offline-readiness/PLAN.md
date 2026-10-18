# PROD Offline Readiness and D7 Persistence Implementation Plan

> **For agentic workers:** Execute one checkpoint at a time and run `/gflow:check` before each commit.

**Goal:** Harden the OAuth bootstrap, generalize the proven single-attempt Phase G contract to explicit environments, and prepare durable Sheet/Drive persistence with offline tests while keeping live actions blocked by missing human OAuth.

**Architecture:** Keep the existing Flow browser engine and `ProductionJobCoordinator`; add an explicit immutable execution identity and a provider/day shared credit guard around the existing Phase G state machine. Add bounded Google Sheets write and Drive upload adapters beside the current read-only source, with optimistic concurrency and readback verification; do not claim Sheets provides CAS.

**Predict verdict:** CAUTION — 7/10. Five personas identified promotion-ack uncertainty, cross-environment budget duplication, Sheets' lack of CAS, and Drive upload timeout/idempotency as principal risks. No live credential is present.

**Current result:** OAuth hardening, Phase G production identity/credit guard, offline tests, and cinematic research are complete. D5/D6 remain blocked by the absent human-installed OAuth credential. D7 is **partially prepared**: the Sheet store is a low-level optimistic adapter and is not wired into `ProductionJobCoordinator`; current Sheet tabs do not provide durable transition history or a provider media ID. The Drive adapter uses multipart upload rather than resumable upload, and its caller must supply a freshly verified folder identity. No live Sheet/Drive write or provider generation was run.

## Checkpoint 1 — OAuth bootstrap safety

**Files:** `scripts/operations/bootstrap_google_workspace_auth.py`, its validation tests, and operator docs.

- [x] Stage candidate credential on VPS.
- [x] Verify as the `gflow` service user against 34 schemas, exact Sheet identity, and writable `Job_Artifacts` folder.
- [x] Reconcile uncertain rename acknowledgements and attempt digest-guarded rollback when state cannot be proven.
- [x] Add regression tests for successful promotion, pre-promotion failure, lost acknowledgement, canonical verification failure, and safe redaction.
- [x] Run focused tests, docs/hygiene/secret checks, and report the human OAuth gate.

## Checkpoint 2 — Environment-neutral Phase G execution context and shared budget

- [x] Add test-first explicit execution context: environment, provider, account, project, auth profile, scene, profile, prompt, attempt, and credit budget context.
- [x] Reject mixed TEST/PROD identity before browser bind or state mutation.
- [x] Reuse the sequential Phase G executor and exact asset-attribution guards; keep the D2 ledger as historical evidence only.
- [x] Add one provider/account/day ledger with the user ceiling of 50 shared across environments; require current quote, free-credit evidence, no paid fallback, and no unresolved attempt.
- [x] Unknown post-click state reserves the known quote and blocks later attempts; WAF/paid/unknown outcomes stop without retry.
- [x] No browser generation or live provider operation in these tests.

## Checkpoint 3 — Durable optimistic Google Sheets transaction store

- [x] Add a bounded write transport separate from `GoogleSheetsSnapshotSource`'s read-only interface.
- [ ] Use the existing PRODUCTION_JOBS and SCENE_JOBS schemas and coordinator contracts. The adapter validates existing schemas but is not wired to `ProductionJobCoordinator`.
- [x] Serialize local V2 writes through one writer lock; read before write, compare exact expected row values/digest, submit bounded updates/appends, then read back.
- [ ] Reconcile uncertain acknowledgements using stable IDs/idempotency keys and refuse changed rows. Durable append-only retry/event history is not represented in the current approved tabs.
- [x] Document external-writer race limitations; do not claim server-side CAS.
- [x] Test conflict, duplicate claim, lost acknowledgement, concurrent local writer, and exact readback with fake transport.

## Checkpoint 4 — Drive persistence adapter

- [ ] Resolve the approved output folder dynamically by current verified ID/name/type/capability; caller must supply a freshly verified folder identity before live use.
- [ ] Implement an injected OAuth/HTTP adapter for resumable upload, metadata read, download, and stable application properties. The current offline adapter uses multipart upload.
- [x] Verify remote file ID, parent, name, MIME type, size, and a separate exact download SHA-256; media duration/identity remains a caller-level media validation gate.
- [x] Preserve local media until remote verification succeeds; reconcile uncertain upload acknowledgements by stable properties before any retry.
- [x] Test adapter paths with fake HTTP only; keep live Drive writes blocked.

## Checkpoint 5 — AI Editor cinematic research

- [x] Inspect official/editorial/academic sources and each named open-source repository at exact branch/SHA/license/activity.
- [x] Audit current V2 Sheet ownership before recommending any fields.
- [x] Write `docs/ai-video-automation-v2/AI_EDITOR_CINEMATIC_RESEARCH_REPORT.md` with evidence-backed design inputs and explicit implementation hard stop.
- [x] Keep repository clones outside the product repo and do not implement EditPlan or new Sheet tables.

## Checkpoint 6 — Quality and review gates

- [x] Run focused OAuth/D7 tests, all 34-schema validation, repository `/gflow:check` checks, full offline suite, `uv build`, and changed-file credential/hygiene scans.
- [x] Record that D5 remains `WAITING_FOR_HUMAN_OAUTH`; no D6/D7 live PASS is asserted.
- [ ] Commit logical checkpoints only after `/gflow:check`; push only `origin/automation-v2`, no rebase/force/develop merge.
- [ ] Deploy exact clean runtime SHAs only after review; run VPS runtime doctor. Never deploy operator-only research artifacts unnecessarily.

## Definition of done for this blocked-live run

- [ ] OAuth bootstrap uses stage → service verify → atomic promotion and can safely reconcile ambiguous acknowledgements.
- [ ] Offline production identity and shared credit guard are environment-neutral and fail closed.
- [ ] Durable Sheets/Drive adapters have fake-transport coverage and explicit concurrency/idempotency limitations.
- [x] Cinematic research report is complete and EditPlan implementation remains unstarted.
- [x] All requested checks are recorded; no video generation, paid API call, production n8n edit, or publishing action occurs.

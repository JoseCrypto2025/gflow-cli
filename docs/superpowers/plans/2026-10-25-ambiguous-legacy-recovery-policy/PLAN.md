# Ambiguous Legacy PROD Recovery Policy

> **For agentic workers:** Continue with the next unchecked task. Run the repo check before every commit.

**Goal:** Make an exhausted ambiguous legacy PROD attempt locally non-runnable while preserving unknown provider fate, its held reservation, and an auditable replacement-eligibility decision.

**Predict verdict:** CAUTION — confidence 6/10

**Scope:** Internal V2 operations policy for one exact legacy attempt. No public CLI/MCP, queue codec, Flow, or provider behavior changes. Ordinary HOLD and modern PROD/frozen TEST recovery remain unchanged.

## Risk register

| Severity | Risk | Mitigation |
|---|---|---|
| Critical | Ordinary HOLD resumes through recover_existing() | Add a distinct QUARANTINED state and check it immediately after idempotent claim, before workspace/request setup or adapter recovery |
| Critical | Two independent Sheet rows cannot be updated atomically | Guard exact old rows, write/read back each row, replay idempotently, and never report complete until both rows and policy audit agree |
| Critical | Chat-only exhaustion could be mistaken for durable evidence | Persist the operator-reported exhausted result as a typed, digest-bound policy prerequisite; do not rerun provider evidence recovery |
| Critical | A policy event could authorize a replacement implicitly | Store eligibility separately from authorization; create no attempt/reservation and enforce one future lineage authorization per predecessor |
| High | Exposure may be understated | Report existing unresolved reservation (50) separately from potential replacement/combined exposure (unknown/unquoted) |
| High | Policy state could leak into modern PROD/TEST behavior | Keep the policy API internal and narrowly legacy-scoped; add regression tests for ordinary HOLD and both existing recovery contracts |

## Task 1 — State/schema contract and failing tests

**Files:** production/scene job models, schemas, data dictionary, focused validation tests.

- [x] Add QUARANTINED as a recognized terminal local job state with no transition out.
- [x] Add failing tests that ordinary HOLD still resumes and QUARANTINED cannot transition or validate as runnable.
- [x] Preserve attempt and credit schemas; do not add D2 fields or mutate reservation/billing.

## Task 2 — Policy decision, exposure, audit, and lineage

**Files:** new internal v2_runtime policy module, focused unit tests, new versioned operations contract.

- [x] Bind the supplied exhausted-evidence result to the exact attempt as `USER_CONFIRMED_HANDOFF`; do not repeat evidence recovery or imply provider evidence.
- [x] Reject identity conflict, changed reservation, missing/duplicate job join, corrupted audit chain, and conflicting partial replay.
- [x] Report existing unresolved exposure as 50; potential replacement and combined exposure as unknown/unquoted.
- [x] Append an idempotent, hash-chained policy event; record eligibility but not authorization.
- [x] Derive one deterministic predecessor lineage key; this task creates no replacement attempt or authorization.
- [x] Test audit corruption, replay, reservation/provider-evidence preconditions, and replacement eligibility without authorization.

## Task 3 — Safe Sheet application and exact readback

**Files:** internal operations apply script/service adapter and integration tests.

- [x] Read fresh schema-bound job rows and exact local D2/shared-credit state; do not rerun evidence classification.
- [x] Require the exact PROD-linked job pair and exactly 50 reserved credits in the existing unknown state.
- [x] Quarantine only the two linked RUNNING rows, preserving every field except status and update time.
- [x] Read both rows back and append the applied policy event; partial writes remain incomplete and replayable.
- [x] Never write D2, credit ledger, billing, approval, reservation, attempt count, or provider.

## Task 4 — Orchestrator and retry guards

**Files:** production orchestrator, attempt append/retry boundary, regression tests.

- [x] Stop QUARANTINED jobs before workspace/request setup, adapter setup, recovery, or generation.
- [x] Reject append/retry for a quarantined predecessor through terminal transition and append rules.
- [x] Preserve ordinary HOLD recovery behavior and cover quarantine terminality and orchestrator stop.

## Task 5 — Operational docs and deployment manifest

**Files:** versioned policy contract, AGENTS routing, data dictionary, deploy allowlist/required files, focused validation tests.

- [x] Document exact preconditions, audit, exposure, idempotency/replay, and replacement non-authorization.
- [x] Keep modern PROD V1 and TEST V1 contracts unchanged.
- [x] Include the internal operator script in the canonical runtime deployment archive.
- [x] Verify there is no public CLI/MCP/queue payload change.

## Task 6 — Verification, commit, push, deploy, and controlled apply

- [x] Run targeted tests, repository checks, full test and coverage gates; no Flow surface changed (Flow E2E is not applicable).
- [ ] Commit only reviewed policy files.
- [ ] Fetch and verify origin/automation-v2; push only a normal fast-forward.
- [ ] Create the documented runtime rollback backup and deploy the exact commit with the canonical workflow.
- [ ] Run runtime doctor, deployment-manifest verification, and source/deployed/remote head parity.
- [ ] Apply the exact policy to D2_13c62112d9881710019049d95cb6da57 only after parity passes; verify both jobs quarantined, reservation still 50/unknown, billing unknown, and audit readback.
- [ ] Stop without any Generate, Approve, new attempt, reservation release, BotTrade, or unrelated n8n action.

## Definition of done

- [ ] Focused regression suite and canonical repository checks pass.
- [ ] CHANGELOG.md records the operator-facing state behavior.
- [ ] Exact commit is pushed and deployed with rollback backup.
- [ ] Runtime doctor and head parity pass.
- [ ] Exact attempt is quarantined with unchanged reservation/billing and durable policy audit, or a precise external safety blocker is reported.

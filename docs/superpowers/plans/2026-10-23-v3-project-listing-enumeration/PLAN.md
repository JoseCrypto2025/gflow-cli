# V3 Project Listing Enumeration Implementation Plan

> **For agentic workers:** Run `/gflow:status --feature v3-project-listing-enumeration` to find the next unchecked task. Implement one task at a time. Run `/gflow:check` before every commit.

**Goal:** Let the provider-silent V3 collector prove complete, stable enumeration from Flow's project listing without changing normal Phase G attribution.

**Architecture:** Add a reusable decoder beside `generation_record()` that returns every unique record matching the proven video-record invariant. Keep `Zzl0ze` observation local to the V3 operations collector, filter before reading bodies, require three distinct project reload listings, and persist only sanitized identifiers/statuses. Normal Phase G RPC subscriptions and D2 ledger code remain unchanged until evidence is complete.

**Predict verdict:** CAUTION — confidence 7/10 (average persona score 7.6; Devil's Advocate required independent listing snapshots and strict attribution limits).

**Risk register:**

| Severity | Risk | Mitigation |
|---|---|---|
| Critical | Listing payload mixes image/video records or changes shape | Reuse `_is_record()` and enforce the measured video record shape; fail closed on malformed candidates. |
| Critical | A new or unstable provider identity is mistaken for historical | Compare each exact-project listing with baseline/history and require identical normalized record sets across three separately observed reloads. |
| Critical | Signed URLs or raw project metadata leak to evidence/logs | Parse transiently and retain only workflow/project/media/status values. |
| High | Reused or missed listing response is treated as complete | Require one observed `Zzl0ze` response per reload; response/body/parser uncertainty blocks evidence. |

## File structure

### New files

```text
tests/operations/test_collect_provider_silent_d2_evidence_v3.py
  Fake-page and observer tests for listing completeness, union, fail-closed behavior, and sanitization.
docs/superpowers/plans/2026-10-23-v3-project-listing-enumeration/SCENARIO.md
  Scenario matrix and acceptance criteria.
```

### Modified files

```text
src/gflow_cli/api/transports/batchexecute.py
  Add all-record decoder; leave generation_record() unchanged.
tests/api/transports/test_batchexecute.py
  Add multi-record decoder regression coverage.
scripts/operations/collect_provider_silent_d2_evidence_v3.py
  Add bounded, collector-owned Zzl0ze observer and per-reload snapshots.
docs/superpowers/plans/2026-10-23-v3-project-listing-enumeration/PLAN.md
  Implementation tasks and verification sequence.
```

## Task 1 — Decoder regression tests (test scaffold)

**What:** Specify complete enumeration of only proven video generation records.

**Files:**
- `tests/api/transports/test_batchexecute.py` — single/multiple/nested/duplicate/unrelated payload cases.

**Steps:**
- [x] Add synthetic payloads using existing `_record()` shape and measured status/URL/size positions.
- [x] Include a length-7 image-shaped record and unrelated UUID arrays as decoys.

**Tests created (red):**
- [x] All unique video records are returned deterministically with status, URLs, and size preserved.
- [x] Unrelated/image records do not become video generations; singular decoder behavior remains unchanged.

## Task 2 — V3 observer and collector tests

**What:** Capture one listing per explicit project reload and fail closed unless three complete stable listings are observed.

**Files:**
- `scripts/operations/collect_provider_silent_d2_evidence_v3.py` — observer and exact-project record union.
- `tests/operations/test_collect_provider_silent_d2_evidence_v3.py` — fake Page/Port collection tests.

**Steps:**
- [x] Filter host, batchexecute path, and exact `rpcids=Zzl0ze` before scheduling response-body reads.
- [x] Install the listener before reload; await one response per reload; remove listener and drain/cancel tasks in `finally`.
- [x] Union listing records with `port._records`, deduplicate on workflow/project/media, and retain only target-project sanitized values.
- [x] Require stable record sets/statuses, no pending work/confirmations, known historical identities, and no chat-only unmatched identifiers.
- [x] Keep Zzl0ze out of normal `_VIDEO_RPCS`; don't alter contract or closure logic.

**Tests created (red):**
- [x] Listing not observed and listing parser/read failure produce no evidence.
- [x] Historical records from listing and status observer union/deduplicate.
- [x] New execution/media identities, conflicting mappings, or changed snapshots fail closed.
- [x] Evidence contains no response body, signed URL, poster URL, or prompt text.
- [x] Three reloads, not repeated reads from one response, are required.

## Task 3 — Offline validation

**What:** Run focused and affected repository gates before commit.

**Files:** None beyond the implementation/test files above.

**Steps:**
- [x] Run decoder and affected pytest suites under Python 3.13; run Ruff, format, Pyright, and docs/hygiene checks.
- [x] Check for repository-supported secret scanning; Gitleaks and detect-secrets are unavailable, so the gate is reported BLOCKED rather than PASS.
- [ ] Review and commit the scoped changes only after all required offline checks pass.

## Task 4 — Live read-only collection and conditional closure

**What:** After commit and push, run the pinned TEST-project collector, then preview/apply only if all V3 evidence requirements pass.

**Files:** Runtime evidence only under the already-bound runtime-state root; no tracked source changes.

**Steps:**
- [ ] Run the collector in the proven host context with TEST identity/project checks and CLEAN journal.
- [ ] Require three distinct Zzl0ze listing responses over 60–120 seconds, historical IDs only, zero pending/confirmation, and stable records.
- [ ] Recheck ledger SHA and run the current operator preview; apply only on PASS and unchanged SHA.
- [ ] Verify the exact target row and unchanged credit accounting after apply.

**Tests created (red):**
- [ ] No paid E2E is authorized or required; this task performs read-only live enumeration only.

## Definition of done

- [ ] Tasks 1–4 complete; failed or incomplete listing evidence creates no V3 evidence package.
- [ ] Existing singular decoder and normal Phase G attribution behavior remain unchanged.
- [ ] Offline checks required by `skills/check/SKILL.md` pass, except explicitly named unavailable tooling.
- [ ] Live collector either proves the full V3 contract or stops without closure.
- [ ] No Generate, approval, new D2, D6, n8n, or BotTrade activity.

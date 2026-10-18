# D5 PROD Identity and Live Google Sheets Source Implementation Plan

> **For agentic workers:** Run `/gflow:status --feature d5-prod-live-sheets` for the next task. Run `/gflow:check` before every commit.

**Goal:** Establish an independent PROD generation identity and make D5 consume only a trusted, schema-valid live Google Sheets snapshot, while holding D5 until a legitimate V2 Google API credential is configured.

**Architecture:** Reuse `build_scene_prompt_payload()` and the existing approval-integrity guard for a first PROD prompt; enforce approval uniqueness by Scene + Generation Profile. Add a read-only Google Workspace REST transport, injected OAuth token provider, schema-backed snapshot source, atomic runtime writer, and production D5 source wiring. Caller-provided JSON remains offline-only.

**Predict verdict:** CAUTION — 6/10. Five-person review aligned on a fail-closed offline-capable source, but no V2 Google Sheets/Drive credential exists, so live D5 must remain held until human setup.

**Risk register:**

| Severity | Risk | Mitigation |
|---|---|---|
| Critical | Caller-authored `read_at` makes stale data appear current | Production entry reads from source directly; producer sets timestamp after complete read and validation. |
| Critical | Credential/API details leak to logs | Dedicated OAuth credential outside checkout, mode 0600; emit safe stable codes only. |
| High | API output diverges from schemas or omits a table | Read full schema set, match ordered headers, run existing full schema/FK/semantic validator. |
| High | PROD operation mutates TEST approval | New profile-scoped Phase E candidate; existing guard; equality regression test. |
| High | OAuth testing refresh token expires | Require production consent status for unattended operation; document consent limitations. |
| Medium | Snapshot escapes runtime or persists broadly | Resolved path containment, symlink rejection, atomic replace, POSIX mode 0600, cleanup. |

## File structure

### New files

```text
src/gflow_cli/v2_runtime/google_sheets.py — credential provider, read-only REST transport, snapshot source/writer
tests/validation/test_google_sheets_source.py — fake transport, schema, provenance, atomicity and failure tests
tests/features/d5_trusted_sheets.feature — offline trust-boundary BDD scenarios
tests/features/test_d5_trusted_sheets_steps.py — BDD binding
docs/ai-video-automation-v2/operations/GOOGLE_WORKSPACE_AUTH_BOOTSTRAP.md — one-time V2 OAuth setup
docs/superpowers/plans/2026-10-18-d5-prod-live-sheets/SCENARIO.md — edge-case matrix
docs/superpowers/plans/2026-10-18-d5-prod-live-sheets/PLAN.md — implementation checklist
```

### Modified files

```text
pyproject.toml, uv.lock — Google OAuth dependency
scripts/operations/vps_flow_presubmit.py — live source wiring; caller snapshots cannot start F5
scripts/validation/validate_v2_data_architecture.py — approval uniqueness per Scene/profile
tests/validation/test_v2_data_architecture.py
tests/validation/test_production_execution_data_v1.py
tests/validation/test_prompt_approval.py
tests/validation/test_vps_flow_presubmit.py — coexistence and fail-closed regression cases
docs/ai-video-automation-v2/operations/VPS_BROWSER_RUNTIME.md — updated D5 readiness evidence
```

## Task 1 — BDD scaffold

**What:** Bind offline trust-boundary scenarios before implementation.

**Steps:**
- [ ] Add scenarios for supplied-snapshot rejection, malformed live read, and TEST/PROD approval coexistence.
- [ ] Verify feature has exactly one offline step binding.

**Tests created (red):** production snapshot refusal; schema-header refusal; independent approval coexistence.

## Task 2 — PROD identity and prompt approval

**What:** Create/reuse exactly one PROD profile and independently approve its first fresh Phase E prompt.

**Steps:**
- [ ] Re-read current rows, derive IDs, and reuse any exact equivalent profile.
- [ ] Preserve GPR_0001, PVA_0001, and the TEST APPROVED row.
- [ ] Run fresh Phase E for SCN_000001 + PROD; create candidate with `build_scene_prompt_payload()`.
- [ ] Read back VALIDATED, rerun Phase E, approve through the existing integrity guard, and verify one approval per Scene/profile.

**Tests:** TEST/PROD coexistence; fresh profile-specific exact approval; immutable TEST row.

## Task 3 — Google API and trusted snapshot source

**What:** Read the approved spreadsheet through an injected scoped credential and verify its full V2 schema.

**Steps:**
- [ ] Implement token-provider and REST GET boundary using the narrowest scope compatible with Sheets and Drive file-level access.
- [ ] Require the approved spreadsheet, complete schema set, and exact ordered headers.
- [ ] Run the full local schema/FK/uniqueness/semantic validator.
- [ ] Assign producer-owned `read_at` after all reads and validation; include `LIVE_GOOGLE_SHEETS` provenance.
- [ ] Atomically write below runtime state with mode 0600 and path/symlink checks.
- [ ] Do not add Sheet mutation methods.

**Tests:** wrong source, missing tab, header mismatch, malformed row, duplicate ID, orphan FK, API/auth failure, secret field, timestamp provenance, atomicity, path containment, permissions, no writes, and redacted errors.

## Task 4 — Production D5 boundary

**What:** Remove human-authored JSON from the live Flow path.

**Steps:**
- [ ] Construct the live source only from an external V2 credential path.
- [ ] Read/validate live data before resolving PROD profile and fresh approved prompt.
- [ ] Keep JSON snapshot loader offline-only; it cannot call F5.
- [ ] Return safe hold when OAuth is missing/invalid; do not open Flow.
- [ ] Clean up transient snapshots; never enter D6.

**Tests:** local file cannot reach F5; missing auth cannot open Flow; valid in-memory source calls only F5 and returns Generate=false / credits=0.

## Task 5 — Credential bootstrap and runbook

**What:** Document the smallest supported human setup for unattended Sheets and Drive operations.

**Steps:**
- [ ] Document OAuth client, least-privilege file scope, explicit spreadsheet/folder grant, production consent status, and refresh-token expiry caveat.
- [ ] Document out-of-repo credential file owner/mode, rotation/revocation, and log exclusions.
- [ ] Record D4 PASS, live-source credential HOLD, and no D5/D6 action.

## Task 6 — Quality, commit, push, deployment

**What:** Run full checks, push the exact approved branch commit, deploy that SHA, and run V2 doctor.

**Steps:**
- [ ] Focused Phase E/profile/prompt/D5/source tests; Phase F/F5 regressions; 34 schemas; Ruff; format; Pyright; `uv build`; hygiene/security; full offline suite.
- [ ] Stage only approved V2 work; commit `feat: add trusted Google Sheets source for D5`.
- [ ] Push only to `origin/automation-v2`, without rebase/force/merge.
- [ ] Deploy exact clean SHA only through supported path and run runtime doctor; record deployed SHA.
- [ ] Keep D5 held if the human OAuth credential has not been installed.

## Definition of done

- [ ] One active PROD profile and independent PROD approval; TEST identity and approval preserved.
- [ ] Production D5 cannot accept caller-authored JSON and requires current live data.
- [ ] Fake tests cover schema, provenance, security, atomicity, and fail-closed behavior.
- [ ] Missing real V2 credential is reported as `WAITING_FOR_HUMAN_AUTH_SETUP`; no Generate/credit spend/D6 occurs.
- [ ] Checks, push, exact-SHA deployment, and runtime doctor are recorded.

## Continuation tasks — UTC root cause, immutable repair, and OAuth bootstrap

These tasks extend the already-approved D5 live-source plan. The continuation request overrides its earlier read-only-only Sheet wording solely for the one-time prompt recovery; the runtime Google Sheets source remains read-only. No general Sheets transaction writer or APPROVED-row metadata bypass is added.

### Task 7 — Canonical UTC and strict schema timestamps

- [x] Add a shared V2 UTC formatter/parser with canonical RFC3339 `Z` output and aware-only input.
- [x] Reuse it in V2 timestamp producers and strict TIMESTAMP validation.
- [x] Add regression tests for one `Z`, UTC normalization, naive rejection, malformed `UTCZ`, and validator errors.

### Task 8 — Fresh Phase E and immutable PROD version recovery

- [x] Re-read all exact source rows and current approval rows immediately before each write.
- [x] Append a new VALIDATED version using fresh Phase E and derived next ID/version.
- [x] Read back and exact-match fields, then atomically switch old/new status in one batch.
- [x] Normalize old malformed metadata only after old row is confirmed SUPERSEDED; preserve all semantic fields.
- [x] Validate all 34 live tables; require zero errors before deployment.

### Task 9 — Deterministic Google Workspace OAuth/Picker bootstrap

- [x] Implement an operator-only bootstrap accepting OAuth client JSON outside checkout.
- [x] Use loopback-only callback, state + PKCE, manual consent, only `drive.file`, and Picker with exact spreadsheet/folder IDs.
- [x] Verify both resources via Sheets/Drive APIs before atomic remote install; never print tokens/client secret.
- [x] Update runbook with exact Cloud Console APIs, audience/publishing, verification status, callback/grant steps, secure install, and timestamp RCA.
- [x] Add fake HTTP/callback/security tests; no password/MFA automation or OAuth live test until human consent.

### Task 10 — Quality, exact-SHA deploy, and D5 gate

- [x] Run all focused and full offline gates, schema validation, changed-file secret scan, and hygiene.
- [x] Commit and push only to `origin/automation-v2`; require clean tree and remote SHA match.
- [x] Deploy the tested runtime source SHA using the supported V2 install path; preserve prior deployed SHA and pass the V2 doctor.
- [x] Hold before Flow until human consent/grants exist; after credential installation verify the 34-table snapshot and existing output folder.
- [ ] Run only the D5 F5 live, non-generating presubmit; keep Generate unclicked and credits at zero.
- [x] Generate remains unclicked; D6 remains NOT_STARTED.

### Task 11 — Sheets request batching and bounded rate-limit recovery

**What:** Keep the full 34-schema validator authoritative while avoiding duplicate reads and surviving transient Google API throttling.

**Steps:**
- [x] Record baseline: 34 sequential values GETs per snapshot; pickerless precheck plus staged verification repeats the full dataset (68 value requests) and makes two additional Sheets metadata GETs.
- [x] Add one `values.batchGet` for the schema-ordered approved tabs, retaining each existing bounded `A1:AZ1001` range and all per-tab validation semantics.
- [x] Remove the redundant full schema read from the pickerless direct grant precheck; retain its exact Sheet/Drive identity checks and run the single authoritative full schema validation against the staged credential.
- [x] Add sequential bounded retry for safe Google API GETs on 429/5xx only, honoring Retry-After within a total deadline; preserve 401/403 behavior.
- [x] Carry only a typed exhausted-429 signal across the remote verifier boundary. On that signal, verify candidate digest and secure metadata, retain the stage, report `STAGED_VERIFY=DEFERRED_RATE_LIMIT`, and skip promotion/finalization. All other failures keep cleanup/rollback.
- [x] Update the OAuth operations runbook with retry limits and the safe re-verification command for a retained stage.

**Tests:** Retry-After seconds/date, exponential fallback and total bounds, success after 429, exhausted 429, 5xx retry, no 401/403 retry, exact batch range mapping and unchanged 34-schema validation, reduced request count, direct-precheck duplicate-read removal, retained stage/no promotion/no finalization after exhausted 429, safe metadata checks, and existing cleanup on non-rate-limit failures.

**Definition of done:** One values batch request per full 34-schema verification; one full snapshot read per bootstrap; complete valid snapshot remains semantically identical; staged 429 yields a secure resumable HOLD with no OAuth rerun; no live OAuth, VPS mutation, Flow, or generation is performed by engineering tests.

### D5 Google Workspace OAuth live closeout — 2026-10-18

- **D5 Google Workspace OAuth: DONE.** Pickerless reuse of the approved `drive.file` grant succeeded; the canonical credential was installed through the exact-path/exact-SHA resume flow and verified as `gflow:gflow`, mode `0600`, readable by `gflow`.
- The live verifier passed the approved Master Sheet, all 34 schemas, and the existing `Job_Artifacts` folder. No OAuth rerun or Picker was needed for promotion.
- The OAuth and credential flow is closed; change it only after a real future failure is demonstrated.
- **Next:** D5 VPS non-generating F5 presubmit remains pending. Revalidate the live PROD identity, current unique APPROVED prompt, and production runtime prerequisites before running it. D6 remains NOT_STARTED; the Generate kill switch remains active.

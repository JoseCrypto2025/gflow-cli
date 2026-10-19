# Prompt Version Lifecycle Persistence Implementation Plan

> **For agentic workers:** implement the checks in order; do not commit or push.

**Goal:** Provide a narrowly scoped repository-supported, fail-closed create/validate/approve lifecycle for immutable `SCENE_PROMPT_VERSION` rows.

**Architecture:** Reuse Phase E and the existing pure approval guard. Add an application use case plus a dedicated Sheets store restricted to the approved spreadsheet and prompt-version tab; do not widen the job store. Expose the operation through an operator-only script, with no public CLI/MCP surface. The schema has no `Is_Current` column, so current is derived solely from the unique `Prompt_Status=APPROVED` row.

**Predict verdict:** CAUTION — average confidence 7/10; mitigations are exact schema pinning, one batch status switch, readback, and explicit non-CAS limitation.

**Risk register:**
| Severity | Risk | Mitigation |
|---|---|---|
| Critical | Google Sheets has no CAS against external editors | Pre-read under local lock; compare stable row digests; batch transition; exact readback; HOLD on any ambiguity |
| High | Existing generic transaction writer intentionally excludes prompt rows | Add dedicated store with fixed tab/schema/range; never accept caller-selected tab/range |
| High | `Is_Current` is absent from both local/live 14-column schema | Do not alter schema; derive current from `APPROVED` status |
| High | Prompt text could leak through errors/logs | Safe errors and reports contain IDs/status/hash only |

## File structure

### New files
- `src/gflow_cli/v2_runtime/prompt_version_lifecycle.py` — use case and store protocol.
- `scripts/operations/reconcile_prompt_version.py` — operator-only preview and live wrapper.
- `tests/validation/test_prompt_version_lifecycle.py` — lifecycle and persistence tests.
- `tests/features/prompt_version_lifecycle.feature` — offline BDD scenarios.
- `tests/features/test_prompt_version_lifecycle_steps.py` — BDD binding.

### Modified files
- `src/gflow_cli/v2_runtime/prompt_approval.py` — explicit transition and immutable-field checks.
- `src/gflow_cli/v2_runtime/google_sheets_transactions.py` — dedicated prompt store/transport operation, preserving job-store scope.
- `CHANGELOG.md` — unreleased internal V2 lifecycle capability.

## Task 1 — Regression tests

**What:** Add fake-store tests and offline BDD coverage before implementation.

**Steps:**
- [ ] Cover candidate creation, ID/version allocation, duplicate rejection, exact matching, stale data, approval ordering, concurrency drift, readback mismatch, and prompt redaction.
- [ ] Cover exactly one APPROVED row as the canonical current marker.

## Task 2 — Pure lifecycle hardening

**What:** Make status transitions explicit and immutable fields immutable for every persisted state.

**Steps:**
- [ ] Preserve Phase E candidate and exact-match logic.
- [ ] Add a transition matrix that forbids DRAFT-to-APPROVED and arbitrary status changes.
- [ ] Ensure only Approved_At and lifecycle metadata change when approving/superseding.

## Task 3 — Dedicated persistence boundary

**What:** Add safe prompt-row create/update/batch update with readback under existing writer-lock conventions.

**Steps:**
- [ ] Pin spreadsheet ID, exact tab, local schema headers, and bounded ranges.
- [ ] Derive next ID/version from live rows under lock; fail on conflicts.
- [ ] Append only builder-produced VALIDATED candidate and verify exact row readback.
- [ ] Re-read expected prior row before approval, batch old/new status changes, read both back, and verify a single APPROVED row.
- [ ] Never retry uncertain writes blindly; fail with a sanitized HOLD.

## Task 4 — Operator wrapper and dry-run

**What:** Provide one repository-supported preview/live command using fresh Phase E and an injected canonical credential path.

**Steps:**
- [ ] Default to preview; require an explicit live-write flag.
- [ ] Print only expected IDs, versions, hashes, and status transitions.
- [ ] Do not call Flow or paid APIs.

## Task 5 — Quality gates and live reconciliation

**What:** Run tests and verify a dry-run before one approved live lifecycle reconciliation, then F5 only.

**Steps:**
- [ ] Targeted tests, relevant regression tests, repo Ruff, Pyright, `git diff --check`.
- [ ] Verify no edits outside task files plus preserve all pre-existing edits and `.runtime/`.
- [ ] Preview the dynamic next prompt version/ID and expected transitions.
- [ ] If preview passes, create one version, validate exact hashes/text, re-run Phase E, approve, verify readback and exactly one APPROVED row.
- [ ] Rerun D5 F5 non-generating. Stop if it does not pass; do not start D2.

## Definition of done

- [ ] Lifecycle transition tests pass, including all requested failure paths.
- [ ] Existing V2 schemas, Phase E, F5, and Phase F tests pass.
- [ ] Ruff, Pyright, and diff check results are reported.
- [ ] The live prompt reconciliation uses the checked-in 14-column schema, preserves historical payloads, and verifies exactly one APPROVED row.
- [ ] F5 passes without Generate; D2 remains untouched.
- [ ] No commit or push.

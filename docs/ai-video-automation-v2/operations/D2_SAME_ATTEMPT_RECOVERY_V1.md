# D2 Same-Attempt Recovery Contract

**Contract ID:** `D2_SAME_ATTEMPT_RECOVERY_V1`\
**Status:** FROZEN

This contract describes the repository's committed same-attempt recovery behavior for Phase G
D2. It is derived from the current implementation and
[`PHASE_G_D2_CONTROLLED_EXECUTION.md`](../architecture/PHASE_G_D2_CONTROLLED_EXECUTION.md).
It does not authorize or perform a recovery. Values identifying a particular run, ledger snapshot,
confirmation, account, quote, or Git revision belong only in current execution evidence, never in
this contract.

## Authoritative sources

The current behavior is defined by:

- `docs/ai-video-automation-v2/architecture/PHASE_G_D2_CONTROLLED_EXECUTION.md` — Phase G and D2
  architecture and boundaries.
- `src/gflow_cli/v2_runtime/phase_g_run.py` — request building, the
  `recover_late_confirmation_same_attempt` operator, existing-media recovery, validation, and Drive
  finalization.
- `src/gflow_cli/v2_runtime/phase_g.py` — request and evidence types, execution state machine, ledger
  invariants, daily credit accounting, and D6 release proof.
- `src/gflow_cli/v2_runtime/phase_g_live_flow.py` — live confirmation fingerprinting, read-only
  confirmation revalidation, one-time approval, same-attempt observation, and exact media download.
- `src/gflow_cli/v2_runtime/live_flow.py` — repository auth-verifier identity comparison and the
  English-locale surface guard.
- `src/gflow_cli/v2_runtime/phase_g_artifacts.py` — local media validation and remote re-download
  verification.
- `scripts/validation/finalize_d2_drive_artifact.py` — the committed Drive connector finalizer
  entry point.
- `tests/validation/test_phase_g_run.py`, `tests/validation/test_phase_g.py`,
  `tests/validation/test_phase_g_live_flow.py`, and `tests/e2e/test_phase_g_d2_live.py` — regression
  evidence for the recovery, gates, and live D2 path.

The function signatures, state names, ledger fields, and CLI surface (if any) must be checked in
the current checkout before execution. The recovery operator is a Python coroutine; the committed
finalization script is not a late-confirmation recovery command.

## Mandatory context pre-read

Before any D2 recovery execution, read in order:

1. `AGENTS.md`.
2. This contract.
3. Current signatures and relevant logic in the authoritative source files above.
4. The latest persisted execution evidence and operator report for the exact attempt, including the
   current ledger identity and the evidence used to identify any pending confirmation.

Do not reconstruct missing values from memory, chat, examples, or another attempt. If the current
code, this contract, and persisted evidence disagree—or required evidence cannot be verified—STOP
without opening Flow or changing state. A source change made after this frozen contract is not
implicitly covered by it.

## Applicable existing-attempt recovery conditions

The late-confirmation path is only for the already submitted D2 attempt whose ledger state is
`UNKNOWN_SUBMISSION_STATE`. It is not a retry path. Eligibility requires all of the following to
match the current committed predicate:

- The attempt is nonterminal and has exactly one durably recorded Generate action and zero prior
  approval actions.
- The attempt has no recorded provider execution ID, media ID, or provider media ID list.
- The attempt has no confirmation already recorded or reconciled in the ledger; a prior observed
  or approved gate is not eligible for the late-confirmation path.
- The request and ledger identify the same scene, prompt version and hash, generation profile,
  provider account, and provider project in the exact supported TEST lane.
- The pinned request is rebuilt from a fresh Phase E approval and matches the persisted attempt.
- Before the live D2 boundary, the expected account email is compared with the identity observed by
  the repository auth verifier. The actual email is runtime evidence and must not be copied into
  this contract.
- The inspected Flow document/browser locale must be English (`en` prefix); other or unknown
  locales fail closed because these controls have not been verified there.
- The D2 ledger exists and its SHA-256 exactly matches the operator's independently reviewed
  expected ledger hash. Its accounting date must equal the current Bangkok calendar date at
  recovery time, as required by the late-confirmation ledger reconciliation.
- The submit baseline is durably captured, structurally valid, tied to the pinned project, and has
  exactly zero pending work. Its media, execution, and confirmation IDs remain available to exclude
  old evidence, and its Agent turn marker must be exactly `0:` as required by the current recovery
  predicate.
- The Agent settings recovery journal is clean or absent. Any dirty, unreadable, or mismatched
  journal blocks recovery.

The separate `recover_and_validate_existing` path applies only when the ledger already contains an
exact provider media ID for a generated attempt in a recoverable artifact state. It retrieves and
validates that recorded media under the same D2 run ID; it does not submit again.

## Current committed late-confirmation operator

The operator is
`gflow_cli.v2_runtime.phase_g_run.recover_late_confirmation_same_attempt`. Its current keyword
parameters are `expected_email`, `expected_state`, `expected_ledger_sha256`,
`expected_confirmation_id`, `expected_video_count`, and `expected_credit_cost`, together with the
rebuilt `PhaseGExecutionRequest`. Confirm their current signature before any execution. Do not put
run-specific argument values in this contract or a reusable prompt.

The operator checks the ledger and request before opening the live generation port. It binds the
request, re-reads the live confirmation, records the revalidated quote against the expected ledger
hash, reserves the cost, and only then approves the gate. It has no Generate action. The Drive
finalizer is `scripts/validation/finalize_d2_drive_artifact.py`; it only finalizes an already
generated and locally validated clip after an independent connector re-download.

## Exact gate revalidation and approval limit

The live gate must be a single post-baseline confirmation in the pinned project. Its fingerprint is
the SHA-256 digest of the gate's current outer HTML; raw markup is not the persisted identity. The
implementation waits up to 15 seconds for the permission DOM to hydrate, checking once per second.
Zero gates at the deadline, more than one gate, or a gate already present in the baseline fails
closed. The live gate fingerprint must exactly equal the independently reviewed expected
fingerprint.

Immediately before approval, the operator requires the same gate identity, project, one-video quote,
and positive known credit cost as the reviewed evidence. Any provider progress detected before the
confirmation blocks approval. The ledger enforces the daily budget and compares a visible balance
with the quote when a balance value is supplied. The current same-attempt recovery reader passes
`visible_balance=None`, so it does not independently verify the currently displayed Flow balance;
missing balance data is not proof of sufficient balance. The prior conservative reservation is
replaced with the exact one-video quote only through the SHA-guarded ledger reconciliation; provider
billing remains unknown until independently reconciled.

At most one approval is permitted for the attempt. The ledger reserves the quote before the live
approval action, and the live port rechecks that exactly one gate with the same fingerprint remains.
The ledger enforces a maximum of 50 accounted credits per Bangkok calendar day across its applicable
D2 attempts, including conservative reservations for unresolved outcomes. The observation window
after approval is bounded to 240 seconds and watches only the original submission baseline.
Approval does not authorize another Generate action, a retry, a new run ID, or a second paid attempt.

## Same-attempt observation and attribution

Observation remains attached to the same D2 run, request, confirmation, project, and original
baseline. Evidence must identify that run and project and attribute to the revalidated confirmation.
Provider execution/media identifiers already present in the baseline are rejected. A success must
resolve to exactly one new provider media ID. Multiple new media IDs are recorded as
`UNEXPECTED_MULTI_OUTPUT`; ambiguous, mismatched, or incomplete evidence stays unresolved and does
not authorize another submission.

The late-confirmation recovery operator does not download media. When the attempt has an exact
attributed media ID, the artifact path retrieves only that ID into the job-scoped temporary area,
then validates the resulting local MP4. Artifact retrieval can be retried against the already
recorded ID under the same D2 run ID, subject to the existing artifact-recovery state checks.

## Media validation

The downloaded file must be a regular non-symlink MP4 with a valid `ftyp` signature, positive size,
exactly one video stream, and a complete successful decode. Duration must match the request within
the committed absolute 0.05-second tolerance; aspect ratio must match within 1%. A SHA-256 is
recorded for the validated local artifact. Audio presence is informational. Failed download or
validation is recorded as an artifact failure; it does not permit generation again.

## Drive persistence and D2 finalization boundary

The connected Drive transfer is an operator boundary. After upload, an operator must independently
download the remote artifact to a separate local path. The committed finalizer takes the runtime
state root, the same D2 run ID, that remote-download path, and the remote file ID and filename. It
does not accept caller-supplied hash claims.

Finalization verifies that the ledger still points to the validated local artifact under its D2
temporary root, verifies the remote re-download's nonzero size and SHA-256 against the local file
and the ledger's recorded validated size/hash, and only then marks the attempt `D2_SUCCEEDED` with
`remote_verified` true. The primary local artifact is removed only after successful verification.
Mismatch or missing evidence leaves D2 incomplete and retains the local artifact for investigation.
The production Drive storage adapter remains outside this connector-transfer contract.

## D6 boundary

This contract governs D2 only. Do not run D6 as part of D2 recovery. D6 eligibility is determined
separately by the centralized `evaluate_d6_release()` proof and dedicated D6 creation path. It
requires the exact prerequisite D2 record to be `D2_SUCCEEDED`, remotely verified, hash-matched, and
compatible with the current D6 release contract marker. A terminal state, unknown outcome,
forensic closure, local validation alone, or this recovery contract does not authorize D6.

## Mandatory STOP conditions

Stop without improvising, approving, or changing state when any of these applies:

- The required contract, current signatures, or latest persisted evidence/operator report is
  missing, stale, unreadable, or inconsistent with another source.
- The ledger is missing, malformed, changed since review, or does not prove the exact eligible
  same-attempt state and identity.
- The recovery is not in the exact supported TEST lane, its fresh Phase E request does not match,
  the repository auth verifier does not confirm the expected identity, the inspected locale is not
  English, or the settings journal needs recovery.
- The baseline is absent or invalid, has pending work, or cannot separate new evidence from prior
  media, execution, and confirmation IDs.
- The gate is absent, stale, ambiguous, changed, not attributable to the pinned project, does not
  quote exactly one video at a known positive cost, any supplied visible balance is insufficient,
  or provider progress precedes it. The current same-attempt recovery path supplies no visible
  balance value, so do not infer a balance check from this condition.
- A Generate action has already been recorded other than the single original action, any approval
  was previously recorded, provider execution/media identity is already present where the late-gate
  path requires it absent, or the attempt is terminal.
- Approval, observation, attribution, media retrieval, validation, or persistence produces an
  unexpected result. Preserve the evidence and leave unresolved states unresolved; do not create a
  new attempt to work around them.

WAF refusal, unknown submission state, and forensic closure are not permission to retry, rotate an
account/profile, bypass controls, or evade a limit. A contract/code/evidence mismatch requires a
separate debugging or documentation task before any further execution.

## Execution, debugging, and code-change separation

Execution prompts that invoke a canonical contract are **PARAMETERS-ONLY / DELTA-ONLY**. Put
workflow logic in this contract and committed code; do not restate or alter it in a run prompt.
Keep recovery execution, debugging/reconciliation, code modification, and paid approval in separate
tasks. This contract itself grants no paid approval and does not authorize an operator to execute
anything.

## Contract versioning

`D2_SAME_ATTEMPT_RECOVERY_V1` is frozen. Do not silently edit its behavioral contract to follow a
code change. If intended behavior changes, create and review a new versioned contract, update the
agent-facing pointer in `AGENTS.md`, and preserve this version as historical evidence. Until the
new version and implementation are aligned, disagreement is a STOP condition. A documentation
correction that does not change behavior must be reviewed and identified as a non-behavioral erratum;
it must not weaken or silently broaden the frozen rules.

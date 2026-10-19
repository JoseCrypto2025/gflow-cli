# Phase G D2 controlled execution contract

Phase G is a separate generation boundary after Phase E approval and Phase F/F5
preparation. `FlowPreparationPort` and `GoogleFlowAgentOnlyPort` remain
submission-free; `FLOW_SUBMISSION_ENABLED` remains `False`. The Phase G
controller receives a distinct generation-only port and never adds submit
methods to the F5 surface.

## Preconditions and settings recovery

The caller must create a fresh `PhaseGExecutionRequest` from the current F5
request after rerunning Phase E. Phase G checks the exact approved prompt row,
scene/profile/provider/account/model/mode/project identity, source and prompt
hashes, duration, aspect ratio, and one-output contract. Superseded prompts and
hash or text drift fail closed.

Before the first Flow setting mutation, the executor reads the complete settings
snapshot and writes it to the Agent settings recovery journal as `DIRTY`. It
applies the pinned model, mode, aspect ratio, output count, and confirmation
policy `Always`, then verifies readback. The baseline of existing media,
executions, pending tiles, confirmation IDs, and Agent turn is captured
immediately before the one allowed Generate action and durably recorded in the
attempt ledger. The fresh confirmation ID and quote are recorded before
approval so a later observer can reconcile that same attempt. A pending
unrelated job blocks the attempt.

The live Flow port requires an expected account email. It compares that value
to the repository auth verifier's observed Google identity before the browser
can reach the D2 generation boundary. The surface check also fails closed when
the document/browser locale is not English, because the existing V2 controls
have not been verified for translated labels.

Settings restoration runs on every path. The journal is marked `CLEAN` only
after exact readback of the original settings. Any restore or readback failure
leaves recovery required and blocks the next attempt.

## Attempt and budget ledger

The task-local JSON ledger lives under `RUNTIME_STATE_ROOT/phase-g/`. It stores
semantic IDs, prompt hashes, UTC timestamps, action counts, confirmation quote,
provider IDs, accounting cost, and terminal state. It stores no credentials,
tokens, cookies, or browser data. A cross-process lock and atomic replacement
protect each update. This local ledger is an operational guardrail, not a
tamper-resistant accounting service; a machine owner can edit or remove it.

Each attempt records its Generate action before crossing the UI boundary and
may approve at most one attributable confirmation. The confirmation must
explicitly quote exactly one video, a known positive cost, and (when visible) a
sufficient account balance. The 50-credit budget is a maximum for the Bangkok
calendar date, not a target. Confirmed costs are reserved before approval.
Definitive no-charge outcomes account for zero. If submission outcome becomes
ambiguous, the quoted charge—or the remaining daily budget when no quote is
known—is reserved until reconciliation. New provider execution IDs are retained
as pending acknowledgments instead of being discarded on observation timeout.
WAF refusal, unresolved attempts, already successful D2 output, and every
recorded Generate action prevent another paid attempt. An already generated
media ID may be recovered and validated again under the same D2 run ID; this
recovery path cannot click Generate.

## Output attribution and media verification

The generation port must link observed provider execution/media evidence to
the exact new confirmation and expected TEST project. IDs present in the
pre-submit baseline are rejected. A successful single-output attempt must
identify exactly one new media ID; multiple new IDs produce
`UNEXPECTED_MULTI_OUTPUT` and are retained as evidence.

Only the exact attributed media may be retrieved, into a job-scoped directory
under `TEMP_ROOT/<D2_RUN_ID>/`. The existing migrated-host media recovery path
is the source of signed URLs and enforces approved Google download hosts,
redirect limits, MP4 signature, and provider-recorded byte size. Phase G then
requires a full PyAV decode, exactly one video stream, SHA-256, nonzero size,
duration, dimensions, aspect ratio, and informational audio presence.

The 8-second contract uses an absolute 0.05-second tolerance. This is derived
from historical Flow evidence in [PR #824](https://github.com/ffroliva/gflow-cli/pull/824),
which reported a 4.01-second result for a 4-second request. It is a narrow
acceptance bound, not a claim that current Flow always behaves this way. A
clip outside the bound is rejected; Phase G does not normalize its duration.
Aspect ratio is accepted within 1% of the requested ratio. Agent-only Flow
does not expose a resolution control, and native audio remains informational.

## Persistence boundary

`GoogleDriveStorageProvider` remains a fail-closed placeholder. The local
`persist_and_verify` helper is a separate contract: it uploads to an injected
storage port, confirms remote existence/size, always downloads the remote
artifact to a separate randomly named verification path, and recomputes SHA-256.
For the connected Drive connector, an operator downloads the uploaded artifact
to a separate verification path and runs
`uv run python scripts/validation/finalize_d2_drive_artifact.py` with the D2
run ID, Drive file ID/name, runtime state root, and redownload path. The ledger
re-hashes both files itself, checks the remote metadata and recorded validated
local artifact, marks D2 success only on an exact match, then removes the
primary local file. Caller-supplied hash evidence is not accepted.
This task-level path is classified `D2_CONNECTOR_DRIVE_PERSISTENCE`; it does
not mean the production Drive storage adapter is ready.

Use `04_PRODUCTION/Job_Artifacts` for this isolated D2 test clip. Do not create
`PRODUCTION_JOBS`, `SCENE_JOBS`, or `FINAL_OUTPUT` rows, and do not place the
single-scene test clip in `05_OUTPUT/Final_Video`.

## Explicit exclusions

This contract does not authorize a second submit, WAF evasion, account/profile
rotation, CAPTCHA solving, personal Chrome reuse, production n8n changes, VPS
deployment, or edits to production Sheets. A post-submit timeout is
`UNKNOWN_SUBMISSION_STATE`; reconcile the same attempt without clicking
Generate again.

### Forensic closure of irrecoverable unknown attempts

`UNKNOWN_SUBMISSION_STATE` is an active unresolved safety hold. When a bounded
forensic investigation is complete but cannot reconstruct the historical
provider outcome, an operator may propose `FORENSICALLY_CLOSED_UNKNOWN` using
`scripts/operations/close_unknown_d2_forensic.py`. The command is dry-run by
default. Applying requires `--apply`, the exact D2 run ID, the expected current
state, the expected ledger SHA-256, the expected canonical evidence SHA-256,
the implementation commit, and a complete sanitized evidence JSON. Store the
immutable evidence package outside Git at
`<runtime-state-root>/phase-g/forensic-evidence/<D2-run-id>.json`.
The ledger lock and SHA check protect the transition from concurrent updates.
Provider IDs, a changed ledger, a non-unknown or terminal state, a Generate
count other than one, or incomplete evidence fail closed.

Forensic closure permanently disables retry of that same run ID. It preserves
the historical accounted-credit reservation and records provider billing as
`UNKNOWN`; it does not establish `RECONCILED_NO_CHARGE` or prove
`GENERATION_FAILED`. A distinct future D2 run ID may be created, but it must
complete the normal controlled-D2 path as `D2_SUCCEEDED` before D6 is eligible.
The closed attempt stores `forensic_closed_at`, `forensic_reason`,
`forensic_evidence_sha256`, `forensic_implementation_commit`,
`provider_outcome=UNKNOWN`, and `credit_outcome=UNKNOWN`, alongside the
sanitized evidence summary. Existing ledger records remain readable.
`UNKNOWN_SUBMISSION_STATE`, `FORENSICALLY_CLOSED_UNKNOWN`,
`RECONCILED_NO_CHARGE`, and `D2_SUCCEEDED` therefore have different meanings.

### D6 release prerequisite

Every D6 request must name one exact `prerequisite_d2_run_id`. The centralized
read-only `evaluate_d6_release()` checks that ID against the authoritative D2
ledger and accepts only a record whose submission and terminal states are both
`D2_SUCCEEDED`, whose remote artifact verification is present, and whose local
and remote-redownload SHA-256 values match. The D2 row must also carry the
current `d6_release_contract_version`; older success rows without this
compatibility marker fail closed and cannot authorize D6. A terminal state alone, including
`FORENSICALLY_CLOSED_UNKNOWN`, never qualifies. Generic `create_attempt()`
rejects all D6 IDs; the dedicated `create_d6_attempt()` rechecks the proof while
holding the D2 and D6 ledger locks in deterministic order, then persists the
exact prerequisite ID and proof summary on the D6 row.

The compatibility fingerprint binds Scene/workload ID, provider, generation
mode, exact model ID, a Phase E digest of the model row plus active model
capabilities and parameter maps, requested duration, aspect ratio, and output
count under release-contract version 1. Exact model matching is conservative
because this repository has no explicit model-family compatibility catalog.
The compatibility digest pins model version/capability configuration even
when the model ID remains stable. TEST/PROD environment, account, project, auth
profile, generation profile, and prompt version are deliberately excluded
because those identities are expected to differ between validation and
production lanes; the bound workload semantics remain identical. The digest
is derived from the fresh Phase E snapshot and persisted D2 row, never caller
success claims. Older D2 rows without the contract marker or compatibility
digest fail closed.

This adds an authorization gate to the shared programmatic Phase G path; no
dedicated D6 CLI or operational runner exists. For PROD, that path stores D6
attempts in `<runtime-state-root>/production/phase-g/d2-credit-ledger.json` and
requires a PROD execution context plus the shared production credit guard. The
generic result and artifact pipeline remains D2-shaped and has not been live
validated for D6. After historical forensic closure, D6 remains blocked until a
distinct D2 run completes the full verified artifact path and reaches
`D2_SUCCEEDED`; D6 must then cite that exact run ID.

Persistent Chrome forensic inspection may update browser-managed profile
metadata even when no authentication or application settings are intentionally
changed. Do not restore or roll back those browser-managed files. The safety
invariants are no auth login, profile creation/copy, cookie copying, Flow
settings change, or prompt change.

The one-shot live regression is
[`tests/e2e/test_phase_g_d2_live.py`](../../../tests/e2e/test_phase_g_d2_live.py).
It is opt-in via `GFLOW_CLI_E2E_RUN_PHASE_G=I_CONFIRM_ONE_D2_GENERATION`, requires
`GFLOW_CLI_E2E_PROFILE=gflow-v2-test`, a fresh Master snapshot captured within
15 minutes, the expected account email, and explicit state/temp paths. It runs
F5, Phase G, and media validation exactly once; Drive persistence remains a
separate operator step.

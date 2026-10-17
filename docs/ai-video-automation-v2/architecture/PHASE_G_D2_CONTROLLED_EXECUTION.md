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
protect each update.

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
The same `VerifiedRemoteArtifact` evidence type is produced for a connector
download via `verify_drive_redownload`. Finalization accepts this evidence,
checks it against the validated local file and ledger, and removes the primary
local file only after the remote hash matches. The connected Drive connector
can create the D2 artifact file and stream a separate download for hash
verification.
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

The one-shot live regression is
[`tests/e2e/test_phase_g_d2_live.py`](../../../tests/e2e/test_phase_g_d2_live.py).
It is opt-in via `GFLOW_CLI_E2E_RUN_PHASE_G=I_CONFIRM_ONE_D2_GENERATION`, requires
`GFLOW_CLI_E2E_PROFILE=gflow-v2-test`, a fresh Master snapshot captured within
15 minutes, the expected account email, and explicit state/temp paths. It runs
F5, Phase G, and media validation exactly once; Drive persistence remains a
separate operator step.

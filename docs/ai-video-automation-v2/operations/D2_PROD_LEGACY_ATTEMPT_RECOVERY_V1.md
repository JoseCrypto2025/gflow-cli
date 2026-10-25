# D2 PROD Legacy Attempt Recovery V1

**Contract ID:** `D2_PROD_LEGACY_ATTEMPT_RECOVERY_V1`
**Status:** Implementation contract
**Scope:** Read-only identity reconstruction and evidence classification for PROD D2 attempts created
before `D2_PROD_SAME_ATTEMPT_RECOVERY_V1`, with a separately versioned audit and exact legacy
reservation-release guard. This contract does not change modern PROD V1 or frozen TEST V1.

## Boundary

`reconcile_legacy_prod_same_attempt(...)` consumes copied rows and per-source snapshot metadata. It
does not read Google Sheets, open Flow, call a provider, click Generate, approve, create an attempt,
or write source rows. It builds a provenance-bound envelope, classifies persisted evidence, and
returns a separate audit event. A caller may persist that event to
`<runtime-state-root>/phase-g/prod-legacy-reconciliation-audit.json`; the D2 attempt row and shared
credit row remain unchanged by classification.

The supported operator is `scripts/operations/reconcile_legacy_prod_attempt.py`. It accepts only an
exact `--d2-run-id`; observation time and software commit are read by the operator, and canonical
Sheets data plus the production D2 and shared credit ledgers are loaded from configured runtime
sources. It builds the legacy envelope itself and fails closed unless
`recovery_prerequisite_satisfied` is true. Classification is read-only by default. The explicit
`--persist-audit` option writes only the contract's separate legacy audit; it never releases credit.
The operator does not open Flow, generate, approve, or create attempts. It reports
`operator_report_for_legacy=NOT_APPLICABLE` for this legacy path without creating a historical
report. Fresh live Sheets access requires `GFLOW_GOOGLE_WORKSPACE_CREDENTIALS` and the production
runtime configuration.

## Pre-recovery context

For this historical PROD path, a separate operator report is `NOT_APPLICABLE` when the runtime that
created the attempt had no PROD D2 report contract or writer. The required pre-recovery context is a
freshly built legacy evidence envelope. The caller must require
`envelope.recovery_prerequisite_satisfied == True` before invoking the reconciler; otherwise it must
stop. That predicate requires the exact unique Production Job, Scene Job, D2 attempt, Scene, approved
Prompt and hash, Generation Profile, Provider Account, PROD Flow project/environment, and credit
reservation joins, with provenance and source snapshot hashes/times for each required identity.

The envelope remains a distinct evidence artifact and does not represent or recreate a historical
operator report. This exception applies only to this legacy PROD contract; modern PROD and the
frozen TEST recovery rules are unchanged. Missing provider evidence still classifies as ambiguous,
and does not authorize releasing a reservation.

An exact positive refusal can be released only through
`ProductionCreditGuard.release_legacy_proven_not_submitted(...)`, after the classification event is
persisted. The guard rereads the exact D2 attempt, reruns the unchanged modern refusal predicate on
an ephemeral identity-normalized view, and verifies the exact reservation. It never writes derived
identity values to the D2 row. Reservation release and its linked audit are idempotent; billing
remains `UNKNOWN`.

## Canonical stores and joins

| Store | Canonical key | Required links |
|---|---|---|
| `PRODUCTION_JOBS` Google Sheet | `Production_Job_ID` | Exact PROD environment and job status |
| `SCENE_JOBS` Google Sheet | `Scene_Job_ID` | `Production_Job_ID`, `Scene_Prompt_ID`, `Attempt_No`; this row and attempt number derive the deterministic D2 run ID using `scene_phase_g_run_id` semantics |
| `SCENES` Google Sheet | `Scene_ID` | Exact scene referenced by the approved prompt |
| `SCENE_PROMPT_VERSION` Google Sheet | `Scene_Prompt_ID` | Exact Scene, approved status, Generation Profile, and `Prompt_SHA256` |
| `GENERATION_PROFILE_MASTER` | `Generation_Profile_ID` | PROD environment and exact Provider Account |
| `PROVIDER_ACCOUNT_MASTER` | `Provider_Account_ID` | PROD environment and exact `Provider_Project_ID` |
| PROD D2 ledger | `<runtime-state-root>/production/phase-g/d2-credit-ledger.json` | Unique `d2_run_id`; direct scene, prompt, profile, account, project, and prompt hash |
| Shared credit ledger | `<runtime-state-root>/production-credit/shared-ledger.json` | Unique `attempt_id`; exact PROD, account, and project; optional persisted job IDs must agree |

Every source snapshot carries its own source name, SHA-256, and observation time. The joins do not
claim the independently updated Sheets and local files form one atomic snapshot. Missing, duplicate,
or conflicting keys return `LEGACY_IDENTITY_AMBIGUOUS`. No `CONTENT_ID` is used or added.

The D2 row's `production_job_id`, `scene_job_id`, and `execution_environment` may be represented in
memory as `DERIVED_CANONICAL_JOIN` values only after all canonical links uniquely agree. The stored
D2 row is never backfilled. The generation profile, provider account, project, prompt, scene, and
hash must exactly agree with both the direct D2 row and their canonical master rows. A credit row
must be unique by exact D2 attempt ID and match account/project/environment; its own billing state
is retained independently.

PROD environment requires all of these to agree: production job environment, profile environment,
provider account environment, shared credit row environment, configured runtime namespace, and a
production-specific D2 ledger path. The source values and snapshot provenance are retained in the
envelope; if any source is absent or disagrees, the envelope is ambiguous.

## Provenance classes

Each identity and evidence field has `VALUE`, `SOURCE`, and `PROVENANCE_CLASS`:

- `PERSISTED_DIRECT` — value is present in its named canonical record.
- `DERIVED_CANONICAL_JOIN` — value is obtained only through exact, unique canonical-key joins.
- `DIAGNOSTIC_ONLY` — status and update-time context that cannot prove provider submission or billing.
- `UNAVAILABLE` — source field is absent; no substitute is inferred.

The envelope digest covers field values, provenance classes, source record keys, snapshot hashes,
and source observation times. Prompt/profile/account and D2 identities stay separate; credentials,
cookies, and account email are not part of the envelope.

## Generate timestamp and freshness

`generate_action_at` is `UNAVAILABLE` when the D2 row lacks it. Never copy `updated_at`, a job
timestamp, file time, or nearby log time into that field. `historical_event_time` in the audit is
null when no authoritative action time exists; `reconciliation_observed_at` is the current
observation time and remains a separate value.

Timestamp-dependent submit, confirmation, and refusal classification requires a directly persisted
Generate timestamp and then uses the existing modern PROD freshness/refusal predicate. The legacy
path never relaxes its positive `PROVEN_NOT_SUBMITTED` requirements. A same-attempt execution or
media identity may be classified without a click timestamp only when it is directly persisted in
the exact D2/Scene Job record, matches the reconstructed identity, and is excluded from the captured
baseline. Externally supplied evidence without direct action ordering remains ambiguous.

Results remain the six existing PROD values. Identity failure is additionally reported as
`LEGACY_IDENTITY_AMBIGUOUS`; it maps to `AMBIGUOUS_AFTER_RECONCILIATION` and cannot release credit.
Absence of submit, HTTP, confirmation, execution, or media evidence is never proof of non-submission.

## Jobs, credits, and audit

If both canonical jobs say `RUNNING` and there is no exact execution proof, the envelope reports
`LEGACY_PREMATURE_RUNNING`. This compatibility assessment does not write `HOLD` or alter either
Sheet row. A separately controlled application step may use the already-supported `HOLD` status
after exact identity validation; it must not mark the jobs failed or queued while provider submission
could have occurred.

Ambiguous classification leaves the exact existing reservation unchanged, including its reserved
amount and unknown billing outcome. The legacy release guard accepts the historical
`UNKNOWN_SUBMISSION_STATE` reservation state only after a persisted `PROVEN_NOT_SUBMITTED` event,
the direct action timestamp, and the exact modern provider refusal predicate all verify. It binds the
release to that one D2 ID/account/project and records a second immutable `RESERVATION_RELEASE` audit
event. Repeated release is a no-op; conflicting identity, prior different release, or incomplete
proof fails closed.

The separate audit file uses its own schema version and lock. A classification event records:
contract version, attempt ID, envelope digest, full field provenance, result, `mutation_performed`,
reservation action, historical event time, reconciliation observation time, and software commit.
A later reservation release is a separate event linked to the classification event digest. Audit
write failure after a credit update is retryable by the same event identity; the credit release
itself is idempotent.

## Isolation and validation

The compatibility API is internal and has no CLI, MCP, queue, or worker-codec twin. The frozen
`D2_SAME_ATTEMPT_RECOVERY_V1` TEST contract and `D2_PROD_SAME_ATTEMPT_RECOVERY_V1` modern contract
remain unchanged. A sanitized deterministic legacy fixture must construct its identity envelope,
classify absent provider evidence as `AMBIGUOUS_AFTER_RECONCILIATION`, retain the reservation, and
leave all fixture source rows unchanged.

Implementation and deployment do not authorize reconciliation of the current live attempt. Live
reconciliation is a separate controlled task after this contract and code are deployed.

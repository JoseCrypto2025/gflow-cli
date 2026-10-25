# D2 PROD Same-Attempt Recovery V1

**Contract ID:** `D2_PROD_SAME_ATTEMPT_RECOVERY_V1`
**Status:** Implementation contract
**Scope:** PROD recovery for an already-created production/scene/D2 attempt. This contract is independent of and does not modify the frozen TEST-only `D2_SAME_ATTEMPT_RECOVERY_V1`.

## Purpose and boundary

This contract defines how the runtime may attach late, authoritative provider evidence to an existing PROD attempt. Reconciliation never submits, approves, or retries generation. It never creates an attempt. A UI Generate action is recorded as an action only; it does not prove provider submission.

The identity chain uses only canonical identifiers already present in the approved model:

`Production_Job_ID → Scene_Job_ID → Scene_Prompt_ID → Scene_ID → D2 attempt ID`

The Phase G request and production execution context additionally pin provider, account, project, generation profile, and prompt hash. There is no canonical `CONTENT_ID` in the approved schema, and this contract does not invent or require one.

## Evidence vocabulary

The runtime distinguishes these levels:

1. `UI_ACTION_OBSERVED` — the existing ledger records the one Generate control action. This is diagnostic evidence only.
2. `PROVIDER_SUBMISSION_PROVEN` — a provider-originated response correlated to the unique submit window positively acknowledges acceptance and identifies the pinned project. A request dispatch, RPC name, spinner, or confirmation dialog alone is insufficient.
3. `PROVIDER_EXECUTION_PROVEN` — one new provider execution identity is tied to the exact attempt's accepted submission and was absent from the saved baseline.
4. `PROVIDER_OUTPUT_PROVEN` — one exact new media identity is tied to the same attempt and project, is absent from baseline identities, and has no competing candidate.

## Current evidence and authority

The following evidence is present in the current runtime models and D2 ledger:

| Field | Current source | Authority |
|---|---|---|
| D2 run ID, production job ID, scene job ID, scene ID, scene prompt ID, generation profile ID, provider account/project IDs, prompt SHA-256, execution environment | Pinned `ProductionExecutionContext` and persisted attempt row | Authoritative local attempt binding; not provider acceptance by itself |
| Generate action count and timestamp | `D2DailyCreditLedger` | Proves only that the UI action was recorded |
| Submit observer dispatch flag, RPC method ID, HTTP method, project ID, request count, dispatch-window completion, request failure, response-seen/status, refusal flag/code, Agent dispatch flag | `PhaseGSubmitTransportEvidence` and persisted attempt row | Request/RPC observations are diagnostic; exact provider response facts are required for a submission or refusal classification |
| Baseline project, media IDs, execution IDs, pending count, confirmation IDs, Agent turn marker and tile/chat hashes | `FlowSubmissionBaseline`, persisted on the D2 attempt | Authoritative exclusion set for identities that predate the attempt; does not prove a later submission was absent |
| Provider execution/workflow ID, media ID(s), project ID, submission state, attributed confirmation ID | `PhaseGAttemptEvidence`, persisted by the D2 ledger | Authoritative only after exact request, project, and baseline attribution checks |
| Confirmation ID, quoted video count, credit cost | Confirmation observation and D2 ledger | Proves a confirmation gate/quote was observed; it is not provider execution or output proof and does not authorize approval under this contract |
| Visible provider balance, spinner, card order, newest asset order, missing DOM card, timeout, or absence of a credit delta | UI or operator observations | Diagnostic only; never determines submission or billing state |

The current models do not contain a unique provider request ID, provider generation ID, or independently verified post-submit account identity in the submit receipt. `provider_submit_rpc_id` identifies the RPC method, not an individual request. Do not label it a request ID. Add a field only when the transport can actually capture it and tests establish its semantics.

## Reconciliation results

`reconcile_prod_same_attempt(...)` returns exactly one value:

- `SUBMISSION_PROVEN_PENDING`: one exact provider acceptance is proven and its execution is pending.
- `SUBMISSION_PROVEN_EXECUTION_FOUND`: one exact provider execution identity is proven.
- `LATE_CONFIRMATION_FOUND`: one new confirmation gate is positively observed for the exact attempt. This result does not approve the gate, reserve credits again, or claim provider submission.
- `OUTPUT_ALREADY_EXISTS`: exactly one new output identity is proven for the pinned project and attempt.
- `PROVEN_NOT_SUBMITTED`: reserved for positive, provider-originated evidence that the exact correlated request was rejected before any provider execution was accepted.
- `AMBIGUOUS_AFTER_RECONCILIATION`: all other cases, including missing evidence, incomplete scans, timeout, stale baseline, competing candidates, or mismatched identity.

The reconciler is an evidence consumer. It has no `FlowGenerationPort`, Generate method, confirmation-approval method, or attempt-creation method. Any future live evidence reader must be read-only, bounded, account/project-pinned, independently reviewed, and covered by a separate live-verification task. This implementation does not claim that PROD project-page re-poll is available or verified.

## Identity, freshness, and uniqueness

Every accepted receipt must bind to the existing D2 run ID and exact Production Job, Scene Job, Scene, Scene Prompt, provider account, provider project, generation profile, and prompt SHA-256. Evidence must be later than the persisted submit action and must identify the same project. The runtime must reject identifiers present in the saved baseline, mismatched identities, stale evidence, unrelated newer assets, and more than one candidate. It must not match by title, filename, card order, recency, or visible balance.

If an evidence source cannot establish one of these required identity links, the result is `AMBIGUOUS_AFTER_RECONCILIATION`.

## `PROVEN_NOT_SUBMITTED` and reservation release

Absence of chat, pending work, media, execution IDs, confirmation, or credit delta is never sufficient. Timeout or failure to observe a receipt is never sufficient.

This version supports `PROVEN_NOT_SUBMITTED` only when the runtime persisted all of these facts for the exact PROD attempt: exactly one `YhhmEf` POST submit request on the pinned project, observed after the Generate action; the dispatch window completed; its correlated provider response was observed with HTTP 403 and the exact provider refusal code `PUBLIC_ERROR_UNUSUAL_ACTIVITY`; the request did not fail; the response contained no generation record; and there is no confirmation, execution, or media identity for the attempt. The refusal code is extracted from the response body of the exact tracked Playwright request/response pair; the RPC method ID is not treated as a request ID. Any missing, duplicate, mismatched, or stale fact remains ambiguous. The rule treats this explicit provider refusal as positive non-acceptance evidence; absence of records, timeout, and balance observations are never substitutes.

Only the exact PROD reservation matching the production job, scene job, attempt, account, and project may be released, at most once, after the positive proof above. The evidence digest is persisted for idempotency and audit. Billing outcome remains a separate field and stays `UNKNOWN` unless independently proven. Visible balance cannot establish either submission or billing outcome.

## Late evidence, credit, and idempotency

Late confirmation, execution, and output evidence attaches to the existing D2 attempt. Recovery does not create an attempt, reserve credits a second time, approve a confirmation, or call Generate. Repeated reconciliation with the same evidence is a no-op that returns the already-recorded result. Conflicting evidence fails closed and preserves the prior audit record.

Receipt persistence is the durable source of truth. The operation records enough evidence identity and timestamps to audit its result in the existing D2 row. It does not write Sheets-backed job rows; the orchestrator keeps unresolved jobs on `HOLD` and only resumes through exact-media recovery. For a positive refusal, the D2 audit is written before the credit guard independently re-reads and verifies that audit and the persisted refusal facts. If the guard write fails, the reservation remains held and retrying the same evidence is idempotent. The operation never creates or releases any other attempt's reservation.

## Job state semantics

Use the existing persisted statuses where possible. Before exact provider execution proof, the Scene Job remains `QUEUED` or moves to `HOLD` with a stable reconciliation reason; the Production Job remains `QUEUED` or `HOLD`. `RUNNING` is written only after exact execution proof. An output proof may move directly into the existing artifact/finalization recovery path without claiming a new execution.

Any added persisted status must be introduced consistently in the runtime validators, Sheets transaction schema and validation, tests/fixtures, and this data dictionary. Do not use unrecognized status strings in the live Sheets table.

## TEST isolation and public surfaces

The frozen `D2_SAME_ATTEMPT_RECOVERY_V1` remains unchanged and TEST-only. Existing TEST late-confirmation approval behavior and its fixtures remain unchanged. The PROD reconciler is an internal V2 runtime API; no CLI, MCP tool, generation queue, or `worker/codec.py` surface is added by this contract.

## Explicit exclusions

This contract does not authorize opening Flow, Generate, confirmation approval, a new attempt, profile/account changes, credit consumption, production job-row mutation during implementation, or current-attempt reconciliation. The current production attempt may only be evaluated in a separate task after this implementation is validated. This contract does not claim live PROD verification.

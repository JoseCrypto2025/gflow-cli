# D6 Generation Outcome Evidence Contract

**Contract ID:** `D6_GENERATION_OUTCOME_EVIDENCE_V1`\
**Status:** ACTIVE\
**Scope:** Future PROD D2-backed generation evidence only. This contract authorizes no
Generate, approval, attempt creation, replacement, reservation, or recovery operation.

## Canonical PROD identity

PROD Phase G generation is bound to provider `PRV_0001`, account `PVA_0002`, account identity
`singleava508@gmail.com`, auth profile `GFLOW_PROD_AUTH` / `gflow-prod`, project
`aafe9282-071f-4333-baa4-92b9f76a9c27`, and project name `AI Video Automation V2 - PROD`.
The request builder, production identity validator, live adapter, and pre-click URL check all
fail closed for any other PROD account/profile/project. A wrong project raises
`FLOW_PROJECT_MISMATCH`; a project page that cannot be reached raises
`FLOW_PROJECT_NOT_READY`. No PROD fallback creates or selects a different project.

## Existing persistence and minimum additions

Production Job and Scene Job rows keep their existing lifecycle and output fields. The D2
attempt JSON record remains the durable source for provider identity and evidence; no new table
or ledger top-level schema version is introduced. It retains the account/project/model/mode,
prompt ID and hash, requested aspect/output, Generate count and time, provider request/response
facts, confirmation/execution/media IDs, state, and the existing pre-submit media/execution/
confirmation baselines.

The same attempt now also records the baseline capture time and project, the Generate intent
checkpoint and click-time identity/configuration values, SHA-256 digests and observation time for
the submit request/response when available, and deduplicated post-submit project asset snapshots.
Only digests are retained for wire bodies. Cookies, tokens, headers, raw prompts in transport
payloads, and raw response bodies are not persisted.

The Flow DOM currently exposes asset media IDs and pending/finished state. The attempt stores
those IDs as asset IDs/media IDs, and records observed execution IDs from parsed provider records
where available. Provider created/updated timestamps and independent asset type identifiers are
not exposed by the measured DOM/API adapter, so they are left absent rather than inferred.

## Project selection and project creation

The PROD adapter opens the repository-managed profile and verifies the configured account, then
navigates directly to the requested `/project/<id>` URL. It reads back the URL project ID and
project title before settings, baseline, or Generate work proceeds. It repeats the exact URL
check immediately before Generate and during bounded outcome observation.

The generic CLI transport contains a `New project` action for ordinary commands that call its
editor-entry path without a project ID. The Phase G PROD adapter does not call that path: it
requires a project ID and uses `GoogleFlowAgentOnlyPort.select_project`. No call site in this
PROD Phase G path falls back to a most-recent project. Historical date-stamped/empty project
creation cannot be conclusively attributed without historical browser logs; project-sprawl
cause is therefore `PARTIAL`.

## Submit evidence and state interpretation

`generate_action_count` is durably consumed before invoking the single browser click. It is not
provider acceptance. Submit request observation, response status/digest, parsed generation
record, refusal evidence, confirmation, execution, and media identity are separate persisted
facts. One request digest hashes the request body in memory and persists only its SHA-256; the
response is treated the same way.

The current attempt ledger states remain authoritative:

| Outcome meaning | Existing persisted representation |
|---|---|
| Prepared | `PREPARED` |
| Local Generate action recorded | `GENERATE_ACTION_RECORDED` and count `1` |
| Submit observed / accepted / processing | request evidence plus `SUBMISSION_ACKNOWLEDGED` or `GENERATION_PENDING` |
| Output located | provider media ID plus an exact-project post-submit snapshot |
| Validated success | `D2_SUCCEEDED` after existing file, decode, aspect, duration, Scene, prompt, and remote-artifact checks |
| Provider rejection | affirmative correlated refusal evidence with `PROVIDER_REFUSED` or `WAF_REJECTED` |
| Proven not submitted | the existing evidence-gated `PROVEN_NOT_SUBMITTED` reconciliation result |
| Timed out with unresolved outcome | ledger `UNKNOWN_SUBMISSION_STATE`; read-only asset monitor reports `TIMEOUT_PENDING` |
| Still unresolved after reconciliation | `AMBIGUOUS_AFTER_RECONCILIATION` |

The existing bounded observation window is 240 seconds with one-second read-only polls. Timeout
does not mean failure. It leaves the attempt unresolved and enters same-attempt reconciliation;
it never causes another click.

## Asset outcome monitor

Before the one click, `baseline_captured_at`, `baseline_project_id`, the visible media IDs,
execution IDs, confirmation IDs, and existing finished-tile/chat fingerprints are recorded.
During observation, a later snapshot stores the exact project ID, observation time, visible media
IDs, and observed execution IDs. Identical snapshots are deduplicated by a digest over project
and ID sets, so re-reading them does not change Generate count, reservations, or attempts.

The monitor subtracts the recorded baseline and links output only through the exact provider media
ID plus exact project identity; if an execution ID is recorded, it must also be new and present in
the snapshot. A latest card, visual order, nearest timestamp, or project ordering is never linkage
proof. `OUTPUT_FOUND` is distinct from `SUCCESS`; only the existing D2 artifact validation and
finalization path can reach `D2_SUCCEEDED` / `SUCCESS`.

## Idempotency and retry limit

Any attempt with Generate count `1` is ineligible for another automated Generate. Outcome
monitoring only reads the pinned project and may append a previously unseen evidence snapshot to
the same attempt record. It does not create a project, attempt, authorization, or reservation, and
does not change the provider-credit reservation. Reconciliation stays under the applicable
versioned same-attempt recovery contract. Neither timeout nor ambiguity authorizes retry.

The frozen `D2_SAME_ATTEMPT_RECOVERY_V1` behavior is unchanged. Any future behavior change to D2
recovery requires a separately reviewed contract version.

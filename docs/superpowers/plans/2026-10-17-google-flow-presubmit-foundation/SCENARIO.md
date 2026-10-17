# Scenario: Google Flow V2 presubmit foundation

## Scope
Provider routing by Provider_ID, immutable Phase E handoff validation, a no-submit Flow preparation state machine, deterministic holds, and offline tests. No schema, sheet, n8n, deployment, or generation changes.

## Severity-ranked scenarios

| Severity | Scenario | Required behavior |
|---|---|---|
| Critical | Generate is enabled while kill switch is off | `FLOW_SUBMISSION_DISABLED`; no submission method exists in the port |
| Critical | Prompt version is VALIDATED, SUPERSEDED, missing, or hash-mismatched | Hold before UI mutation |
| Critical | Configured project is missing or actual identity differs | Hold; never create/select implicit project |
| High | Session unauthenticated, expired, challenged, or account mismatch | Deterministic hold; no automated login |
| High | Unknown surface or stale UI readback | Hold without retries |
| High | Model/mode/duration/aspect/resolution/output/audio/quality differs | Hold; no silent normalization |
| High | Required reference lacks mapped provider identity | `FLOW_REFERENCE_SYNC_REQUIRED`; no upload |
| High | Frame mapping is absent or assigned to wrong slot | Hold |
| High | Prompt readback is truncated or changed | `FLOW_PROMPT_VALUE_MISMATCH` |
| High | Non-empty negative prompt lacks proven contract | `FLOW_NEGATIVE_PROMPT_UNSUPPORTED` |
| Medium | Generate missing or disabled | Hold, not ready |
| Medium | Concurrent same account/project preparation | one lane only; contention hold |
| Medium | Diagnostic path escapes TEMP_ROOT or includes auth material | reject/redact |
| Medium | Google Flow and fal share VIDEO_GENERATION | separate adapters by Provider_ID |

## Offline boundary
Fake port only. Tests must not instantiate FlowApiClient, call live Sheets, touch browser profiles, submit network requests, or invoke generation methods.

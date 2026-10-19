# Google Flow Presubmit Contract

## Inputs

1. Successful Phase E `PreflightResult`.
2. Persisted `SCENE_PROMPT_VERSION` with `Prompt_Status=APPROVED` and matching Scene ID, Generation Profile ID, exact compiled prompt/negative prompt, Source Context SHA256, and Prompt SHA256.
3. Resolved Provider_ID, Provider_Account_ID, Model_ID, Generation_Mode, and explicit dedicated Provider_Project_ID.
4. Exact provider reference mappings for each Phase E reference, including Flow project identity and frame slot where applicable.
5. Runtime settings roots; no secrets or browser data are passed in the request.

## State progression

`REQUEST_VALIDATED → SESSION_READY → PROJECT_READY → SURFACE_READY → SETTINGS_BOUND → REFERENCES_BOUND → PROMPT_BOUND → PRESUBMIT_VERIFIED → FLOW_PRESUBMIT_READY`

Any check may exit to HOLD/FAILED. There are no submission or rendering states in this phase.

## Agent-only transaction evidence

F5 reports three separate observations. `BASELINE` is the live UI state before
F5 changes anything. A baseline value that differs from the request is input
to the preparation transaction, not a presubmit failure. `PREPARED` is the
state after F5 applies the expected settings, binds references, fills the
provider prompt, and verifies the complete readback. `RESTORED` is the
persistent Agent settings state after F5 restores and reads back the original
defaults. A successful Agent-only transaction requires PREPARED to match the
request, RESTORED to match BASELINE, and the recovery journal to be `CLEAN`.
The operator report must keep these states distinct and identify the provider
UI prompt digest separately from the approved creative prompt digest.

## Adapter facade

The `FlowPreparationPort` is deliberately limited to inspection and preparation operations. It owns no Generate click, submit, upload, or generation call. Existing transport methods with those capabilities are not reachable through this interface. A later concrete facade may call only reviewed methods from the repository-owned browser and centralized Flow surface modules.

## Readback requirements

Every material value follows `EXPECTED → SET → READ BACK → COMPARE`. The adapter verifies session/account, project, surface, model, mode, duration, aspect, resolution, output count, audio, quality, exact provider references and frame slots, exact positive prompt, negative prompt contract/readback, Generate presence/enabled state, and lease ownership. Any mismatch holds. Generate is only inspected.

## Stable holds

| Code | Meaning |
|---|---|
| `FLOW_SUBMISSION_DISABLED` | Phase F has no submission capability |
| `SCENE_PROMPT_NOT_APPROVED` | Persisted prompt row is not APPROVED |
| `SCENE_PROMPT_CONTRACT_MISMATCH` | Prompt identity/text/source or hash differs from Phase E |
| `FLOW_AUTH_REQUIRED` / `FLOW_AUTH_EXPIRED` / `FLOW_AUTH_CHALLENGE` | Session cannot be used without operator action |
| `FLOW_ACCOUNT_MISMATCH` | Session account differs from configured provider account |
| `FLOW_PROJECT_NOT_CONFIGURED` / `FLOW_PROJECT_MISMATCH` | Dedicated project missing or identity differs |
| `FLOW_SURFACE_CHANGED` | Current editor surface is unknown or changed |
| `FLOW_SETTING_VALUE_MISMATCH` | Any setting readback differs; no normalization |
| `FLOW_REFERENCE_SYNC_REQUIRED` | Exact provider mapping is absent; no automatic upload |
| `FLOW_REFERENCE_BINDING_MISMATCH` / `FLOW_REFERENCE_SLOT_MISMATCH` | Wrong asset identity or frame slot |
| `FLOW_PROMPT_VALUE_MISMATCH` | Positive prompt is changed, truncated, or otherwise unequal |
| `FLOW_NEGATIVE_PROMPT_UNSUPPORTED` / `FLOW_NEGATIVE_PROMPT_MISMATCH` | Separate negative prompt contract unavailable or readback differs |
| `FLOW_GENERATE_CONTROL_MISSING` / `FLOW_GENERATE_CONTROL_DISABLED` | Generate is not ready; no attempt is made to enable or click it |
| `FLOW_LANE_BUSY` | Same provider account/project already has a preparation lane |

## Future generation boundary

Phase G requires a separately approved, exactly-one Generate action and exact current-attempt attribution. Existing operations/render cards/assets must be snapshotted first. UI activity is not proof. Result selection must use provider attempt identity, then validate media and checksum before persistent Drive upload and remote verification. This contract does not implement Phase G.

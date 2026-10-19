# Google Flow V2 Provider Adapter

## Scope and boundaries

Phase F adds a provider-specific preparation boundary after Phase E. Phase E remains the sole owner of Scene resolution, approvals, reference selection, capability checks, prompt compilation, and source/prompt hashes. The adapter consumes its `PreflightResult` plus the persisted `SCENE_PROMPT_VERSION` row and does not recompile or enrich the prompt.

The adapter is keyed by the configured `Provider_ID`; it does not assume a fixed ID. It checks that the provider's service type remains `VIDEO_GENERATION`. The current approved Sheet row labels Google's provider type `VIDEO_GENERATION_PLATFORM`; the router normalizes this existing label to the V2 `VIDEO_GENERATION` service contract without editing schemas or Sheet data. Other video providers can therefore register distinct adapters for the same service type.

`GoogleFlowProviderAdapter.prepare` is backed by `FlowPreparationPort`. The port only permits session inspection, explicit project selection, surface inspection, settings application, exact reference binding, prompt input, negative-prompt handling, and readback. It has no Generate click, submit, upload, keyboard-submit, network-submit, or generation method. This first foundation defines the safe port; a production browser facade must reuse the repository-owned Playwright/Flow lifecycle and centralized selectors.

## Approval and integrity gate

The request must contain a persisted prompt version with `Prompt_Status=APPROVED`. VALIDATED, SUPERSEDED, or other statuses are held before any browser call. Scene, profile, source-context hash, exact positive and negative prompt text, and the canonical Phase E prompt hash must all match. Phase F does not approve or persist prompt rows.

For Agent-only preparation, distinguish `BASELINE` (settings observed before
mutation), `PREPARED` (request settings and provider prompt verified by exact
readback), and `RESTORED` (original persistent defaults read back after the
transaction). Baseline differences such as 16:9 aspect or an old composer
prompt are expected preparation inputs; only a PREPARED mismatch blocks F5.
The transaction restores the settings snapshot and marks the recovery journal
`CLEAN` only after restoration readback succeeds. The approved creative prompt
SHA remains separate from the provider UI prompt SHA, which includes the
deterministic Agent duration instruction.

The reusable row-transition guard lives in `gflow_cli.v2_runtime.prompt_approval`, with the operator lifecycle in `gflow_cli.v2_runtime.prompt_version_lifecycle` and its dedicated Sheets adapter in `google_sheets_transactions`. A persisted `APPROVED` row may be superseded only while preserving its prompt payload and hashes. Reapproval persists a distinct `DRAFT`, validates it against a fresh, error-free Phase E result, then requires another fresh exact match before approval. The two-row approval update uses one `spreadsheets.batchUpdate` request; the implementation re-reads both rows and verifies exactly one `APPROVED` row. Google Sheets has no server-side compare-and-swap, so any detected drift or uncertain readback is a HOLD. Current approval is represented by `Prompt_Status=APPROVED`; this 14-column schema has no `Is_Current` field. The Phase F provider adapter remains read-only with respect to prompt rows.

## Dedicated project and session

The provider account must resolve to one explicitly configured automation project. The adapter selects that ID and compares the returned identity. Missing and mismatched project identities are held. It does not create projects or infer a project from recency, list order, or an open page. Session states other than `FLOW_SESSION_READY` and an account identity mismatch are held. No password, token, cookie, or profile contents enter the request or report.

## Settings, references, and prompt

Settings are taken from Phase E's canonical technical configuration and checked after application and again in final readback. The classic composer uses explicit controls for mapped values it exposes. The Agent-only composer has verified model, generation mode, aspect ratio, and output-count controls; it has no duration, resolution, audio, or quality control. For Agent-only T2V, duration is a required structured Phase E value translated by the Flow adapter into a deterministic duration instruction in the Agent request. The creative prompt remains unchanged and its approval hash remains pinned; the adapter verifies readback of the complete composed request. It does not claim that the Agent settings panel applied or read back duration. Duration is checked against active model capabilities before translation, and the produced clip's actual duration requires post-generation validation. Native Flow audio is not explicitly controlled on this surface. `Audio_Mode=EXTERNAL_TTS` belongs to a separate downstream TTS stage and is not sent to Flow by this adapter. Unsupported explicit resolution/native-audio/quality values hold rather than being dropped. Duration normalization is forbidden.

References are bound only by exact `REFERENCE_PROVIDER_MAP` identities within the expected project. Missing mappings require a separate sync phase. Frame references require exact START_FRAME/END_FRAME slots. The adapter never uploads a reference or chooses by title, filename, thumbnail, or DOM position. TEXT_TO_VIDEO has no implied references.

The approved compiled prompt is passed verbatim and compared exactly after readback; truncation or mutation is a hold. A non-empty negative prompt is only allowed when the selected mode reports a verified separate contract, and it too must be read back exactly. The positive prompt is never modified to carry a negative prompt.

## Presubmit state and kill switch

The final success state is `FLOW_PRESUBMIT_READY`, which requires a ready session, matching account and dedicated project, known surface, exact settings, exact mapped references, exact prompt readback, and Generate present and enabled. Generate is inspected only.

`FLOW_SUBMISSION_ENABLED` is hard-coded false in Phase F. The router-compatible `prepare_request` and `assert_submission_disabled` both raise the machine-readable `FLOW_SUBMISSION_DISABLED` error; enabling a flag cannot create a submit implementation. Phase F has no `SUBMITTED`, `RENDERING`, or `COMPLETED` state.

## Browser ownership and runtime paths

Do not create a second Playwright lifecycle or use external CDP, personal Chrome attachment, screen coordinates, or Computer Use in production. A concrete facade should reuse existing repository browser/session and Flow surface handling, and centralized selector/driver modules. Use a single exclusive account/project lane and the existing `ProfileLease` for its browser-state directory under `RUNTIME_STATE_ROOT/browser-state/<Provider_Account_ID>/`.

Browser state is credential-equivalent: keep it outside Git, Sheets, and Drive; do not print or report its contents; restrict future service ownership. Transient diagnostics belong below `TEMP_ROOT`; operational logs belong in `LOG_ROOT`. Never keep session data in `/var/tmp` long term. V2 runtime roots remain the only root configuration.

## Failure contract

The adapter returns stable hold codes including `SCENE_PROMPT_NOT_APPROVED`, `SCENE_PROMPT_CONTRACT_MISMATCH`, `FLOW_AUTH_REQUIRED`, `FLOW_AUTH_EXPIRED`, `FLOW_AUTH_CHALLENGE`, `FLOW_ACCOUNT_MISMATCH`, `FLOW_PROJECT_NOT_CONFIGURED`, `FLOW_PROJECT_MISMATCH`, `FLOW_SURFACE_CHANGED`, `FLOW_SETTING_VALUE_MISMATCH`, `FLOW_REFERENCE_SYNC_REQUIRED`, `FLOW_REFERENCE_BINDING_MISMATCH`, `FLOW_REFERENCE_SLOT_MISMATCH`, `FLOW_PROMPT_VALUE_MISMATCH`, `FLOW_NEGATIVE_PROMPT_UNSUPPORTED`, `FLOW_GENERATE_CONTROL_MISSING`, `FLOW_GENERATE_CONTROL_DISABLED`, and `FLOW_LANE_BUSY`. No blind retry is performed.

## Phase G boundary (documented only)

Before any future authorized Generate action, capture existing operation, render-card, and asset identities. One explicit action is permitted only in that separately approved phase. A click, disabled button, spinner, or generic `generation_started` flag is not submission proof. Only new attempt-specific provider evidence may acknowledge submission. Resolve the exact attempt to the exact result; download under `TEMP_ROOT/<job>`, validate media, checksum it, upload to persistent Drive, verify remotely, then clean up. Never select the first/newest card or match only title/filename. Phase F implements none of this lifecycle.

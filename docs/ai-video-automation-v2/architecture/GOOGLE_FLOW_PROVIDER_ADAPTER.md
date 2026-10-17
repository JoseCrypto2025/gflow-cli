# Google Flow V2 Provider Adapter

## Scope and boundaries

Phase F adds a provider-specific preparation boundary after Phase E. Phase E remains the sole owner of Scene resolution, approvals, reference selection, capability checks, prompt compilation, and source/prompt hashes. The adapter consumes its `PreflightResult` plus the persisted `SCENE_PROMPT_VERSION` row and does not recompile or enrich the prompt.

The adapter is keyed by the configured `Provider_ID`; it does not assume a fixed ID. It checks that the provider's service type remains `VIDEO_GENERATION`. The current approved Sheet row labels Google's provider type `VIDEO_GENERATION_PLATFORM`; the router normalizes this existing label to the V2 `VIDEO_GENERATION` service contract without editing schemas or Sheet data. Other video providers can therefore register distinct adapters for the same service type.

`GoogleFlowProviderAdapter.prepare` is backed by `FlowPreparationPort`. The port only permits session inspection, explicit project selection, surface inspection, settings application, exact reference binding, prompt input, negative-prompt handling, and readback. It has no Generate click, submit, upload, keyboard-submit, network-submit, or generation method. This first foundation defines the safe port; a production browser facade must reuse the repository-owned Playwright/Flow lifecycle and centralized selectors.

## Approval and integrity gate

The request must contain a persisted prompt version with `Prompt_Status=APPROVED`. VALIDATED, SUPERSEDED, or other statuses are held before any browser call. Scene, profile, source-context hash, exact positive and negative prompt text, and the canonical Phase E prompt hash must all match. Phase F does not approve or persist prompt rows.

## Dedicated project and session

The provider account must resolve to one explicitly configured automation project. The adapter selects that ID and compares the returned identity. Missing and mismatched project identities are held. It does not create projects or infer a project from recency, list order, or an open page. Session states other than `FLOW_SESSION_READY` and an account identity mismatch are held. No password, token, cookie, or profile contents enter the request or report.

## Settings, references, and prompt

Settings are taken from Phase E's canonical technical configuration and checked after application and again in final readback: model, generation mode, duration, aspect ratio, resolution, output count, audio mode, and quality tier. Values must match exactly; duration normalization is forbidden.

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

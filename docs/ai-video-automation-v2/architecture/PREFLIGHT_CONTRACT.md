# Phase E Preflight Contract

## Meaning

PREFLIGHT_PASS means Scene data, account/model/profile/template configuration,
capabilities, parameters, references, and compiled prompt are ready for a
future Provider Adapter handoff. It does not verify provider authentication,
browser login, credits, quota, health, or successful generation.

PREFLIGHT_HOLD means one or more required inputs or rules could not be
resolved confidently. The result is fail-closed: no guessed default, alternate
provider/model/profile, duration clamp, dropped required reference, or
first-row selection.

The entry point requires Scene_ID, Generation_Profile_ID, and runtime
environment. Generation_Profile_ID is explicit; profile defaults are not
silently selected.

## Required gates

1. Scene exists, its Script exists and is APPROVED, the Scene is LOCKED, and
   Scene_Type is GENERATED_VIDEO.
2. The explicit profile exists, is ACTIVE, and matches runtime environment.
3. Provider Account, Provider, and Model exist and are ACTIVE; account and
   model belong to one Provider and the account environment matches the
   profile environment.
4. Prompt Template exists, is ACTIVE, and every constrained provider/model/
   generation-mode field matches the resolved profile.
5. Generation mode and configured technical values pass ACTIVE model
   capability validation. Capability JSON is parsed as data. Prose such as
   “both ratios” is not turned into specific values.
6. Active model parameter mappings for the selected model/mode are unique,
   correctly typed, valid JSON, and within configured allowed values. Required
   canonical values must resolve.
7. Required references resolve to ACTIVE assets with quality PASS and
   persistent Drive identity. Required usage instructions and frame roles
   must be present.
8. Only approved template placeholders are accepted. Unknown placeholders,
   unresolved required positive blocks, and empty positive prompts hold.
9. Context and prompt hashes must be produced from canonical semantic inputs.

## Generation-mode structure

- TEXT_TO_VIDEO can pass without image references.
- FIRST_FRAME_TO_VIDEO requires START_FRAME.
- FIRST_LAST_FRAME_TO_VIDEO requires START_FRAME and END_FRAME.
- INGREDIENTS_TO_VIDEO requires resolved reference ingredients according to
  the selected data contract; no arbitrary image count is invented.
- EXTEND_VIDEO and VIDEO_TO_VIDEO hold with
  SOURCE_VIDEO_INPUT_CONTRACT_MISSING because the locked relation table has no
  source-video role. No schema workaround is introduced.

## Result and stable errors

The result is a provider-neutral object with status, readiness flag, input
IDs, resolved provider/account/model/mode/template IDs, structured technical
configuration, compiled positive/negative prompt, deterministic references,
hashes, and sorted errors/warnings. Each issue has code and message. Sorting
by code then message makes ordering independent of sheet row order.

Important stable codes include SCENE_NOT_FOUND, SCRIPT_NOT_FOUND,
SCRIPT_NOT_APPROVED, SCENE_NOT_LOCKED, SCENE_TYPE_NOT_GENERATABLE,
PROFILE_NOT_FOUND, PROFILE_INACTIVE, PROFILE_ENVIRONMENT_MISMATCH,
PROVIDER_ACCOUNT_NOT_FOUND, PROVIDER_ACCOUNT_INACTIVE, PROVIDER_MISMATCH,
MODEL_NOT_FOUND, MODEL_INACTIVE, TEMPLATE_NOT_FOUND, TEMPLATE_INACTIVE,
TEMPLATE_PROVIDER_MISMATCH, TEMPLATE_MODEL_MISMATCH,
TEMPLATE_GENERATION_MODE_MISMATCH, GENERATION_MODE_UNSUPPORTED,
DURATION_UNSUPPORTED, ASPECT_RATIO_UNSUPPORTED, RESOLUTION_UNSUPPORTED,
CAPABILITY_NOT_MACHINE_ENUMERABLE, REQUIRED_PARAMETER_MAPPING_MISSING,
PARAMETER_VALUE_INVALID, REQUIRED_REFERENCE_MISSING, REFERENCE_NOT_FOUND,
REFERENCE_INACTIVE, REFERENCE_QUALITY_NOT_PASS,
REFERENCE_DRIVE_IDENTITY_MISSING, REFERENCE_USAGE_INSTRUCTION_MISSING,
REFERENCE_SELECTION_AMBIGUOUS, START_FRAME_REQUIRED, END_FRAME_REQUIRED,
SOURCE_VIDEO_INPUT_CONTRACT_MISSING, PROMPT_TEMPLATE_UNKNOWN_PLACEHOLDER,
PROMPT_PLACEHOLDER_UNRESOLVED, COMPILED_PROMPT_EMPTY,
CONTINUITY_CYCLE_DETECTED, and CONTEXT_HASH_FAILED.

Warnings remain explicit. In particular, CAPABILITY_NOT_MACHINE_ENUMERABLE
never becomes an assumed provider capability. If the uncertain value is
required for safe readiness, the result is HOLD as well.

Duration resolves from `SCENES.Duration_Target_Sec`, then the optional
`GENERATION_PROFILE_MASTER.Default_Duration_Sec`. If both are blank and the
active model map marks duration required, Phase E holds with
`REQUIRED_PARAMETER_MAPPING_MISSING`; it does not infer a duration. For the
Agent-only Google Flow T2V adapter, this structured duration becomes a dynamic
plain-language directive because the Agent settings panel has no duration
control. The directive is not an explicit setting readback, so Phase G must
validate the resulting media duration. Aspect ratio resolves in order from the
Scene override, Series default, then Generation Profile default; the model
parameter map validates/translates the result but does not own a duplicate
profile default. `Audio_Mode=EXTERNAL_TTS` describes downstream TTS and is not
converted to a Google Flow `AUDIO_ENABLED` value; native Flow audio remains
unspecified unless an explicit verified provider contract exists.

## Security and execution boundary

Auth_Profile_Key is treated as a logical reference only. Phase E does not
resolve it, test credentials, read cookies, inspect browser state, or return
the logical reference in results. No credential or local path may enter
results, logs, hashes, or snapshots.

Core logic consumes in-memory normalized catalog rows. It has no Sheets,
Drive, browser, provider API, network, or production-job write path. Provider
authentication, credit/quota availability, and live health checks belong to
future runtime checks. No provider execution, asset upload, production row, or
live spreadsheet write occurs in Phase E.

## Prompt candidate

On PASS, a separate in-memory builder can produce a SCENE_PROMPT_VERSION
schema-compatible candidate using an explicit prompt ID and positive version.
Its status is VALIDATED and it has no Approved_At. It is not persisted. Atomic
version allocation and explicit approval are later persistence/workflow
responsibilities. See PROMPT_BUILDER_ARCHITECTURE.md for resolution,
inheritance, blocks, hashes, and the future adapter boundary.

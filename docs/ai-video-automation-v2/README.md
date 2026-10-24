# AI Video Automation Master Data V2

This documentation set describes the source-controlled data foundation for the approved V2 workflow. It is isolated from gflow-cli runtime code and does not change the Google Flow client, production n8n, or VPS deployment.

## Source of truth

The native database is [AI_VIDEO_AUTOMATION_MASTER_V2](https://docs.google.com/spreadsheets/d/1wtoIBfeHNdGYK6cyhDfKPG6W3TnnfL4QDvCAtJBX27Q/edit) in the Google Drive `AI Automation/00_DATABASE` folder. It contains 23 Master tabs, 8 transaction tabs, and 3 relation tabs (34 total). The eleven transaction tabs contain no production data; the five new tabs have schema headers and unchecked checkbox defaults only.

## Contents

- `../../schemas/master/`: 23 JSON Schemas for the master tables.
- `../../schemas/transaction/`: EPISODE_MASTER, SCRIPT_MASTER, SCENES, SCENE_PROMPT_VERSION, PRODUCTION_JOBS, SCENE_JOBS, TTS_JOBS, and FINAL_OUTPUT.
- `../../schemas/relation/`: SCENE_CHARACTER_MAP, SCENE_DIALOGUE_LINES, and SCENE_REFERENCE_MAP.
- `database/MASTER_DATA_DICTIONARY.md`: keys, field types, FK status, and constraints.
- `architecture/`: ownership, relationship, provider contract, and Git baseline notes.
- `../../scripts/validation/validate_master_data.py`: read-only Master CSV validation.
- `../../scripts/validation/validate_v2_data_architecture.py`: schema bundle and CSV validator for all 34 tables.
- `../../src/gflow_cli/v2_runtime/`: isolated, no-provider-I/O V2 runtime foundations.

## Data guardrails

- Stable ID text keys; ISO 8601 UTC timestamps.
- No sample scene, episode, production job, output, or post rows.
- Provider/model capability records are factual reference data with source and verification timestamp.
- No credentials, keys, or tokens. Flow video generation and Flow credits were not used.

## V2.1 multi-provider extension

The four added Master tables are PROVIDER_ACCOUNT_MASTER, MODEL_PARAMETER_MAP, GENERATION_PROFILE_MASTER, and PROVIDER_COST_MASTER. Provider-neutral profiles let future scenes/jobs resolve account, model, mode, prompt template, and defaults without provider-specific fields. No fake production rows or unverified cost rows are included.

The spreadsheet display timezone is Asia/Bangkok. Database timestamps remain ISO 8601 UTC. Auth_Profile_Key is a logical secret reference only; credentials, tokens, cookies, browser sessions, and private keys are prohibited. No video generation, Flow credits, or paid generation API calls were used.

## Phase B: creative data architecture

EPISODE_MASTER, SCRIPT_MASTER, and SCENES are transaction tables. SCENE_CHARACTER_MAP, SCENE_DIALOGUE_LINES, and SCENE_REFERENCE_MAP are relation tables. Scene references now have an active Scene_ID foreign key. They contain no sample content. See the [data dictionary](database/MASTER_DATA_DICTIONARY.md) and [relationship map](architecture/RELATIONSHIP_MAP.md).

## Phase C: runtime foundation

The initial `v2_runtime` package defined environment configuration, per-job temp workspaces, logical asset references, verification, disk admission checks, catalog-based profile validation, and adapter boundaries. Later production execution adapters are documented below. See [runtime foundation](architecture/V2_RUNTIME_FOUNDATION.md), [gap report](architecture/GFLOW_V2_ARCHITECTURE_GAP_REPORT.md), and [legacy compatibility contract](architecture/LEGACY_API_COMPATIBILITY.md).

## Production Execution Data V1

Five transaction schemas define immutable scene prompt versions, production runs, scene and TTS attempts, and versioned final artifacts. The TTS schema includes separate hashes for exact input text and resolved synthesis configuration so retries cannot reuse output after voice-setting drift. See `database/PRODUCTION_EXECUTION_DATA_DICTIONARY.md` for the operational contract. Posting and analytics remain out of scope.

## Phase E: Prompt Builder and Preflight

The provider-neutral compiler accepts Scene_ID, explicit Generation_Profile_ID,
and runtime environment. It resolves creative context and inheritance, validates
profile/model capabilities and parameters, resolves Drive-backed references,
renders the approved template vocabulary, and returns deterministic hashes plus
a machine-readable PASS or HOLD result. A PASS may yield an in-memory
SCENE_PROMPT_VERSION candidate with status VALIDATED; it does not approve or
persist the candidate. See [Prompt Builder Architecture](architecture/PROMPT_BUILDER_ARCHITECTURE.md)
and [Preflight Contract](architecture/PREFLIGHT_CONTRACT.md). The live Sheet
remains unchanged and the phase ends before provider execution.

Persisting a candidate is a separate operator action implemented by
`scripts/operations/reconcile_prompt_version.py`. It creates a new immutable
`DRAFT`, validates it against fresh Phase E, and approves only after another
fresh exact match. Approval switches the prior/current status cells through one
atomic Sheets batch and verifies readback. In the approved 14-column schema,
`Prompt_Status=APPROVED` is the current marker; there is no `Is_Current` column.
The operator resolves `TEST` or `PROD` from the selected ACTIVE generation
profile in its fresh live snapshot and rejects missing, duplicate, inactive, or
unsupported profile environments.

## Phase G: controlled local execution foundation

The new Phase G domain controller is distinct from the submission-free F5
preparation port. It pins each attempt to a fresh approved prompt, records a
single Generate action, enforces the Bangkok-day 50-credit ceiling, preserves
settings recovery, and supplies full media and remote-persistence verification
gates. The live D2 Flow port composes the isolated TEST profile, Agent-only
settings controls, migrated batchexecute parser, and exact media recovery path.
The production Drive storage adapter remains fail-closed; D2 persistence uses
the connected Drive connector with separate remote download/hash verification. See
[Phase G D2 controlled execution](architecture/PHASE_G_D2_CONTROLLED_EXECUTION.md).
The live port requires an expected TEST account identity and fails closed on
non-English UI locales until translated control behavior is verified. A
one-shot, credit-consuming regression is available at
`tests/e2e/test_phase_g_d2_live.py` and remains opt-in.

## TTS and media finalization V2

`src/gflow_cli/v2_runtime/segmented_tts.py` calls the existing authenticated
segmented-v2 service on loopback. `TTS_API_KEY` is injected through the runtime
environment and is never written to Sheets or logged. `/health` is public for
health monitoring, while the synthesis POST requires `x-api-key`. The response
must identify a completed provider job and an audio file contained under the
configured `TTS_OUTPUT_ROOT`; the service and caller therefore run on the same
host or share that approved output path. Uncertain submits remain on HOLD and
are not retried automatically. A completed `TTS_JOBS` row is reused only when
its input-text and synthesis-configuration hashes, persistent Drive identity,
hash, size, duration, remote verification, and
cleanup evidence are complete.

Provider audio is probed and fully decoded, converted once to mono 48 kHz PCM,
then uploaded to the approved Drive folder and verified by a separate download
and SHA-256 comparison. The internal FFmpeg merger keeps the video timeline
authoritative, pads shorter audio, rejects longer narration with
`TTS_DURATION_MISMATCH`, prefers video stream copy, and uses a controlled H.264
fallback. Final audio is AAC at 48 kHz mono and 192 kbit/s. Both output streams
are fully decoded and hashed. `FINAL_OUTPUT` records the assembly manifest hash
and merge strategy. The V2 merger does not use `-shortest`; it does not
call the historical `/audio/enhance` service, avoiding another lossy audio
transform until a separately validated need exists.

For a scene whose persisted `Dialogue_Mode` is `NONE`, NO_AUDIO finalization
requires no linked dialogue rows and a fully decoded source MP4 with no audio
stream. It creates no `TTS_JOBS` row and records a remotely verified `FINAL_VIDEO`
in the existing `FINAL_OUTPUT` schema. The input video is retained locally.
Speech is never inferred from prompt text. These adapters do not add schema
fields or change the Flow generation CLI/MCP surface.

The current one-scene production NO_AUDIO operator and its exact gate are
documented in [Production Scene Orchestrator V2](operations/PRODUCTION_SCENE_ORCHESTRATOR_V2.md).

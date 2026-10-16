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

The `v2_runtime` package defines environment configuration, per-job temp workspaces, logical asset references, verification, disk admission checks, catalog-based profile validation, and adapter boundaries. It does not start a worker or listener, authenticate to Drive, call a provider, or generate media. See [runtime foundation](architecture/V2_RUNTIME_FOUNDATION.md), [gap report](architecture/GFLOW_V2_ARCHITECTURE_GAP_REPORT.md), and [legacy compatibility contract](architecture/LEGACY_API_COMPATIBILITY.md).

## Production Execution Data V1

Five transaction schemas define immutable scene prompt versions, production runs, scene and TTS attempts, and versioned final artifacts. See `database/PRODUCTION_EXECUTION_DATA_DICTIONARY.md` for the operational contract. New tabs remain header-only; provider adapters, prompt execution, posting, and analytics are out of scope.

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

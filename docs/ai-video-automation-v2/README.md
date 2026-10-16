# AI Video Automation Master Data V2

This documentation set describes the source-controlled data foundation for the approved V2 workflow. It is isolated from gflow-cli runtime code and does not change the Google Flow client, production n8n, or VPS deployment.

## Source of truth

The native database is [AI_VIDEO_AUTOMATION_MASTER_V2](https://docs.google.com/spreadsheets/d/1wtoIBfeHNdGYK6cyhDfKPG6W3TnnfL4QDvCAtJBX27Q/edit) in the Google Drive `AI Automation/00_DATABASE` folder. It contains 23 master tabs, 3 transaction tabs, and 3 relation tabs. All six Phase B tabs are blank except for schema headers.

## Contents

- `../../schemas/master/`: 23 JSON Schemas for the master tables.
- `../../schemas/transaction/`: EPISODE_MASTER, SCRIPT_MASTER, and SCENES.
- `../../schemas/relation/`: SCENE_CHARACTER_MAP, SCENE_DIALOGUE_LINES, and SCENE_REFERENCE_MAP.
- `database/MASTER_DATA_DICTIONARY.md`: keys, field types, FK status, and constraints.
- `architecture/`: ownership, relationship, provider contract, and Git baseline notes.
- `../../scripts/validation/validate_master_data.py`: read-only Master CSV validation.
- `../../scripts/validation/validate_v2_data_architecture.py`: schema bundle and CSV validator for all 29 tables.
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

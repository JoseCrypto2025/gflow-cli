# AI Video Automation Master Data V2

This documentation set describes the source-controlled data foundation for the approved V2 workflow. It is isolated from gflow-cli runtime code and does not change the Google Flow client, production n8n, or VPS deployment.

## Source of truth

The native master database is [AI_VIDEO_AUTOMATION_MASTER_V2](https://docs.google.com/spreadsheets/d/1wtoIBfeHNdGYK6cyhDfKPG6W3TnnfL4QDvCAtJBX27Q/edit) in the Google Drive `AI Automation/00_DATABASE` folder. The spreadsheet contains 23 master tabs. The local JSON schemas define their columns and the additional `SCENE_REFERENCE_MAP` relation table.

## Contents

- `../../schemas/master/`: 23 JSON Schemas for the master tables.
- `../../schemas/relation/SCENE_REFERENCE_MAP.schema.json`: relation schema only; no scene rows are created.
- `database/MASTER_DATA_DICTIONARY.md`: keys, field types, FK status, and constraints.
- `architecture/`: ownership, relationship, provider contract, and Git baseline notes.
- `../../scripts/validation/validate_master_data.py`: read-only validator for all 23 master CSV exports, including schema, JSON, ID, unique-key, FK, capability, cost, and secret checks.

## Data guardrails

- Stable ID text keys; ISO 8601 UTC timestamps.
- No sample scene, episode, production job, output, or post rows.
- Provider/model capability records are factual reference data with source and verification timestamp.
- No credentials, keys, or tokens. Flow video generation and Flow credits were not used.

## V2.1 multi-provider extension

The four added Master tables are PROVIDER_ACCOUNT_MASTER, MODEL_PARAMETER_MAP, GENERATION_PROFILE_MASTER, and PROVIDER_COST_MASTER. Provider-neutral profiles let future scenes/jobs resolve account, model, mode, prompt template, and defaults without provider-specific fields. No fake production rows or unverified cost rows are included.

The spreadsheet display timezone is Asia/Bangkok. Database timestamps remain ISO 8601 UTC. Auth_Profile_Key is a logical secret reference only; credentials, tokens, cookies, browser sessions, and private keys are prohibited. No video generation, Flow credits, or paid generation API calls were used.

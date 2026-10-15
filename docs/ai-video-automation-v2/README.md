# AI Video Automation Master Data V2

This documentation set describes the source-controlled data foundation for the approved V2 workflow. It is isolated from gflow-cli runtime code and does not change the Google Flow client, production n8n, or VPS deployment.

## Source of truth

The native master database is [AI_VIDEO_AUTOMATION_MASTER_V2](https://docs.google.com/spreadsheets/d/1wtoIBfeHNdGYK6cyhDfKPG6W3TnnfL4QDvCAtJBX27Q/edit) in the Google Drive `AI Automation/00_DATABASE` folder. The spreadsheet contains 19 master tabs. The local JSON schemas define their columns and the additional `SCENE_REFERENCE_MAP` relation table.

## Contents

- `../../schemas/master/`: 19 JSON Schemas for the master tables.
- `../../schemas/relation/SCENE_REFERENCE_MAP.schema.json`: relation schema only; no scene rows are created.
- `database/MASTER_DATA_DICTIONARY.md`: keys, field types, FK status, and constraints.
- `architecture/`: ownership, relationship, provider contract, and Git baseline notes.
- `../../scripts/validation/validate_master_data.py`: read-only CSV-export validator for headers, required fields, allowed values, types, primary keys, active foreign keys, and the polymorphic reference map.

## Data guardrails

- Stable ID text keys; ISO 8601 UTC timestamps.
- No sample scene, episode, production job, output, or post rows.
- Provider/model capability records are factual reference data with source and verification timestamp.
- No credentials, keys, or tokens. Flow video generation and Flow credits were not used.
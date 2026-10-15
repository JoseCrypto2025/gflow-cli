# Data Architecture V2

## Ownership

- Development source, schemas, validation, scripts, tests, docs, and temporary development artifacts live in this repository. V2 schemas are under `schemas/`, validation is under `scripts/validation/`, and V2 documentation is under `docs/ai-video-automation-v2/`.
- Google Drive is persistent source of truth for the native master sheet, reference assets, content, production assets, final videos, thumbnails, reports, and archive. No persistent Temp folder is created on Drive.
- Future n8n on the VPS orchestrates production. VPS holds temporary files only; completed assets must be verified in Drive before job completion. Production n8n and VPS are out of scope for this foundation phase.
- Production secrets belong in n8n credentials or secure VPS secret management, never in Sheets, docs, examples, or committed code.

## Data model

- The database contains 19 Master tables. `SCENE_REFERENCE_MAP` is prepared as a relation schema only.
- Active foreign keys are documented and validation-script checked. Episode and scene keys are deferred. Sheets is not treated as a relational database.
- `REFERENCE_ENTITY_MAP.Entity_ID` is polymorphic and resolved from `Entity_Type`.
- `MODEL_CAPABILITY_MAP` stores verified capabilities as extensible rows. No changing prices are hard-coded.
- Prompt fields are curated fragments and are not copied from descriptions by default.

## Git baseline

- Canonical fork: `https://github.com/JoseCrypto2025/gflow-cli`; upstream: `https://github.com/ffroliva/gflow-cli`.
- The new `automation-v2` branch starts at upstream default branch `develop`, commit `38ce8ac8f299e19c9d35e1def30ab7339ad64cdc`. Details are in `docs/ai-video-automation-v2/architecture/GIT_BASELINE.md`.

## Google Drive source of truth

The native database is [AI_VIDEO_AUTOMATION_MASTER_V2](https://docs.google.com/spreadsheets/d/1wtoIBfeHNdGYK6cyhDfKPG6W3TnnfL4QDvCAtJBX27Q/edit), stored in `AI Automation/00_DATABASE`. It has exactly 19 master tabs; `SCENE_REFERENCE_MAP` remains schema-only.

## Future flow

Google Sheets -> n8n VPS -> Flow Worker -> provider -> temporary VPS asset -> verify -> upload to Drive -> update Sheets -> clean VPS temp. No production workflow or deployment is implemented in this phase.

## Next phase

EPISODE_MASTER, SCRIPT_MASTER, SCENES, PRODUCTION_JOBS, SCENE_JOBS, TTS_JOBS, FINAL_OUTPUT, POST_QUEUE, POST_RESULTS, and ANALYTICS.
# Data Architecture V2

## Ownership

- Development source, schemas, validation, scripts, tests, docs, and temporary development artifacts live in this repository. V2 schemas are under `schemas/`, validation is under `scripts/validation/`, and V2 documentation is under `docs/ai-video-automation-v2/`.
- Google Drive is persistent source of truth for the native master sheet, reference assets, content, production assets, final videos, thumbnails, reports, and archive. No persistent Temp folder is created on Drive.
- Future n8n on the VPS orchestrates production. VPS holds temporary files only; completed assets must be verified in Drive before job completion. Production n8n and VPS are out of scope for this foundation phase.
- Production secrets belong in n8n credentials or secure VPS secret management, never in Sheets, docs, examples, or committed code.

## Data model

- The spreadsheet has 23 Master tables, 8 transaction tables, and 3 relation tables (34 tabs total). `EPISODE_MASTER`, `SCRIPT_MASTER`, and `SCENES` are creative transaction data; the three `SCENE_*` relation tabs normalize scene relationships.
- Active foreign keys are documented and validated. `SCENE_REFERENCE_MAP.Scene_ID` is active. Sheets is not treated as a relational database.
- `REFERENCE_ENTITY_MAP.Entity_ID` is polymorphic and resolved from `Entity_Type`.
- `MODEL_CAPABILITY_MAP` stores verified capabilities as extensible rows. No changing prices are hard-coded.
- Prompt fields are curated fragments and are not copied from descriptions by default.

## Git baseline

- Canonical fork: `https://github.com/JoseCrypto2025/gflow-cli`; upstream: `https://github.com/ffroliva/gflow-cli`.
- The new `automation-v2` branch starts at upstream default branch `develop`, commit `38ce8ac8f299e19c9d35e1def30ab7339ad64cdc`. Details are in `docs/ai-video-automation-v2/architecture/GIT_BASELINE.md`.

## Google Drive source of truth

The native database is [AI_VIDEO_AUTOMATION_MASTER_V2](https://docs.google.com/spreadsheets/d/1wtoIBfeHNdGYK6cyhDfKPG6W3TnnfL4QDvCAtJBX27Q/edit), stored in `AI Automation/00_DATABASE`. It has 34 tabs: 23 Master, 8 transaction, and 3 relation. All 11 transaction tabs have no production rows; the five new tabs contain schema headers and unchecked checkbox defaults only.

## Future flow

Google Sheets -> future orchestration -> V2 worker -> provider adapter -> temporary VPS asset -> verify remote upload -> update Sheets -> clean VPS temp. This repository phase defines contracts only; no production workflow or deployment is implemented.

## Phase E prompt and preflight boundary

Phase E resolves a locked GENERATED_VIDEO Scene plus an explicit
Generation_Profile_ID into a provider-neutral, capability-validated prompt and
structured adapter parameters. The result is PASS (ready for a future adapter
handoff, subject to future live authentication/health checks) or HOLD. An
optional in-memory SCENE_PROMPT_VERSION candidate is VALIDATED, never
APPROVED, and no live table is written. See
[Prompt Builder Architecture](PROMPT_BUILDER_ARCHITECTURE.md) and
[Preflight Contract](PREFLIGHT_CONTRACT.md). The current locked reference-role
schema cannot represent source video, so EXTEND_VIDEO and VIDEO_TO_VIDEO hold.

## Next phase

Posting and analytics remain future work; the five Production Execution Data V1 transaction tables are defined and header-only. The Phase B episode/script/scene design is complete but contains no real story data.

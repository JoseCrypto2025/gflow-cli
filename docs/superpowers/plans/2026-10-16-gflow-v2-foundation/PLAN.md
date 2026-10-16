# Gflow V2 Data and Go-Live Runtime Foundation Plan

> **For agentic workers:** This mission is authorized end to end. Keep all work on `automation-v2`. Do not call providers, generate video, touch production n8n/relay/BotTrade, or deploy to VPS. Run offline tests and the repository quality gates before each checkpoint commit.

**Goal:** Add the approved Episode/Script/Scene relational layer and an isolated provider-neutral V2 runtime foundation that can be configured for VPS operation without depending on a Windows workstation.

**Architecture:** Keep the existing `gflow_cli` Flow client and public CLI unchanged. Add canonical row schemas and validation in the existing schema/validation structure, then put the runtime foundation behind a new `gflow_cli.v2_runtime` package: fail-closed settings, logical asset references, a storage port, job-scoped temp workspaces, remote verification, disk guard, Master Data catalog ports, provider service-type routing, and legacy request contracts. Real provider calls, secret authentication, listener binding, n8n changes, and deployment remain out of scope.

**Predict verdict:** CAUTION — 7/10. The upstream PLAN ADR #12 intentionally removed generic provider indirection until a second provider exists; this mission authorizes that second-provider boundary. Mitigation: isolate all new routing under `v2_runtime`, do not change `FlowApiClient`, and test only fake/local adapters.

**Risk register:**

| Severity | Risk | Mitigation |
|---|---|---|
| Critical | Local cleanup could escape a job workspace or delete unverified output | Accept only validated job IDs, verify real-path containment and symlink state, require remote verification before SUCCESS cleanup, and keep RUNNING/UNKNOWN untouched |
| Critical | Provider or model mismatch could route to an incompatible service | Resolve profile/account/model/capabilities from Master Data ports and check adapter service type before invocation |
| High | Old Flow CLI assumptions conflict with the always-on VPS target | Keep V2 isolated; no edits to existing Flow runtime modules unless a shared type is unavoidable and separately tested |
| High | Legacy duration 30 could be sent to a model supporting only short clips | Return a scene-planning decision from capability-aware duration planning; never pass unsupported duration through |
| High | Google Drive API credentials are unavailable for production storage implementation | Implement and test the contract/mock only; fail closed when the real provider adapter is not registered |

## File structure

### New files

```text
schemas/transaction/EPISODE_MASTER.schema.json
schemas/transaction/SCRIPT_MASTER.schema.json
schemas/transaction/SCENES.schema.json
schemas/relation/SCENE_CHARACTER_MAP.schema.json
schemas/relation/SCENE_DIALOGUE_LINES.schema.json
src/gflow_cli/v2_runtime/
  config.py, assets.py, storage.py, workspace.py, verification.py,
  disk_guard.py, catalog.py, providers.py, legacy_api.py, __init__.py
tests/v2_runtime/
  test_config.py, test_assets.py, test_storage.py, test_workspace.py,
  test_verification.py, test_disk_guard.py, test_providers.py, test_legacy_api.py
.env.example
docs/superpowers/plans/2026-10-16-gflow-v2-foundation/SCENARIO.md
docs/superpowers/plans/2026-10-16-gflow-v2-foundation/PLAN.md
docs/ai-video-automation-v2/architecture/GFLOW_V2_ARCHITECTURE_GAP_REPORT.md
docs/ai-video-automation-v2/architecture/V2_RUNTIME_FOUNDATION.md
docs/ai-video-automation-v2/architecture/LEGACY_API_COMPATIBILITY.md
```

### Modified files

```text
schemas/relation/SCENE_REFERENCE_MAP.schema.json
scripts/validation/validate_master_data.py (expand into separate all-schema/table validator without breaking V2.1 validation)
docs/ai-video-automation-v2/README.md
docs/ai-video-automation-v2/database/MASTER_DATA_DICTIONARY.md
docs/ai-video-automation-v2/architecture/RELATIONSHIP_MAP.md
docs/ai-video-automation-v2/architecture/DATA_ARCHITECTURE_V2.md
docs/ai-video-automation-v2/architecture/GOOGLE_FLOW_DATA_CONTRACT.md
pyproject.toml / .gitignore only if needed for package and test discovery
```

## Phase B — Data architecture checkpoint

### Task B1 — Schema and validator test scaffold

**What:** Pin table fields, ID formats, keys, uniqueness, conditional dialogue rules, inherited-value resolution, and Scene Reference FK activation before implementation.

**Steps:**
- [ ] Add schema/validator tests for all six definitions and existing 23 Master schemas.
- [ ] Add tests for PK patterns, unique composites, active/deferred FK rules, and the dialogue speaker condition.
- [ ] Confirm original Master schema files remain byte-identical.

### Task B2 — Six canonical schemas and validator

**What:** Add three transaction schemas, two new relation schemas, activate the existing Scene Reference FK, and validate all 29 tabs without creating records.

**Steps:**
- [ ] Preserve the exact requested field order and relationship names.
- [ ] Record only explicit allowed vocabularies; leave unspecified lifecycle vocabularies documented rather than invented.
- [ ] Add the conditional CHARACTER speaker rule and unique composite keys.
- [ ] Ensure the schema loader counts 23 Master, 5 other new tables, and one updated relation schema without classifying transactions as Master data.

### Task B3 — Google Sheet tabs

**What:** Add six blank tabs to the grounded spreadsheet and style them consistently with the existing V2 tabs.

**Steps:**
- [ ] Add exact headers, freeze row 1, add filters, readable widths, and validated dropdown/checkbox controls.
- [ ] Add no sample content and do not modify existing Master cell values.
- [ ] Read back metadata, headers, validations, filter/freeze state, and timezone.

### Task B4 — Data documentation and validation gate

**Steps:**
- [ ] Update dictionary, relationship map, data architecture, Google Flow contract, and README.
- [ ] Document Scene grain, inheritance, creative-vs-execution boundaries, and exact UTC timestamp policy.
- [ ] Validate schemas, sheet headers, table count, and blank-row state.
- [ ] Commit and push only Phase B files as `feat: establish episode script and scene data architecture`.

## Phase C — Runtime checkpoint

### Task C1 — Runtime tests first

**Steps:**
- [ ] Add offline tests for fail-closed config, Windows path rejection in production, logical AssetReference, workspace isolation, traversal/symlink refusal, state-aware retention, remote verification, and disk guard PASS/HOLD.
- [ ] Add fake-catalog/fake-adapter tests for Generation Profile resolution, capability checks, provider service-type mismatch, and legacy duration planning.
- [ ] Add tests proving no provider call occurs when configuration/profile/verification is invalid.

### Task C2 — Runtime implementation

**Steps:**
- [ ] Implement immutable settings for development/staging/production with required production values and no secret fields.
- [ ] Define AssetReference and storage Protocol operations; provide local/mock storage and an unconfigured Google Drive contract adapter that fails closed.
- [ ] Implement isolated workspaces with real-path containment, symlink refusal, controlled retention, and SUCCESS verification gate.
- [ ] Implement remote metadata verification and structured disk guard outcomes.
- [ ] Define Master Data catalog ports and provider service-type router; profile resolution must use catalog rows, capabilities, and parameter mappings.
- [ ] Define legacy request/response DTOs and capability-aware duration planning without starting an HTTP listener.

### Task C3 — Documentation, config example, and audit

**Steps:**
- [ ] Add placeholder-only `.env.example` and document all runtime variables.
- [ ] Document storage, assets, job lifecycle, temp layout, disk guard, adapter boundary, legacy API, deployment paths, and zero local-PC dependency.
- [ ] Re-run the hard-coded-path scan and classify each remaining production/dev/docs/test occurrence.
- [ ] Verify VPS findings remain observational only; do not use SSH write commands.

### Task C4 — Full offline validation and checkpoint

**Steps:**
- [ ] Run applicable hygiene, doc-link, ruff, format, pyright, schema checks, and unit tests; record any unavailable tool or pre-existing failure.
- [ ] Do not run generation E2E, paid APIs, browser automation that can generate, or live video tests.
- [ ] Confirm Master V2.1 and Sheets remain valid; no secrets were added.
- [ ] Commit and push only Phase C files as `feat: establish gflow v2 go-live runtime foundation`.
- [ ] Verify branch, remote SHA, clean worktree, and untouched protected production systems.

## Definition of done

- [ ] All six requested schemas and the active Scene Reference relation validate.
- [ ] Spreadsheet has 29 correctly named tabs with exact headers and no fake rows.
- [ ] Runtime contracts pass safety and routing tests without paid provider calls.
- [ ] Local and remote branch contain separate Phase B and Phase C commits.
- [ ] No production n8n, BotTrade, relay, VPS filesystem, or local-PC runtime dependency was changed.
- [ ] `/gflow:check` equivalent gates are green or exact unavailable/pre-existing failures are documented.

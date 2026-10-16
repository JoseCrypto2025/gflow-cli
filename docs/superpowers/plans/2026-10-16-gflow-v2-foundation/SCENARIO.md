# Scenario: Gflow V2 Data and Runtime Foundation

## Coverage map

- D1 auth/session: no provider credentials or authentication transport is implemented; `Auth_Profile_Key` remains a logical reference.
- D2 WAF/reCAPTCHA, D3 selectors, D5 browser concurrency, D9 Flow transport, D10 browser mode: skipped because this foundation does not call Google Flow, launch a browser, or generate media.
- D4 queue/resume: job lifecycle is represented by contracts only; RUNNING and UNKNOWN are never automatically cleaned.
- D6 data layer: six new schemas, active Scene Reference FK, and conditional dialogue relationships are validated without modifying existing master rows.
- D7 errors: config, disk, storage verification, and provider resolution return typed, machine-readable HOLD/REJECT results.
- D8 paths: production paths must be absolute POSIX paths; temp cleanup rejects traversal and symlink escape.
- D11 inputs: legacy duration is resolved against capability values and never passed through blindly.
- D12 observability: disk checks and lifecycle decisions carry stable fields and UTC timestamps.
- D13 MCP: no CLI/MCP command is added; legacy API compatibility is a contract, and no listener is bound.

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D6 | Existing master sheet contains no rows for new episode/script/scene tables | High | Six blank tabs are added with exact schema headers; no fabricated records | Integration |
| 2 | D6 | SCENE_REFERENCE_MAP references a scene after SCENES exists | High | `Scene_ID` becomes required ACTIVE FK and is checked by validator | Unit |
| 3 | D6 | Dialogue speaker is CHARACTER but Character_ID is blank | High | Validation rejects the row; non-character speakers cannot carry Character_ID | Unit |
| 4 | D8 | Production config contains `D:\media` or `C:/media` | Critical | Configuration is rejected before runtime work | Unit |
| 5 | D8 | Job ID is empty, `..`, absolute, or contains traversal | Critical | Workspace creation/cleanup rejects with machine-readable reason | Unit |
| 6 | D8 | Job directory is a symlink pointing outside TEMP_ROOT | Critical | Cleanup rejects and leaves target and external data untouched | Unit |
| 7 | D4 | Workspace state is RUNNING or UNKNOWN | Critical | Cleanup is held regardless of age | Unit |
| 8 | D4 | Successful upload reports success but remote metadata is absent or size/checksum differs | Critical | Remote verification fails; local output remains | Unit |
| 9 | D11 | Legacy request asks for duration 30 with a model whose supported durations are 4/6/8 | High | Return an explicit multi-scene planning requirement; do not submit a 30-second provider request | Unit |
| 10 | D6 | Generation profile references a disabled provider/account/model or mismatched provider | Critical | Router returns HOLD/REJECT without adapter invocation | Unit |
| 11 | D13 | VIDEO_GENERATION request is matched to a TTS adapter | Critical | Router rejects service-type mismatch before execution | Unit |
| 12 | D7 | Required production config is missing or malformed | Critical | Startup config resolution fails closed with field/reason and no fallback | Unit |
| 13 | D12 | Free space is below configured threshold plus estimate | High | Disk guard returns DISK_GUARD_HOLD and structured measurements; no process is killed | Unit |

## Must-cover before merge (Critical + High)

1. All six schemas parse and their PK/FK/unique constraints are enforced.
2. Scene Reference FK is active and dialogue conditional FK rules are enforced.
3. Production configuration refuses missing values, Windows drive paths, and unsafe roots.
4. Temp cleanup refuses traversal, symlink escape, cross-job targets, RUNNING, and UNKNOWN state.
5. Cleanup is allowed only after remote existence and supported size/checksum verification pass.
6. Disk guard PASS/HOLD cases and service-type/provider-profile resolution are covered.
7. Legacy duration 30 is not passed directly to a 4/6/8-second provider.

## Deferred (Medium + Low — log as issues, not blockers)

1. Real Google Drive authentication and network operations remain outside this phase; only the provider contract and local mock are implemented.
2. Unknown-job stale cleanup is deferred; UNKNOWN remains protected until an explicit stale policy is specified.
3. No live Flow or paid provider E2E is run. This change does not call a provider or touch a generation surface.

## Suggested BDD scenarios

```gherkin
Feature: Gflow V2 temporary workspace safety
  Scenario: Refuse symlink escape during cleanup
    Given a job workspace path resolves outside TEMP_ROOT
    When cleanup is requested for that job
    Then cleanup is rejected and the outside target remains intact

Feature: Gflow V2 provider-neutral routing
  Scenario: Reject a service type mismatch
    Given a Generation Profile resolves to a video generation provider
    And the registered adapter supports TTS only
    When the video request is routed
    Then the router returns a machine-readable HOLD without invoking the adapter
```

## Known-issues cross-reference

The root PLAN ADR #12 removed the legacy provider indirection and deferred its return until a second provider is added. This mission explicitly establishes the V2 multi-provider boundary, so implementation remains isolated in a new V2 package and leaves `FlowApiClient` and the legacy CLI unchanged.

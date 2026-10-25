# Production Execution Data V1 Dictionary

Five transaction tables capture immutable, retry-safe execution state. UTC ISO 8601 timestamps are canonical. Google Sheets stores structured state, not verbose runtime logs. New tables are header-only until an execution phase is approved.

## SCENE_PROMPT_VERSION

Grain: one compiled prompt version for one Scene and Generation Profile.

Primary key: `Scene_Prompt_ID`

Unique: Scene_ID + Prompt_Version

Current-version rule: `Prompt_Status=APPROVED` identifies the current row for
one Scene/Profile pair. This approved 14-column schema has no persisted
`Is_Current` field; no extra column is written.

| Field | Type | Required | FK | Allowed values | Definition / rule |
|---|---|---:|---|---|---|
| `Scene_Prompt_ID` | STRING | Yes | — | — | Scene Prompt ID Must match ^SPV_[0-9]{6}$ and be unique. |
| `Scene_ID` | STRING | Yes | SCENES.Scene_ID | — | Scene ID |
| `Prompt_Version` | INTEGER | Yes | — | — | Prompt Version Positive integer; unique with Scene_ID. |
| `Generation_Profile_ID` | STRING | Yes | GENERATION_PROFILE_MASTER.Generation_Profile_ID | — | Generation Profile ID |
| `Compiled_Prompt_Text` | STRING | No | — | — | Compiled prompt; required from COMPILED onward. |
| `Compiled_Negative_Prompt` | STRING | No | — | — | Compiled Negative Prompt |
| `Compiler_Version` | STRING | No | — | — | Compiler Version |
| `Source_Context_SHA256` | STRING | No | — | — | Source Context SHA256 When populated, 64 hexadecimal characters. |
| `Prompt_SHA256` | STRING | No | — | — | Prompt SHA256 When populated, 64 hexadecimal characters. |
| `Prompt_Status` | STRING | Yes | — | DRAFT, COMPILED, VALIDATED, APPROVED, SUPERSEDED, REJECTED | Prompt Status |
| `Validation_Message` | STRING | No | — | — | Validation Message |
| `Approved_At` | TIMESTAMP | No | — | — | Required when APPROVED. |
| `Created_At` | TIMESTAMP | No | — | — | Created At |
| `Updated_At` | TIMESTAMP | No | — | — | Updated At |

## PRODUCTION_JOBS

Grain: one production run for exactly one Script Version.

Primary key: `Production_Job_ID`

Unique: Script_ID + Production_Run_No; Request_Idempotency_Key

| Field | Type | Required | FK | Allowed values | Definition / rule |
|---|---|---:|---|---|---|
| `Production_Job_ID` | STRING | Yes | — | — | Production Job ID Must match ^PRD_[0-9]{6}$ and be unique. |
| `Script_ID` | STRING | Yes | SCRIPT_MASTER.Script_ID | — | Script ID |
| `Production_Run_No` | INTEGER | Yes | — | — | Production Run No Positive integer; unique with Script_ID. |
| `Execution_Environment` | STRING | Yes | — | DEV, TEST, PROD | Execution Environment |
| `Request_Idempotency_Key` | STRING | Yes | — | — | Request Idempotency Key Globally unique. |
| `Queue_Priority` | INTEGER | No | — | — | Queue Priority |
| `Scene_Set_SHA256` | STRING | No | — | — | Scene Set SHA256 When populated, 64 hexadecimal characters. |
| `Job_Status` | STRING | Yes | — | REQUESTED, VALIDATING, READY, QUEUED, RUNNING, QUARANTINED, HOLD, FAILED, COMPLETED, CANCELLED | QUARANTINED is a terminal local stop for an exhausted ambiguous legacy attempt; it never proves provider failure or billing outcome. |
| `Hold_Reason_Code` | STRING | No | — | — | Required for HOLD. |
| `Error_Code` | STRING | No | — | — | Required for FAILED. |
| `Error_Message` | STRING | No | — | — | Failure details; required for FAILED. |
| `Requested_At` | TIMESTAMP | Yes | — | — | Requested At |
| `Started_At` | TIMESTAMP | No | — | — | Required for RUNNING, COMPLETED, or FAILED. |
| `Completed_At` | TIMESTAMP | No | — | — | Required for COMPLETED, FAILED, or CANCELLED. |
| `Created_At` | TIMESTAMP | No | — | — | Created At |
| `Updated_At` | TIMESTAMP | No | — | — | Updated At |

## SCENE_JOBS

Grain: one attempt for one Scene Prompt inside one Production Job.

Primary key: `Scene_Job_ID`

Unique: Production_Job_ID + Scene_Prompt_ID + Attempt_No

| Field | Type | Required | FK | Allowed values | Definition / rule |
|---|---|---:|---|---|---|
| `Scene_Job_ID` | STRING | Yes | — | — | Scene Job ID Must match ^SJB_[0-9]{6}$ and be unique. |
| `Production_Job_ID` | STRING | Yes | PRODUCTION_JOBS.Production_Job_ID | — | Production Job ID |
| `Scene_Prompt_ID` | STRING | Yes | SCENE_PROMPT_VERSION.Scene_Prompt_ID | — | Scene Prompt ID |
| `Attempt_No` | INTEGER | Yes | — | — | Attempt No Positive integer; unique per Production_Job_ID + Scene_Prompt_ID. |
| `Job_Status` | STRING | Yes | — | PENDING, VALIDATING, QUEUED, RUNNING, QUARANTINED, UPLOADING, REMOTE_VERIFYING, COMPLETED, HOLD, FAILED, CANCELLED | QUARANTINED is terminal and blocks automatic resume/retry; provider fate remains independent. |
| `Provider_Execution_ID` | STRING | No | — | — | Provider Execution ID |
| `Temporary_Workspace` | STRING | No | — | — | Runtime-owned temporary path only; never persistent storage. |
| `Remote_Output_URI` | URL | No | — | — | Persistent URI, not an ephemeral provider URL. |
| `Remote_File_ID` | STRING | No | — | — | Remote File ID |
| `Output_SHA256` | STRING | No | — | — | Output SHA256 When populated, 64 hexadecimal characters. |
| `Output_Size_Bytes` | INTEGER | No | — | — | Output Size Bytes Integer >= 0. |
| `Output_Duration_Sec` | DECIMAL | No | — | — | Output Duration Sec Positive when completed. |
| `Remote_Verified` | BOOLEAN | No | — | — | Remote Verified |
| `Local_Cleanup_Status` | STRING | No | — | NOT_STARTED, RETAINED, CLEANED, CLEANUP_FAILED | Local Cleanup Status |
| `Error_Code` | STRING | No | — | — | Error Code |
| `Error_Message` | STRING | No | — | — | Error Message |
| `Queued_At` | TIMESTAMP | No | — | — | Queued At |
| `Started_At` | TIMESTAMP | No | — | — | Started At |
| `Generation_Completed_At` | TIMESTAMP | No | — | — | Generation Completed At |
| `Remote_Verified_At` | TIMESTAMP | No | — | — | Remote Verified At |
| `Local_Deleted_At` | TIMESTAMP | No | — | — | Required when CLEANED. |
| `Completed_At` | TIMESTAMP | No | — | — | Completed At |
| `Created_At` | TIMESTAMP | No | — | — | Created At |
| `Updated_At` | TIMESTAMP | No | — | — | Updated At |

## TTS_JOBS

Grain: one TTS attempt for one Dialogue Line in one Production Job.

Primary key: `TTS_Job_ID`

Unique: Production_Job_ID + Dialogue_Line_ID + Attempt_No

| Field | Type | Required | FK | Allowed values | Definition / rule |
|---|---|---:|---|---|---|
| `TTS_Job_ID` | STRING | Yes | — | — | TTS Job ID Must match ^TTS_[0-9]{6}$ and be unique. |
| `Production_Job_ID` | STRING | Yes | PRODUCTION_JOBS.Production_Job_ID | — | Production Job ID |
| `Dialogue_Line_ID` | STRING | Yes | SCENE_DIALOGUE_LINES.Dialogue_Line_ID | — | Dialogue Line ID |
| `Attempt_No` | INTEGER | Yes | — | — | Attempt No Positive integer; unique per Production_Job_ID + Dialogue_Line_ID. |
| `Resolved_Voice_ID` | STRING | No | VOICE_MASTER.Voice_ID | — | Actual voice used; immutable execution snapshot. |
| `Provider_Account_ID` | STRING | No | PROVIDER_ACCOUNT_MASTER.Provider_Account_ID | — | Resolved account snapshot. |
| `Model_ID` | STRING | No | MODEL_MASTER.Model_ID | — | Resolved audio-capable model. |
| `Input_Text_SHA256` | STRING | No | — | — | Input Text SHA256 When populated, 64 hexadecimal characters. |
| `Synthesis_Config_SHA256` | STRING | No | — | — | SHA-256 of resolved voice, provider, model, locale, target duration, rate, pitch, and volume; prevents an attempt from being reused after synthesis configuration drift. |
| `Job_Status` | STRING | Yes | — | PENDING, VALIDATING, QUEUED, RUNNING, UPLOADING, REMOTE_VERIFYING, COMPLETED, HOLD, FAILED, CANCELLED | Job Status |
| `Provider_Execution_ID` | STRING | No | — | — | Provider Execution ID |
| `Remote_Audio_URI` | URL | No | — | — | Persistent URI, not an ephemeral provider URL. |
| `Remote_File_ID` | STRING | No | — | — | Remote File ID |
| `Audio_SHA256` | STRING | No | — | — | Audio SHA256 When populated, 64 hexadecimal characters. |
| `Audio_Size_Bytes` | INTEGER | No | — | — | Audio Size Bytes Integer >= 0. |
| `Audio_Duration_Ms` | INTEGER | No | — | — | Audio Duration Ms Positive when completed. |
| `Remote_Verified` | BOOLEAN | No | — | — | Remote Verified |
| `Local_Cleanup_Status` | STRING | No | — | NOT_STARTED, RETAINED, CLEANED, CLEANUP_FAILED | Local Cleanup Status |
| `Error_Code` | STRING | No | — | — | Error Code |
| `Error_Message` | STRING | No | — | — | Error Message |
| `Started_At` | TIMESTAMP | No | — | — | Started At |
| `Remote_Verified_At` | TIMESTAMP | No | — | — | Remote Verified At |
| `Local_Deleted_At` | TIMESTAMP | No | — | — | Required when CLEANED. |
| `Completed_At` | TIMESTAMP | No | — | — | Completed At |
| `Created_At` | TIMESTAMP | No | — | — | Created At |
| `Updated_At` | TIMESTAMP | No | — | — | Updated At |

## FINAL_OUTPUT

Grain: one assembled artifact version for one Production Job.

Primary key: `Final_Output_ID`

Unique: Production_Job_ID + Output_Type + Output_Version

| Field | Type | Required | FK | Allowed values | Definition / rule |
|---|---|---:|---|---|---|
| `Final_Output_ID` | STRING | Yes | — | — | Final Output ID Must match ^FOUT_[0-9]{6}$ and be unique. |
| `Production_Job_ID` | STRING | Yes | PRODUCTION_JOBS.Production_Job_ID | — | Production Job ID |
| `Output_Version` | INTEGER | Yes | — | — | Output Version Positive integer. |
| `Output_Type` | STRING | Yes | — | PREVIEW_VIDEO, FINAL_VIDEO, AUDIO_MASTER, SUBTITLE | Output Type |
| `Is_Primary` | BOOLEAN | No | — | — | Is Primary |
| `Assembly_Manifest_SHA256` | STRING | No | — | — | Assembly Manifest SHA256 When populated, 64 hexadecimal characters. |
| `Remote_Output_URI` | URL | No | — | — | Persistent URI, not an ephemeral provider URL. |
| `Remote_File_ID` | STRING | No | — | — | Remote File ID |
| `Output_SHA256` | STRING | No | — | — | Output SHA256 When populated, 64 hexadecimal characters. |
| `Output_Size_Bytes` | INTEGER | No | — | — | Output Size Bytes Integer >= 0. |
| `Duration_Sec` | DECIMAL | No | — | — | Duration Sec Positive when applicable. |
| `Width` | INTEGER | No | — | — | Width Positive when applicable. |
| `Height` | INTEGER | No | — | — | Height Positive when applicable. |
| `Aspect_Ratio` | STRING | No | — | — | Aspect Ratio |
| `Validation_Status` | STRING | Yes | — | PENDING, PASS, FAIL | Validation Status |
| `Validation_Message` | STRING | No | — | — | Validation Message |
| `Output_Status` | STRING | Yes | — | ASSEMBLING, GENERATED, VALIDATING, READY, REJECTED, ARCHIVED | Output Status |
| `Remote_Verified` | BOOLEAN | No | — | — | Remote Verified |
| `Created_At` | TIMESTAMP | No | — | — | Created At |
| `Updated_At` | TIMESTAMP | No | — | — | Updated At |

## Lifecycle, history, and completion rules

Production lifecycle: REQUESTED → VALIDATING → READY → QUEUED → RUNNING → COMPLETED. HOLD, FAILED, and CANCELLED remain explicit states. Scene/TTS lifecycle: PENDING → VALIDATING → QUEUED → RUNNING → UPLOADING → REMOTE_VERIFYING → COMPLETED. Retries append new rows; prompt edits create new versions; final outputs are never overwritten. Approved prompts require compiled text, Approved_At, and an ACTIVE Generation Profile; at most one prompt is APPROVED per Scene.

Completed Scene/TTS attempts require persistent remote media, verification, checksums/metadata and timestamps. Verification confirms remote existence and readable metadata, matching size when available, and matching checksum when available. Upload success alone is insufficient. CLEANED requires Remote_Verified=TRUE and Local_Deleted_At. PROD temporary workspace data cannot depend on Windows paths or Drive Desktop.

READY output requires PASS and Remote_Verified=TRUE. Production COMPLETED requires one active primary FINAL_VIDEO that is READY, PASS and remotely verified. TTS voice/account/model are execution snapshots; all three providers must match, and the model must be audio/TTS capable. Dialogue voice resolution is override then character default; narrator/system voice must be explicitly resolved.

## Deterministic hashes

Source_Context_SHA256 covers canonical UTF-8 JSON with stable key order and normalized strings for Scene, character/reference maps, prompt-relevant dialogue, resolved Character, Wardrobe, Location, Visual Style, Camera Profile, and Lighting data, Generation Profile, Prompt Template, and reference asset identities/versions. Prompt_SHA256 covers canonical compiled prompt representation. Scene_Set_SHA256 covers the ordered Scene_No list of Scene_ID + approved Scene_Prompt_ID. Input_Text_SHA256 hashes exact normalized dialogue sent to TTS. Assembly_Manifest_SHA256 covers ordered successful scene outputs, TTS outputs, applicable subtitle inputs, and assembly settings/config version; detailed manifest files belong in Drive Reports/Job Artifacts.

Phase E builds a SCENE_PROMPT_VERSION-compatible candidate in memory after
successful preflight. The candidate is VALIDATED, not APPROVED, and is never
written to the live sheet by the compiler. See
../architecture/PROMPT_BUILDER_ARCHITECTURE.md and
../architecture/PREFLIGHT_CONTRACT.md for the compile/readiness contract.

Job tables do not duplicate series/episode/scene/provider/model/prompt-template descriptions and contain no persistent local paths. Posting is separate; analytics is later. Exact dialogue text is represented by `Input_Text_SHA256`; the resolved synthesis settings are represented by `Synthesis_Config_SHA256`; secrets are never stored. A completed TTS row can be reused only when both hashes and its persistent URI/file ID, audio hash/size/duration, `Remote_Verified`, and `Local_Cleanup_Status=CLEANED` evidence are complete. NO_AUDIO uses no TTS row; it records a `FINAL_VIDEO` only after the scene has `Dialogue_Mode=NONE`, no linked dialogue, a full video decode with no audio stream, and Drive readback/hash verification. See [V2 Runtime Foundation](../architecture/V2_RUNTIME_FOUNDATION.md).

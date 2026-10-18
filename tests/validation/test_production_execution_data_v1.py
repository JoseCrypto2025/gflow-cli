from __future__ import annotations

from pathlib import Path

from scripts.validation.validate_v2_data_architecture import load_schemas, validate_rows

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "schemas"


def _seed() -> dict[str, list[dict[str, str]]]:
    return {
        "SCENES": [{"Scene_ID": "SCN_000001", "Script_ID": "SCR_000001"}],
        "SCRIPT_MASTER": [{"Script_ID": "SCR_000001"}],
        "GENERATION_PROFILE_MASTER": [{"Generation_Profile_ID": "GPR_0001", "Status": "ACTIVE"}],
        "PROVIDER_MASTER": [{"Provider_ID": "PRV_0001"}, {"Provider_ID": "PRV_0002"}],
        "PROVIDER_ACCOUNT_MASTER": [{"Provider_Account_ID": "PVA_0001", "Provider_ID": "PRV_0001"}],
        "MODEL_MASTER": [
            {"Model_ID": "MOD_0001", "Provider_ID": "PRV_0001", "Generation_Type": "TEXT_TO_SPEECH"}
        ],
        "VOICE_MASTER": [{"Voice_ID": "VOI_000001", "Provider_ID": "PRV_0001"}],
        "SCENE_DIALOGUE_LINES": [{"Dialogue_Line_ID": "SDL_000001", "Scene_ID": "SCN_000001"}],
        "SCENE_PROMPT_VERSION": [
            {
                "Scene_Prompt_ID": "SPV_000001",
                "Scene_ID": "SCN_000001",
                "Prompt_Version": "1",
                "Generation_Profile_ID": "GPR_0001",
                "Compiled_Prompt_Text": "compiled prompt",
                "Prompt_Status": "APPROVED",
                "Approved_At": "2026-10-16T00:00:00Z",
            }
        ],
        "PRODUCTION_JOBS": [
            {
                "Production_Job_ID": "PRD_000001",
                "Script_ID": "SCR_000001",
                "Production_Run_No": "1",
                "Execution_Environment": "TEST",
                "Request_Idempotency_Key": "req-1",
                "Job_Status": "RUNNING",
                "Requested_At": "2026-10-16T00:00:00Z",
                "Started_At": "2026-10-16T00:01:00Z",
            }
        ],
    }


def test_34_schemas_and_valid_empty_tables() -> None:
    schemas = load_schemas(SCHEMA_DIR)
    assert len(schemas) == 34
    assert validate_rows(SCHEMA_DIR, {name: [] for name in schemas}) == []


def test_approved_prompt_is_unique_and_hashes_are_checked() -> None:
    tables = _seed()
    tables["SCENE_PROMPT_VERSION"].append(
        {
            "Scene_Prompt_ID": "SPV_000002",
            "Scene_ID": "SCN_000001",
            "Prompt_Version": "2",
            "Generation_Profile_ID": "GPR_0001",
            "Compiled_Prompt_Text": "next",
            "Prompt_Status": "APPROVED",
            "Approved_At": "2026-10-16T00:00:00Z",
            "Prompt_SHA256": "bad",
        }
    )
    errors = validate_rows(SCHEMA_DIR, tables)
    assert any("maximum one APPROVED prompt" in e for e in errors)
    assert any("64 hex" in e for e in errors)


def test_test_and_prod_approvals_coexist_per_scene_and_profile() -> None:
    tables = _seed()
    tables["GENERATION_PROFILE_MASTER"].append(
        {"Generation_Profile_ID": "GPR_0002", "Status": "ACTIVE"}
    )
    tables["SCENE_PROMPT_VERSION"].append(
        {
            "Scene_Prompt_ID": "SPV_000002",
            "Scene_ID": "SCN_000001",
            "Prompt_Version": "1",
            "Generation_Profile_ID": "GPR_0002",
            "Compiled_Prompt_Text": "independent production prompt",
            "Prompt_Status": "APPROVED",
            "Approved_At": "2026-10-18T00:00:00Z",
        }
    )
    errors = validate_rows(SCHEMA_DIR, tables)
    assert not any("maximum one APPROVED prompt" in error for error in errors)

    tables["SCENE_PROMPT_VERSION"].append(
        {
            "Scene_Prompt_ID": "SPV_000003",
            "Scene_ID": "SCN_000001",
            "Prompt_Version": "2",
            "Generation_Profile_ID": "GPR_0002",
            "Compiled_Prompt_Text": "duplicate production approval",
            "Prompt_Status": "APPROVED",
            "Approved_At": "2026-10-18T00:01:00Z",
        }
    )
    errors = validate_rows(SCHEMA_DIR, tables)
    assert any("maximum one APPROVED prompt per Scene/profile" in error for error in errors)


def test_retry_and_verified_completion_cleanup_invariant() -> None:
    tables = _seed()
    tables["SCENE_JOBS"] = [
        {
            "Scene_Job_ID": "SJB_000001",
            "Production_Job_ID": "PRD_000001",
            "Scene_Prompt_ID": "SPV_000001",
            "Attempt_No": "1",
            "Job_Status": "FAILED",
            "Error_Code": "TEMP",
        },
        {
            "Scene_Job_ID": "SJB_000002",
            "Production_Job_ID": "PRD_000001",
            "Scene_Prompt_ID": "SPV_000001",
            "Attempt_No": "2",
            "Job_Status": "COMPLETED",
            "Remote_Output_URI": "https://drive.google.com/file/d/1",
            "Output_SHA256": "a" * 64,
            "Output_Size_Bytes": "100",
            "Output_Duration_Sec": "4",
            "Remote_Verified": "TRUE",
            "Local_Cleanup_Status": "CLEANED",
            "Generation_Completed_At": "2026-10-16T00:02:00Z",
            "Remote_Verified_At": "2026-10-16T00:03:00Z",
            "Completed_At": "2026-10-16T00:03:00Z",
            "Local_Deleted_At": "2026-10-16T00:04:00Z",
        },
    ]
    errors = validate_rows(SCHEMA_DIR, tables)
    assert not any(
        "SCENE_JOBS row 3: COMPLETED" in e or "SCENE_JOBS row 3: cleanup" in e for e in errors
    )
    tables["SCENE_JOBS"][1]["Remote_Verified"] = "FALSE"
    errors = validate_rows(SCHEMA_DIR, tables)
    assert any("cleanup prohibited" in e for e in errors)
    assert any("COMPLETED requires Remote_Verified" in e for e in errors)


def test_tts_provider_model_voice_mismatch_is_rejected() -> None:
    tables = _seed()
    tables["PROVIDER_ACCOUNT_MASTER"].append(
        {"Provider_Account_ID": "PVA_0002", "Provider_ID": "PRV_0002"}
    )
    tables["MODEL_MASTER"].append(
        {"Model_ID": "MOD_0002", "Provider_ID": "PRV_0002", "Generation_Type": "TEXT_TO_SPEECH"}
    )
    tables["TTS_JOBS"] = [
        {
            "TTS_Job_ID": "TTS_000001",
            "Production_Job_ID": "PRD_000001",
            "Dialogue_Line_ID": "SDL_000001",
            "Attempt_No": "1",
            "Resolved_Voice_ID": "VOI_000001",
            "Provider_Account_ID": "PVA_0002",
            "Model_ID": "MOD_0002",
            "Job_Status": "QUEUED",
        }
    ]
    errors = validate_rows(SCHEMA_DIR, tables)
    assert any("providers must match" in e for e in errors)
    tables["TTS_JOBS"][0]["Resolved_Voice_ID"] = ""
    errors = validate_rows(SCHEMA_DIR, tables)
    assert any("Resolved_Voice_ID required before execution" in e for e in errors)


def test_final_output_completion_gate_and_primary_uniqueness() -> None:
    tables = _seed()
    job = tables["PRODUCTION_JOBS"][0]
    job.update({"Job_Status": "COMPLETED", "Completed_At": "2026-10-16T00:05:00Z"})
    errors = validate_rows(SCHEMA_DIR, tables)
    assert any("COMPLETED requires a verified primary FINAL_VIDEO" in e for e in errors)
    out = {
        "Final_Output_ID": "FOUT_000001",
        "Production_Job_ID": "PRD_000001",
        "Output_Version": "1",
        "Output_Type": "FINAL_VIDEO",
        "Is_Primary": "TRUE",
        "Assembly_Manifest_SHA256": "b" * 64,
        "Remote_Output_URI": "https://drive.google.com/file/d/2",
        "Remote_File_ID": "2",
        "Output_SHA256": "c" * 64,
        "Output_Size_Bytes": "500",
        "Duration_Sec": "10",
        "Width": "1080",
        "Height": "1920",
        "Validation_Status": "PASS",
        "Output_Status": "READY",
        "Remote_Verified": "TRUE",
    }
    tables["FINAL_OUTPUT"] = [out]
    errors = validate_rows(SCHEMA_DIR, tables)
    assert not any(
        "FINAL_OUTPUT row 2: READY" in e or "COMPLETED requires a verified primary FINAL_VIDEO" in e
        for e in errors
    )
    tables["FINAL_OUTPUT"].append(dict(out, Final_Output_ID="FOUT_000002", Output_Version="2"))
    assert any(
        "duplicate active primary FINAL_VIDEO" in e for e in validate_rows(SCHEMA_DIR, tables)
    )


def test_request_key_and_positive_retry_number() -> None:
    tables = _seed()
    tables["PRODUCTION_JOBS"].append(
        dict(tables["PRODUCTION_JOBS"][0], Production_Job_ID="PRD_000002", Production_Run_No="2")
    )
    tables["SCENE_JOBS"] = [
        {
            "Scene_Job_ID": "SJB_000001",
            "Production_Job_ID": "PRD_000001",
            "Scene_Prompt_ID": "SPV_000001",
            "Attempt_No": "0",
            "Job_Status": "PENDING",
        }
    ]
    errors = validate_rows(SCHEMA_DIR, tables)
    assert any("unique key ['Request_Idempotency_Key']" in e for e in errors)
    assert any("Attempt_No must be positive" in e for e in errors)


def test_tts_success_records_resolved_voice_and_verified_persistent_audio() -> None:
    tables = _seed()
    tables["TTS_JOBS"] = [
        {
            "TTS_Job_ID": "TTS_000001",
            "Production_Job_ID": "PRD_000001",
            "Dialogue_Line_ID": "SDL_000001",
            "Attempt_No": "1",
            "Resolved_Voice_ID": "VOI_000001",
            "Provider_Account_ID": "PVA_0001",
            "Model_ID": "MOD_0001",
            "Input_Text_SHA256": "d" * 64,
            "Job_Status": "COMPLETED",
            "Remote_Audio_URI": "https://drive.google.com/file/d/audio",
            "Remote_File_ID": "audio",
            "Audio_SHA256": "e" * 64,
            "Audio_Size_Bytes": "20",
            "Audio_Duration_Ms": "1000",
            "Remote_Verified": "TRUE",
            "Remote_Verified_At": "2026-10-16T00:03:00Z",
            "Completed_At": "2026-10-16T00:04:00Z",
        }
    ]
    errors = validate_rows(SCHEMA_DIR, tables)
    assert not any("TTS_JOBS row 2:" in error for error in errors)


def test_prod_temporary_workspace_rejects_local_drive_paths() -> None:
    tables = _seed()
    tables["PRODUCTION_JOBS"][0]["Execution_Environment"] = "PROD"
    tables["SCENE_JOBS"] = [
        {
            "Scene_Job_ID": "SJB_000001",
            "Production_Job_ID": "PRD_000001",
            "Scene_Prompt_ID": "SPV_000001",
            "Attempt_No": "1",
            "Job_Status": "PENDING",
            "Temporary_Workspace": r"D:\gflow\temp\job",
        }
    ]
    errors = validate_rows(SCHEMA_DIR, tables)
    assert any("PROD workspace must not use local paths" in error for error in errors)


def test_failed_production_job_requires_error_message() -> None:
    tables = _seed()
    tables["PRODUCTION_JOBS"][0].update(
        {
            "Job_Status": "FAILED",
            "Completed_At": "2026-10-16T00:05:00Z",
            "Error_Code": "E_RUNTIME",
        }
    )
    errors = validate_rows(SCHEMA_DIR, tables)
    assert any("Error_Message required for FAILED" in error for error in errors)

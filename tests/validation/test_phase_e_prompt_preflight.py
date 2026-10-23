from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from gflow_cli.v2_runtime.errors import FlowPresubmitError
from gflow_cli.v2_runtime.google_flow import build_agent_only_directive
from gflow_cli.v2_runtime.phase_e import (
    InMemorySceneCatalog,
    build_scene_prompt_payload,
    preflight_scene,
)


def _fixtures() -> dict[str, list[dict[str, object]]]:
    caps = [
        ("GENERATION_MODE", '{"mode":"TEXT_TO_VIDEO","supported":true}'),
        ("DURATION_SEC", "[4,6,8]"),
        ("ASPECT_RATIO", "16:9"),
        ("RESOLUTION", "720p"),
        ("OUTPUT_COUNT", "1"),
        ("AUDIO_MODE", "NONE"),
        ("QUALITY_TIER", "STANDARD"),
    ]
    return {
        "SCENES": [
            {
                "Scene_ID": "SCN_000001",
                "Script_ID": "SCR_000001",
                "Scene_No": 1,
                "Scene_Title": "Opening",
                "Scene_Type": "GENERATED_VIDEO",
                "Scene_Summary": "A person enters.",
                "Action_Description": "A person enters the room.",
                "Location_ID": "LOC_0001",
                "Duration_Target_Sec": 8,
                "Aspect_Ratio_Override": "16:9",
                "Scene_Definition_Status": "LOCKED",
                "Created_At": "2026-01-01T00:00:00Z",
            }
        ],
        "SCRIPT_MASTER": [
            {
                "Script_ID": "SCR_000001",
                "Episode_ID": "EPI_000001",
                "Script_Status": "APPROVED",
                "Dialogue_Language": "en",
            }
        ],
        "EPISODE_MASTER": [
            {"Episode_ID": "EPI_000001", "Series_ID": "SER_0001", "Primary_Language": "th"}
        ],
        "SERIES_MASTER": [
            {"Series_ID": "SER_0001", "Default_Aspect_Ratio": "16:9", "Primary_Language": "ja"}
        ],
        "SCENE_CHARACTER_MAP": [
            {
                "Scene_Character_ID": "SCH_000001",
                "Scene_ID": "SCN_000001",
                "Character_ID": "CHR_0001",
                "Sort_Order": 1,
                "Status": "ACTIVE",
            }
        ],
        "CHARACTER_MASTER": [
            {
                "Character_ID": "CHR_0001",
                "Character_Name": "Mali",
                "Prompt_Identity_Text": "A woman with short dark hair.",
                "Status": "ACTIVE",
            }
        ],
        "LOCATION_MASTER": [
            {
                "Location_ID": "LOC_0001",
                "Prompt_Location_Text": "A quiet wooden room.",
                "Status": "ACTIVE",
            }
        ],
        "GENERATION_PROFILE_MASTER": [
            {
                "Generation_Profile_ID": "GPR_0001",
                "Provider_Account_ID": "PVA_0001",
                "Model_ID": "MDL_0001",
                "Generation_Mode": "TEXT_TO_VIDEO",
                "Environment": "DEV",
                "Purpose": "test",
                "Status": "ACTIVE",
                "Prompt_Template_ID": "PMT_0001",
                "Default_Aspect_Ratio": "16:9",
                "Default_Duration_Sec": None,
                "Default_Resolution": "720p",
                "Default_Output_Count": 1,
                "Audio_Mode": "NONE",
                "Quality_Tier": "STANDARD",
            }
        ],
        "PROVIDER_ACCOUNT_MASTER": [
            {
                "Provider_Account_ID": "PVA_0001",
                "Provider_ID": "PRV_0001",
                "Environment": "DEV",
                "Auth_Profile_Key": "GFLOW_DEV_AUTH",
                "Status": "ACTIVE",
            }
        ],
        "PROVIDER_MASTER": [
            {"Provider_ID": "PRV_0001", "Provider_Type": "VIDEO_GENERATION", "Status": "ACTIVE"}
        ],
        "MODEL_MASTER": [
            {
                "Model_ID": "MDL_0001",
                "Provider_ID": "PRV_0001",
                "Model_Version": "1",
                "Status": "ACTIVE",
                "Default_Resolution": "720p",
            }
        ],
        "MODEL_CAPABILITY_MAP": [
            {
                "Model_Capability_ID": f"CAP_{i:06d}",
                "Model_ID": "MDL_0001",
                "Capability_Type": kind,
                "Capability_Value": value,
                "Status": "ACTIVE",
            }
            for i, (kind, value) in enumerate(caps, 1)
        ],
        "MODEL_PARAMETER_MAP": [
            {
                "Model_Parameter_ID": "MPM_000001",
                "Model_ID": "MDL_0001",
                "Generation_Mode": "TEXT_TO_VIDEO",
                "Canonical_Parameter": "ASPECT_RATIO",
                "Adapter_Parameter_Key": "google_flow.agent.video.aspect_ratio",
                "Parameter_Data_Type": "STRING",
                "Allowed_Values_JSON": '["16:9","9:16"]',
                "Default_Value_JSON": "",
                "Is_Required": True,
                "Status": "ACTIVE",
            },
            {
                "Model_Parameter_ID": "MPM_000002",
                "Model_ID": "MDL_0001",
                "Generation_Mode": "TEXT_TO_VIDEO",
                "Canonical_Parameter": "OUTPUT_COUNT",
                "Adapter_Parameter_Key": "google_flow.agent.video.output_count",
                "Parameter_Data_Type": "INTEGER",
                "Allowed_Values_JSON": "[1,2,3,4]",
                "Default_Value_JSON": "",
                "Is_Required": True,
                "Status": "ACTIVE",
            },
            {
                "Model_Parameter_ID": "MPM_000003",
                "Model_ID": "MDL_0001",
                "Generation_Mode": "TEXT_TO_VIDEO",
                "Canonical_Parameter": "DURATION_SEC",
                "Adapter_Parameter_Key": "google_flow.agent.video.duration_instruction",
                "Parameter_Data_Type": "NUMBER",
                "Allowed_Values_JSON": "[4,6,8]",
                "Default_Value_JSON": "",
                "Is_Required": True,
                "Status": "ACTIVE",
            },
        ],
        "PROMPT_TEMPLATE_MASTER": [
            {
                "Prompt_Template_ID": "PMT_0001",
                "Provider_ID": "PRV_0001",
                "Model_ID": "MDL_0001",
                "Generation_Mode": "TEXT_TO_VIDEO",
                "Template_Version": "1",
                "System_Prompt": "neutral system context",
                "Prompt_Template": "{SUBJECT}\n{ACTION}\n{ENVIRONMENT}",
                "Negative_Template": "{NEGATIVE_CONSTRAINTS}",
                "Status": "ACTIVE",
            }
        ],
    }


def _compile(tables: dict[str, list[dict[str, object]]] | None = None):
    return preflight_scene(
        InMemorySceneCatalog(_fixtures() if tables is None else tables),
        "SCN_000001",
        "GPR_0001",
        "development",
    )


def test_preflight_pass_is_deterministic_and_provider_neutral() -> None:
    result = _compile()
    assert result.status == "PREFLIGHT_PASS", (
        result.errors,
        result.compiled_prompt,
        result.compiled_negative_prompt,
    )
    assert result.ready_for_provider_adapter
    assert result.resolved["provider_account_id"] == "PVA_0001"
    assert "A woman with short dark hair." in (result.compiled_prompt or "")
    assert result.source_context_sha256 and len(result.source_context_sha256) == 64
    assert result.prompt_sha256 and len(result.prompt_sha256) == 64


@pytest.mark.parametrize("duration", [4, 6, 8])
def test_phase_e_duration_is_structured_and_drives_distinct_agent_directives(duration: int) -> None:
    tables = _fixtures()
    tables["SCENES"][0]["Duration_Target_Sec"] = duration
    result = _compile(tables)

    assert result.status == "PREFLIGHT_PASS", result.errors
    assert result.technical_configuration["duration_sec"] == duration
    assert result.technical_configuration["canonical_parameters"]["DURATION_SEC"] == duration
    assert (
        result.technical_configuration["adapter_parameters"][
            "google_flow.agent.video.duration_instruction"
        ]
        == duration
    )
    directive = build_agent_only_directive(
        result.compiled_prompt or "scene prompt", result.technical_configuration
    )
    assert directive == (
        f"Make me a {duration} second video of {result.compiled_prompt} "
        f"in a {result.technical_configuration['aspect_ratio']} aspect ratio."
    )


def test_different_structured_durations_produce_different_agent_directives() -> None:
    directives = []
    for duration in (4, 6, 8):
        tables = _fixtures()
        tables["SCENES"][0]["Duration_Target_Sec"] = duration
        result = _compile(tables)
        directives.append(
            build_agent_only_directive(
                result.compiled_prompt or "scene prompt", result.technical_configuration
            )
        )
    assert len(set(directives)) == 3


def test_absent_scene_and_profile_duration_holds_without_inventing_a_default() -> None:
    tables = _fixtures()
    tables["SCENES"][0]["Duration_Target_Sec"] = None
    tables["GENERATION_PROFILE_MASTER"][0]["Default_Duration_Sec"] = None
    result = _compile(tables)

    assert result.status == "PREFLIGHT_HOLD"
    assert result.technical_configuration["duration_sec"] is None
    assert "REQUIRED_PARAMETER_MAPPING_MISSING" in {issue.code for issue in result.errors}
    with pytest.raises(FlowPresubmitError) as exc:
        build_agent_only_directive("scene prompt", result.technical_configuration)
    assert exc.value.code == "FLOW_AGENT_DIRECTIVE_UNRESOLVED"


def test_profile_duration_is_only_a_fallback_after_scene_duration() -> None:
    tables = _fixtures()
    tables["SCENES"][0]["Duration_Target_Sec"] = None
    tables["GENERATION_PROFILE_MASTER"][0]["Default_Duration_Sec"] = 6
    result = _compile(tables)

    assert result.status == "PREFLIGHT_PASS", result.errors
    assert result.technical_configuration["duration_sec"] == 6


def test_generation_profile_aspect_ratio_is_fallback_after_scene_and_series() -> None:
    tables = _fixtures()
    tables["SCENES"][0]["Aspect_Ratio_Override"] = ""
    tables["SERIES_MASTER"][0]["Default_Aspect_Ratio"] = ""
    tables["MODEL_CAPABILITY_MAP"] = [
        row for row in tables["MODEL_CAPABILITY_MAP"] if row["Capability_Type"] != "ASPECT_RATIO"
    ]
    result = _compile(tables)

    assert result.status == "PREFLIGHT_PASS", result.errors
    assert result.technical_configuration["aspect_ratio"] == "16:9"


def test_external_tts_is_not_mapped_to_google_flow_native_audio() -> None:
    tables = _fixtures()
    tables["GENERATION_PROFILE_MASTER"][0]["Audio_Mode"] = "EXTERNAL_TTS"
    result = _compile(tables)

    assert result.status == "PREFLIGHT_PASS", result.errors
    assert result.technical_configuration["audio_mode"] == "EXTERNAL_TTS"
    assert "AUDIO_ENABLED" not in result.technical_configuration["canonical_parameters"]


def test_profile_missing_and_inactive_hold_with_machine_codes() -> None:
    tables = _fixtures()
    tables["GENERATION_PROFILE_MASTER"] = []
    assert "PROFILE_NOT_FOUND" in {x.code for x in _compile(tables).errors}
    tables = _fixtures()
    tables["GENERATION_PROFILE_MASTER"][0]["Status"] = "HOLD"
    assert "PROFILE_INACTIVE" in {x.code for x in _compile(tables).errors}


def test_missing_scene_and_explicit_profile_input_hold() -> None:
    result = preflight_scene(InMemorySceneCatalog(_fixtures()), "SCN_999999", "GPR_0001", "DEV")
    assert "SCENE_NOT_FOUND" in {x.code for x in result.errors}
    result = preflight_scene(InMemorySceneCatalog(_fixtures()), "SCN_000001", "", "DEV")
    assert "PROFILE_NOT_FOUND" in {x.code for x in result.errors}


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("Provider_ID", "PRV_9999", "TEMPLATE_PROVIDER_MISMATCH"),
        ("Model_ID", "MDL_9999", "TEMPLATE_MODEL_MISMATCH"),
        ("Generation_Mode", "FIRST_FRAME_TO_VIDEO", "TEMPLATE_GENERATION_MODE_MISMATCH"),
    ],
)
def test_prompt_template_compatibility_fails_closed(field: str, value: str, expected: str) -> None:
    tables = _fixtures()
    tables["PROMPT_TEMPLATE_MASTER"][0][field] = value
    assert expected in {x.code for x in _compile(tables).errors}


def test_provider_account_model_provider_and_environment_mismatches_hold() -> None:
    tables = _fixtures()
    tables["PROVIDER_ACCOUNT_MASTER"] = []
    assert "PROVIDER_ACCOUNT_NOT_FOUND" in {x.code for x in _compile(tables).errors}
    tables = _fixtures()
    tables["MODEL_MASTER"][0]["Provider_ID"] = "PRV_9999"
    assert "PROVIDER_MISMATCH" in {x.code for x in _compile(tables).errors}
    tables = _fixtures()
    tables["GENERATION_PROFILE_MASTER"][0]["Environment"] = "PROD"
    assert "PROFILE_ENVIRONMENT_MISMATCH" in {x.code for x in _compile(tables).errors}


@pytest.mark.parametrize(
    ("scene_type", "expected"),
    [("STATIC_IMAGE", "SCENE_TYPE_NOT_GENERATABLE"), ("GENERATED_VIDEO", "SCENE_NOT_LOCKED")],
)
def test_scene_type_and_lock_gate(scene_type: str, expected: str) -> None:
    tables = _fixtures()
    tables["SCENES"][0]["Scene_Type"] = scene_type
    tables["SCENES"][0]["Scene_Definition_Status"] = "DRAFT"
    assert expected in {x.code for x in _compile(tables).errors}


def test_script_must_be_approved() -> None:
    tables = _fixtures()
    tables["SCRIPT_MASTER"][0]["Script_Status"] = "REVIEW"
    assert "SCRIPT_NOT_APPROVED" in {x.code for x in _compile(tables).errors}


def test_unsupported_duration_and_aspect_ratio_are_not_clamped_or_substituted() -> None:
    tables = _fixtures()
    tables["SCENES"][0]["Duration_Target_Sec"] = 30
    tables["SCENES"][0]["Aspect_Ratio_Override"] = "3:2"
    codes = {x.code for x in _compile(tables).errors}
    assert "DURATION_UNSUPPORTED" in codes
    assert "ASPECT_RATIO_UNSUPPORTED" in codes


def test_live_agent_parameter_map_resolves_prose_capability_values() -> None:
    tables = _fixtures()
    tables["MODEL_CAPABILITY_MAP"] = [
        row for row in tables["MODEL_CAPABILITY_MAP"] if row["Capability_Type"] != "ASPECT_RATIO"
    ]
    tables["MODEL_CAPABILITY_MAP"].append(
        {
            "Model_Capability_ID": "CAP_000099",
            "Model_ID": "MDL_0001",
            "Capability_Type": "GENERATION_MODE",
            "Capability_Value": (
                '{"mode":"TEXT_TO_VIDEO","supported":true,'
                '"aspect_ratios":"Both (official source wording; values not enumerated)"}'
            ),
            "Status": "ACTIVE",
        }
    )
    result = _compile(tables)
    assert result.status == "PREFLIGHT_PASS", result.errors
    assert "CAPABILITY_NOT_MACHINE_ENUMERABLE" not in {x.code for x in result.warnings}


def test_first_frame_and_source_video_contracts_fail_closed() -> None:
    tables = _fixtures()
    tables["GENERATION_PROFILE_MASTER"][0]["Generation_Mode"] = "FIRST_FRAME_TO_VIDEO"
    tables["PROMPT_TEMPLATE_MASTER"][0]["Generation_Mode"] = "FIRST_FRAME_TO_VIDEO"
    tables["MODEL_CAPABILITY_MAP"].append(
        {
            "Model_Capability_ID": "CAP_000098",
            "Model_ID": "MDL_0001",
            "Capability_Type": "GENERATION_MODE",
            "Capability_Value": '{"mode":"FIRST_FRAME_TO_VIDEO","supported":true}',
            "Status": "ACTIVE",
        }
    )
    first_frame = _compile(tables)
    assert "START_FRAME_REQUIRED" in {x.code for x in first_frame.errors}, first_frame.errors
    tables = _fixtures()
    tables["GENERATION_PROFILE_MASTER"][0]["Generation_Mode"] = "VIDEO_TO_VIDEO"
    tables["PROMPT_TEMPLATE_MASTER"][0]["Generation_Mode"] = "VIDEO_TO_VIDEO"
    tables["MODEL_CAPABILITY_MAP"].append(
        {
            "Model_Capability_ID": "CAP_000097",
            "Model_ID": "MDL_0001",
            "Capability_Type": "GENERATION_MODE",
            "Capability_Value": '{"mode":"VIDEO_TO_VIDEO","supported":true}',
            "Status": "ACTIVE",
        }
    )
    assert "SOURCE_VIDEO_INPUT_CONTRACT_MISSING" in {x.code for x in _compile(tables).errors}


def test_unknown_and_unresolved_placeholders_hold() -> None:
    tables = _fixtures()
    tables["PROMPT_TEMPLATE_MASTER"][0]["Prompt_Template"] = "{SUBJECT} {FLOW_SELECTOR}"
    assert "PROMPT_TEMPLATE_UNKNOWN_PLACEHOLDER" in {x.code for x in _compile(tables).errors}
    tables = _fixtures()
    tables["PROMPT_TEMPLATE_MASTER"][0]["Prompt_Template"] = "{CAMERA}"
    assert "PROMPT_PLACEHOLDER_UNRESOLVED" in {x.code for x in _compile(tables).errors}


def test_required_parameter_map_json_type_and_allowed_value_validation() -> None:
    tables = _fixtures()
    tables["MODEL_PARAMETER_MAP"] = [
        {
            "Model_Parameter_ID": "MPM_000001",
            "Model_ID": "MDL_0001",
            "Generation_Mode": "TEXT_TO_VIDEO",
            "Canonical_Parameter": "OUTPUT_COUNT",
            "Adapter_Parameter_Key": "output.count",
            "Parameter_Data_Type": "STRING",
            "Allowed_Values_JSON": '["two"]',
            "Is_Required": True,
            "Status": "ACTIVE",
        }
    ]
    errors = {x.code for x in _compile(tables).errors}
    assert "PARAMETER_VALUE_INVALID" in errors


def test_reference_fallback_is_validated_and_stable() -> None:
    tables = _fixtures()
    tables["REFERENCE_ASSET_MASTER"] = [
        {
            "Reference_Asset_ID": "REF_000001",
            "Asset_Name": "Mali reference",
            "Drive_File_ID": "drive-file-1",
            "Version": "2",
            "Checksum": "a" * 64,
            "Reference_Quality_Status": "PASS",
            "Status": "ACTIVE",
        }
    ]
    tables["REFERENCE_ENTITY_MAP"] = [
        {
            "Reference_Entity_Map_ID": "REM_000001",
            "Reference_Asset_ID": "REF_000001",
            "Entity_Type": "CHARACTER",
            "Entity_ID": "CHR_0001",
            "Reference_Role": "CHARACTER",
            "Is_Primary": True,
            "Priority": 1,
            "Usage_Instruction_Default": "Keep facial identity consistent.",
            "Status": "ACTIVE",
        }
    ]
    one = _compile(tables)
    shuffled = deepcopy(tables)
    shuffled["REFERENCE_ENTITY_MAP"].reverse()
    two = _compile(shuffled)
    assert one.status == "PREFLIGHT_PASS" and one.references[0].drive_file_id == "drive-file-1"
    assert one.source_context_sha256 == two.source_context_sha256
    tables["REFERENCE_ASSET_MASTER"][0]["Reference_Quality_Status"] = "PENDING"
    assert "REFERENCE_QUALITY_NOT_PASS" in {x.code for x in _compile(tables).errors}


def test_continuity_self_reference_and_duplicate_reference_ambiguity_hold() -> None:
    tables = _fixtures()
    tables["SCENES"][0]["Continuity_Source_Scene_ID"] = "SCN_000001"
    assert "CONTINUITY_CYCLE_DETECTED" in {x.code for x in _compile(tables).errors}
    tables = _fixtures()
    tables["REFERENCE_ASSET_MASTER"] = [
        {
            "Reference_Asset_ID": f"REF_{i:06d}",
            "Drive_File_ID": f"drive-{i}",
            "Reference_Quality_Status": "PASS",
            "Status": "ACTIVE",
        }
        for i in (1, 2)
    ]
    tables["REFERENCE_ENTITY_MAP"] = [
        {
            "Reference_Entity_Map_ID": f"REM_{i:06d}",
            "Reference_Asset_ID": f"REF_{i:06d}",
            "Entity_Type": "CHARACTER",
            "Entity_ID": "CHR_0001",
            "Reference_Role": "CHARACTER",
            "Is_Primary": True,
            "Priority": 1,
            "Status": "ACTIVE",
        }
        for i in (1, 2)
    ]
    assert "REFERENCE_SELECTION_AMBIGUOUS" in {x.code for x in _compile(tables).errors}


def test_continuity_cycle_is_detected_without_recursive_prompt_context() -> None:
    tables = _fixtures()
    tables["SCENES"].extend(
        [
            {
                "Scene_ID": "SCN_000002",
                "Continuity_Source_Scene_ID": "SCN_000003",
                "Scene_End_State": "second state",
            },
            {
                "Scene_ID": "SCN_000003",
                "Continuity_Source_Scene_ID": "SCN_000001",
                "Scene_End_State": "third state",
            },
        ]
    )
    tables["SCENES"][0]["Continuity_Source_Scene_ID"] = "SCN_000002"
    assert "CONTINUITY_CYCLE_DETECTED" in {x.code for x in _compile(tables).errors}


def test_dialogue_context_is_ordered_hashed_but_not_unconditionally_injected() -> None:
    tables = _fixtures()
    tables["SCENE_DIALOGUE_LINES"] = [
        {
            "Dialogue_Line_ID": "DLG_000002",
            "Scene_ID": "SCN_000001",
            "Line_No": 2,
            "Dialogue_Text": "Second line.",
            "Status": "ACTIVE",
        },
        {
            "Dialogue_Line_ID": "DLG_000001",
            "Scene_ID": "SCN_000001",
            "Line_No": 1,
            "Dialogue_Text": "First line.",
            "Status": "ACTIVE",
        },
    ]
    result = _compile(tables)
    reordered = deepcopy(tables)
    reordered["SCENE_DIALOGUE_LINES"].reverse()
    assert result.source_context_sha256 == _compile(reordered).source_context_sha256
    assert "First line." not in (result.compiled_prompt or "")
    changed = deepcopy(tables)
    changed["SCENE_DIALOGUE_LINES"][1]["Dialogue_Text"] = "Changed line."
    assert result.source_context_sha256 != _compile(changed).source_context_sha256


def test_hashes_ignore_row_order_and_audit_timestamps_but_change_for_semantics() -> None:
    tables = _fixtures()
    base = _compile(tables)
    shuffled = deepcopy(tables)
    shuffled["MODEL_CAPABILITY_MAP"].reverse()
    shuffled["SCENE_CHARACTER_MAP"].reverse()
    assert base.source_context_sha256 == _compile(shuffled).source_context_sha256
    audit_only = deepcopy(tables)
    audit_only["SCENES"][0]["Updated_At"] = "2099-01-01T00:00:00Z"
    assert base.source_context_sha256 == _compile(audit_only).source_context_sha256
    semantic = deepcopy(tables)
    semantic["CHARACTER_MASTER"][0]["Prompt_Identity_Text"] = "A woman with long dark hair."
    assert base.source_context_sha256 != _compile(semantic).source_context_sha256


def test_prompt_payload_is_validated_not_approved_and_requires_explicit_identity() -> None:
    result = _compile()
    payload = build_scene_prompt_payload(result, "SPV_000001", 1)
    assert payload["Prompt_Status"] == "VALIDATED"
    assert "Approved_At" not in payload
    assert payload["Scene_ID"] == "SCN_000001"
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "schemas"
        / "transaction"
        / "SCENE_PROMPT_VERSION.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    headers = {field["field_name"] for field in schema["fields"]}
    required = {field["field_name"] for field in schema["fields"] if field["required"]}
    assert set(payload) <= headers
    assert required <= set(payload)
    with pytest.raises(ValueError):
        build_scene_prompt_payload(result, "SPV_000001", 0)
    with pytest.raises(ValueError):
        build_scene_prompt_payload(_compile({}), "SPV_000002", 1)


def test_source_context_does_not_expose_auth_profile_reference() -> None:
    serialized = str(_compile().as_dict())
    assert "GFLOW_DEV_AUTH" not in serialized
    assert "token" not in serialized.lower()

from __future__ import annotations

from pathlib import Path

from scripts.validation.validate_v2_data_architecture import load_schemas, validate_rows

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "schemas"


def _episode(episode_id: str, episode_no: str = "1") -> dict[str, str]:
    return {
        "Episode_ID": episode_id,
        "Series_ID": "SER_0001",
        "Episode_No": episode_no,
        "Episode_Title": f"Episode {episode_no}",
        "Episode_Status": "DRAFT",
    }


def test_schemas_cover_the_six_phase_b_tables_and_activate_scene_reference_fk() -> None:
    schemas = load_schemas(SCHEMA_DIR)
    expected = {
        "EPISODE_MASTER",
        "SCRIPT_MASTER",
        "SCENES",
        "SCENE_CHARACTER_MAP",
        "SCENE_DIALOGUE_LINES",
        "SCENE_REFERENCE_MAP",
    }
    assert expected <= schemas.keys()
    assert schemas["SCENE_REFERENCE_MAP"]["table_type"] == "relation"
    scene_id = next(
        field
        for field in schemas["SCENE_REFERENCE_MAP"]["fields"]
        if field["field_name"] == "Scene_ID"
    )
    assert scene_id["required"] is True
    assert scene_id["foreign_key"] == "SCENES.Scene_ID"
    assert scene_id["foreign_key_status"] == "ACTIVE FK"


def test_episode_series_fk_and_composite_unique_key_are_enforced() -> None:
    tables = {
        "SERIES_MASTER": [{"Series_ID": "SER_0001"}],
        "EPISODE_MASTER": [_episode("EPI_000001"), _episode("EPI_000002")],
    }
    errors = validate_rows(SCHEMA_DIR, tables)
    assert any("unique" in error.lower() and "Series_ID" in error for error in errors)


def test_orphan_episode_series_fk_is_rejected() -> None:
    tables = {"SERIES_MASTER": [], "EPISODE_MASTER": [_episode("EPI_000001")]}
    errors = validate_rows(SCHEMA_DIR, tables)
    assert any("SERIES_MASTER.Series_ID" in error for error in errors)


def test_duplicate_ids_and_non_positive_episode_number_are_rejected() -> None:
    rows = [_episode("EPI_000001", "0"), _episode("EPI_000001", "2")]
    errors = validate_rows(SCHEMA_DIR, {"EPISODE_MASTER": rows})
    assert any("duplicate primary key" in error for error in errors)
    assert any("Episode_No must be positive" in error for error in errors)


def test_dialogue_character_fk_is_conditional() -> None:
    tables = {
        "SCENES": [{"Scene_ID": "SCN_000001"}],
        "SCENE_DIALOGUE_LINES": [
            {
                "Dialogue_Line_ID": "SDL_000001",
                "Scene_ID": "SCN_000001",
                "Line_No": "1",
                "Speaker_Type": "CHARACTER",
                "Character_ID": "",
                "Dialogue_Text": "Hello",
                "Status": "DRAFT",
            }
        ],
    }
    errors = validate_rows(SCHEMA_DIR, tables)
    assert any("Character_ID is required" in error for error in errors)


def test_non_character_dialogue_must_not_reference_character() -> None:
    tables = {
        "SCENES": [{"Scene_ID": "SCN_000001"}],
        "CHARACTER_MASTER": [{"Character_ID": "CHR_000001"}],
        "SCENE_DIALOGUE_LINES": [
            {
                "Dialogue_Line_ID": "SDL_000001",
                "Scene_ID": "SCN_000001",
                "Line_No": "1",
                "Speaker_Type": "NARRATOR",
                "Character_ID": "CHR_000001",
                "Dialogue_Text": "Narration",
                "Status": "DRAFT",
            }
        ],
    }
    errors = validate_rows(SCHEMA_DIR, tables)
    assert any("Character_ID must be blank" in error for error in errors)


def test_only_one_approved_script_per_episode() -> None:
    tables = {
        "EPISODE_MASTER": [{"Episode_ID": "EPI_000001"}],
        "SCRIPT_MASTER": [
            {
                "Script_ID": "SCR_000001",
                "Episode_ID": "EPI_000001",
                "Script_Version": "1",
                "Script_Title": "Draft A",
                "Full_Script": "A",
                "Script_Status": "APPROVED",
            },
            {
                "Script_ID": "SCR_000002",
                "Episode_ID": "EPI_000001",
                "Script_Version": "2",
                "Script_Title": "Draft B",
                "Full_Script": "B",
                "Script_Status": "APPROVED",
            },
        ],
    }
    errors = validate_rows(SCHEMA_DIR, tables)
    assert any("maximum one APPROVED Script" in error for error in errors)


def test_blank_checkbox_defaults_do_not_create_fake_rows() -> None:
    schemas = load_schemas(SCHEMA_DIR)
    assert schemas["SCENE_REFERENCE_MAP"]["table_name"] == "SCENE_REFERENCE_MAP"
    errors = validate_rows(
        SCHEMA_DIR,
        {"SCENE_REFERENCE_MAP": [{"Is_Required": "FALSE", "Is_Primary": "FALSE"}]},
    )
    assert errors == []

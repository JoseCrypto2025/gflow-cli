from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from gflow_cli.v2_runtime.timestamps import format_utc_timestamp, parse_utc_timestamp
from scripts.validation.validate_v2_data_architecture import _type_error, validate_rows


def test_format_utc_timestamp_normalizes_to_one_terminal_z() -> None:
    local = datetime(2026, 10, 18, 11, 10, 28, tzinfo=timezone(timedelta(hours=7)))

    result = format_utc_timestamp(local)

    assert result == "2026-10-18T04:10:28Z"
    assert result.count("Z") == 1
    assert result.count("+") == 0
    assert result.count("UTC") == 0


def test_format_utc_timestamp_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        format_utc_timestamp(datetime(2026, 10, 18, 4, 10, 28))


def test_parse_utc_timestamp_requires_canonical_z_and_is_aware() -> None:
    parsed = parse_utc_timestamp("2026-10-18T04:10:28Z")
    assert parsed == datetime(2026, 10, 18, 4, 10, 28, tzinfo=UTC)
    assert parsed.tzinfo is UTC

    for malformed in (
        "2026-10-18T04:10:28 UTCZ",
        "2026-10-18 04:10:28 UTC",
        "2026-10-18T04:10:28",
        "2026-10-18T04:10:28+07:00",
    ):
        with pytest.raises(ValueError):
            parse_utc_timestamp(malformed)


def test_schema_validator_rejects_malformed_utc_timestamp() -> None:
    row = {
        "Scene_Prompt_ID": "SPV_999999",
        "Scene_ID": "SCN_000001",
        "Prompt_Version": "99",
        "Generation_Profile_ID": "GPR_0002",
        "Prompt_Status": "SUPERSEDED",
        "Created_At": "2026-10-18T04:10:28 UTCZ",
    }

    errors = validate_rows(
        Path("schemas"),
        {"SCENE_PROMPT_VERSION": [row]},
    )

    assert any("SCENE_PROMPT_VERSION row 2: Created_At" in error for error in errors)
    assert _type_error("2026-10-18T04:10:28 UTCZ", "TIMESTAMP") is not None

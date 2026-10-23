from __future__ import annotations

import json
from pathlib import Path

import pytest

from gflow_cli.v2_runtime.google_sheets import (
    APPROVED_SPREADSHEET_ID,
    SOURCE_LIVE_GOOGLE_SHEETS,
    GoogleSheetsReadError,
    GoogleSheetsSnapshot,
)
from scripts.operations.export_d2_master_snapshot import _normalize_snapshot_rows


def _schema_dir(path: Path) -> Path:
    (path / "master").mkdir(parents=True)
    (path / "transaction").mkdir()
    (path / "relation").mkdir()
    schema = {
        "table_name": "DEMO",
        "table_type": "master",
        "primary_key": "Record_ID",
        "fields": [
            {"field_name": "Record_ID", "required": True, "data_type": "STRING"},
            {"field_name": "Value", "required": False, "data_type": "STRING"},
            {"field_name": "Is_Default", "required": False, "data_type": "BOOLEAN"},
        ],
    }
    (path / "master" / "DEMO.schema.json").write_text(json.dumps(schema), encoding="utf-8")
    return path


def _snapshot(rows: list[dict[str, object]]) -> GoogleSheetsSnapshot:
    return GoogleSheetsSnapshot(
        spreadsheet_id=APPROVED_SPREADSHEET_ID,
        read_at="2026-10-23T12:00:00Z",
        source=SOURCE_LIVE_GOOGLE_SHEETS,
        tables={"DEMO": rows},
    )


def test_normalization_drops_false_only_blank_pk_and_preserves_real_false(tmp_path: Path) -> None:
    schema_dir = _schema_dir(tmp_path / "schemas")
    snapshot = _snapshot(
        [
            {"Record_ID": "", "Value": "", "Is_Default": "FALSE"},
            {"Record_ID": "", "Value": None, "Is_Default": False},
            {"Record_ID": "ID_0001", "Value": "ok", "Is_Default": "FALSE"},
        ]
    )

    normalized, removed = _normalize_snapshot_rows(snapshot, schema_dir)

    assert removed == 2
    assert normalized.tables == {
        "DEMO": [{"Record_ID": "ID_0001", "Value": "ok", "Is_Default": "FALSE"}]
    }


@pytest.mark.parametrize(
    "row",
    [
        {"Record_ID": "", "Value": "real data", "Is_Default": "FALSE"},
        {"Record_ID": "", "Value": "", "Is_Default": "TRUE"},
        {"Record_ID": "", "Value": 0, "Is_Default": "FALSE"},
    ],
)
def test_normalization_fails_closed_when_blank_pk_has_substantive_data(
    tmp_path: Path, row: dict[str, object]
) -> None:
    schema_dir = _schema_dir(tmp_path / "schemas")

    with pytest.raises(GoogleSheetsReadError, match="without its primary key"):
        _normalize_snapshot_rows(_snapshot([row]), schema_dir)

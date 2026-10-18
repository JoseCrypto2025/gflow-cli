from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

import pytest
from pytest_bdd import given, scenarios, then, when

from gflow_cli.v2_runtime.google_sheets import GoogleSheetsReadError, GoogleSheetsSnapshotSource
from scripts.validation.validate_v2_data_architecture import validate_rows

scenarios("d5_trusted_sheets.feature")


@pytest.fixture
def context() -> dict[str, Any]:
    return {}


@given("the D5 process has no live Sheets credential")
def _no_live_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GFLOW_GOOGLE_WORKSPACE_CREDENTIALS", raising=False)


@when("the operator passes a local snapshot path", target_fixture="snapshot_attempt")
def _pass_snapshot(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    from scripts.operations import vps_flow_presubmit

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "vps_flow_presubmit",
            "--snapshot",
            str(Path("operator.json")),
            "--scene-id",
            "SCN_000001",
            "--profile-id",
            "GPR_0002",
        ],
    )
    try:
        vps_flow_presubmit.main()
    except SystemExit as error:
        return {"exit_code": error.code}
    raise AssertionError("the production entry point unexpectedly accepted --snapshot")


@then("the production entry point refuses the snapshot argument")
def _snapshot_refused(snapshot_attempt: dict[str, object]) -> None:
    assert snapshot_attempt["exit_code"] == 2


class _ChangedHeaderTransport:
    def read_values(
        self, spreadsheet_id: str, sheet_name: str, value_range: str
    ) -> list[list[str]]:
        del spreadsheet_id, sheet_name, value_range
        return [["Value", "Record_ID"], ["x", "ID_1"]]


@given("fake Sheets transport returns a changed header")
def _changed_header(context: dict[str, Any], tmp_path: Path) -> None:
    schema_root = tmp_path / "schemas"
    for folder in ("master", "transaction", "relation"):
        (schema_root / folder).mkdir(parents=True, exist_ok=True)
    (schema_root / "master" / "DEMO.schema.json").write_text(
        '{"table_name":"DEMO","table_type":"master","fields":'
        '[{"field_name":"Record_ID"},{"field_name":"Value"}]}',
        encoding="utf-8",
    )
    context["source"] = GoogleSheetsSnapshotSource(
        _ChangedHeaderTransport(), schema_root, validator=lambda *_: []
    )


@when("the source assembles its snapshot", target_fixture="source_attempt")
def _read_changed(context: dict[str, Any]) -> dict[str, object]:
    try:
        context["source"].read()
    except GoogleSheetsReadError as error:
        return {"error": error}
    raise AssertionError("changed header unexpectedly passed")


@then("the source returns a safe schema hold")
def _schema_hold(source_attempt: dict[str, object]) -> None:
    error = source_attempt["error"]
    assert isinstance(error, GoogleSheetsReadError)
    assert "headers do not match" in str(error)


@given("one approved TEST prompt and one approved PROD prompt for a Scene")
def _dual_approval_rows(context: dict[str, Any]) -> None:
    context["tables"] = {
        "GENERATION_PROFILE_MASTER": [
            {"Generation_Profile_ID": "GPR_0001", "Status": "ACTIVE"},
            {"Generation_Profile_ID": "GPR_0002", "Status": "ACTIVE"},
        ],
        "SCENE_PROMPT_VERSION": [
            {
                "Scene_Prompt_ID": "SPV_000001",
                "Scene_ID": "SCN_000001",
                "Prompt_Version": "1",
                "Generation_Profile_ID": "GPR_0001",
                "Compiled_Prompt_Text": "test",
                "Prompt_Status": "APPROVED",
                "Approved_At": "2026-10-18T00:00:00Z",
            },
            {
                "Scene_Prompt_ID": "SPV_000002",
                "Scene_ID": "SCN_000001",
                "Prompt_Version": "1",
                "Generation_Profile_ID": "GPR_0002",
                "Compiled_Prompt_Text": "prod",
                "Prompt_Status": "APPROVED",
                "Approved_At": "2026-10-18T00:00:00Z",
            },
        ],
    }


@when("the V2 validator checks the prompt rows", target_fixture="approval_validation")
def _validate_dual_approval(context: dict[str, Any]) -> dict[str, object]:
    from pathlib import Path

    schema_dir = Path(__file__).resolve().parents[2] / "schemas"
    return {
        "errors": validate_rows(
            schema_dir, cast(dict[str, list[dict[str, object]]], context["tables"])
        )
    }


@then("both profile-scoped approvals are valid")
def _both_valid(approval_validation: dict[str, object]) -> None:
    errors = cast(list[str], approval_validation["errors"])
    assert not any("maximum one APPROVED prompt" in error for error in errors)

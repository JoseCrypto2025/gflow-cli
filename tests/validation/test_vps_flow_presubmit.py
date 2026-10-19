from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from gflow_cli.v2_runtime.config import RuntimeSettings
from gflow_cli.v2_runtime.errors import FlowPresubmitError
from gflow_cli.v2_runtime.google_flow import FlowPresubmitResult
from gflow_cli.v2_runtime.phase_e import Row
from scripts.operations import vps_flow_presubmit as presubmit
from tests.validation.test_phase_e_prompt_preflight import _fixtures


def _settings(tmp_path: Path, environment: str = "production") -> RuntimeSettings:
    return RuntimeSettings(
        environment,
        "google-drive",
        tmp_path / "state",
        tmp_path / "temp",
        tmp_path / "logs",
    )


def _prod_tables() -> dict[str, list[dict[str, object]]]:
    tables = deepcopy(_fixtures())
    tables["SCENES"][0]["Aspect_Ratio_Override"] = "9:16"
    for capability in tables["MODEL_CAPABILITY_MAP"]:
        if capability.get("Capability_Type") == "ASPECT_RATIO":
            capability["Capability_Value"] = '["16:9","9:16"]'
    profile = tables["GENERATION_PROFILE_MASTER"][0]
    profile["Environment"] = "PROD"
    account = tables["PROVIDER_ACCOUNT_MASTER"][0]
    account.update(
        {
            "Environment": "PROD",
            "Auth_Profile_Key": "GFLOW_PROD_AUTH",
            "Execution_Target": "WEB_UI",
            "Base_URL": "https://flow.google.com/",
            "Provider_Project_ID": "prod-project-id",
            "Status": "ACTIVE",
        }
    )
    account["Provider_Account_ID"] = "PVA_0002"
    profile["Provider_Account_ID"] = "PVA_0002"
    tables["MODEL_MASTER"][0]["Model_Name"] = "Veo production model"
    return tables


def _phase_e_rows(tables: dict[str, list[dict[str, object]]]) -> Mapping[str, list[Row]]:
    return cast(Mapping[str, list[Row]], tables)


def _add_approved(tables: dict[str, list[dict[str, object]]], profile_id: str = "GPR_0001") -> None:
    from gflow_cli.v2_runtime.phase_e import InMemorySceneCatalog, preflight_scene

    preflight = preflight_scene(
        InMemorySceneCatalog(_phase_e_rows(tables)), "SCN_000001", profile_id, "PROD"
    )
    assert preflight.status == "PREFLIGHT_PASS"
    tables["SCENE_PROMPT_VERSION"] = [
        {
            "Scene_Prompt_ID": "SPV_000004",
            "Scene_ID": "SCN_000001",
            "Generation_Profile_ID": profile_id,
            "Prompt_Status": "APPROVED",
            "Compiled_Prompt_Text": preflight.compiled_prompt,
            "Compiled_Negative_Prompt": preflight.compiled_negative_prompt or "",
            "Source_Context_SHA256": preflight.source_context_sha256,
            "Prompt_SHA256": preflight.prompt_sha256,
        }
    ]


def test_build_request_resolves_only_current_prod_rows(tmp_path: Path) -> None:
    tables = _prod_tables()
    _add_approved(tables)

    request, names = presubmit.build_prod_presubmit_request(
        _phase_e_rows(tables),
        scene_id="SCN_000001",
        profile_id="GPR_0001",
        settings=_settings(tmp_path),
        expected_email="flow@example.com",
    )

    assert request.provider_account_id == "PVA_0002"
    assert request.provider_project_id == "prod-project-id"
    assert request.prompt_version.scene_prompt_id == "SPV_000004"
    assert names == {"MDL_0001": "Veo production model"}


def test_build_request_ignores_false_only_profile_rows(tmp_path: Path) -> None:
    tables = _prod_tables()
    production_profile = tables["GENERATION_PROFILE_MASTER"][0]
    production_profile["Generation_Profile_ID"] = "GPR_0002"
    production_profile["Provider_Account_ID"] = "PVA_0002"
    test_profile = deepcopy(production_profile)
    test_profile.update(
        {
            "Generation_Profile_ID": "GPR_0001",
            "Environment": "TEST",
            "Provider_Account_ID": "PVA_0001",
        }
    )
    tables["GENERATION_PROFILE_MASTER"].append(test_profile)
    tables["GENERATION_PROFILE_MASTER"].extend({"Is_Default": False} for _ in range(4))
    _add_approved(tables, "GPR_0002")

    request, _ = presubmit.build_prod_presubmit_request(
        _phase_e_rows(tables),
        scene_id="SCN_000001",
        profile_id="GPR_0002",
        settings=_settings(tmp_path),
        expected_email="flow@example.com",
    )

    assert request.provider_account_id == "PVA_0002"
    assert request.generation_mode == "TEXT_TO_VIDEO"


@pytest.mark.parametrize(
    ("case", "profile_id", "subreason"),
    [
        ("missing", "GPR_9999", "profile_not_found"),
        ("duplicate", "GPR_0001", "profile_duplicate"),
        ("environment", "GPR_0001", "profile_environment_invalid"),
        ("status", "GPR_0001", "profile_status_invalid"),
    ],
)
def test_build_request_reports_profile_resolution_subreason(
    tmp_path: Path, case: str, profile_id: str, subreason: str
) -> None:
    tables = _prod_tables()
    _add_approved(tables)
    profile = tables["GENERATION_PROFILE_MASTER"][0]
    if case == "duplicate":
        tables["GENERATION_PROFILE_MASTER"].append(deepcopy(profile))
    elif case == "environment":
        profile["Environment"] = "TEST"
    elif case == "status":
        profile["Status"] = "HOLD"

    with pytest.raises(presubmit.PresubmitInputError) as exc_info:
        presubmit.build_prod_presubmit_request(
            _phase_e_rows(tables),
            scene_id="SCN_000001",
            profile_id=profile_id,
            settings=_settings(tmp_path),
            expected_email="flow@example.com",
        )

    assert exc_info.value.code == "PROD_PROFILE_INVALID"
    assert exc_info.value.subreason == subreason


@pytest.mark.parametrize(
    ("change", "expected_code"),
    [
        ("test_profile", "PROD_PROFILE_INVALID"),
        ("bad_auth_key", "PROD_ACCOUNT_INVALID"),
        ("bad_project", "PROD_ACCOUNT_INVALID"),
        ("stale_approval", "APPROVED_PROMPT_STALE"),
        ("duplicate_approval", "APPROVED_PROMPT_AMBIGUOUS"),
    ],
)
def test_build_request_rejects_ambiguous_or_non_prod_state(
    tmp_path: Path, change: str, expected_code: str
) -> None:
    tables = _prod_tables()
    _add_approved(tables)
    if change == "test_profile":
        tables["GENERATION_PROFILE_MASTER"][0]["Environment"] = "TEST"
    elif change == "bad_auth_key":
        tables["PROVIDER_ACCOUNT_MASTER"][0]["Auth_Profile_Key"] = "GFLOW_V2_TEST_AUTH"
    elif change == "bad_project":
        tables["PROVIDER_ACCOUNT_MASTER"][0]["Provider_Project_ID"] = ""
    elif change == "stale_approval":
        tables["SCENE_PROMPT_VERSION"][0]["Prompt_SHA256"] = "0" * 64
    elif change == "duplicate_approval":
        tables["SCENE_PROMPT_VERSION"].append(dict(tables["SCENE_PROMPT_VERSION"][0]))

    with pytest.raises(presubmit.PresubmitInputError) as exc_info:
        presubmit.build_prod_presubmit_request(
            _phase_e_rows(tables),
            scene_id="SCN_000001",
            profile_id="GPR_0001",
            settings=_settings(tmp_path),
            expected_email="flow@example.com",
        )

    assert exc_info.value.code == expected_code


def test_build_request_requires_explicit_identity_and_production_runtime(tmp_path: Path) -> None:
    with pytest.raises(presubmit.PresubmitInputError, match="Google identity"):
        presubmit.build_prod_presubmit_request(
            {},
            scene_id="SCN_000001",
            profile_id="GPR_0001",
            settings=_settings(tmp_path),
            expected_email="",
        )
    with pytest.raises(presubmit.PresubmitInputError) as exc_info:
        presubmit.build_prod_presubmit_request(
            {},
            scene_id="SCN_000001",
            profile_id="GPR_0001",
            settings=_settings(tmp_path, "test"),
            expected_email="flow@example.com",
        )
    assert exc_info.value.code == "PRODUCTION_RUNTIME_REQUIRED"


def test_snapshot_source_and_freshness_are_required(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text(
        '{"spreadsheet_id":"wrong","read_at":"2026-10-18T00:00:00Z","tables":{}}',
        encoding="utf-8",
    )

    with pytest.raises(presubmit.PresubmitInputError) as exc_info:
        presubmit.load_snapshot(path, now=datetime(2026, 10, 18, tzinfo=UTC))
    assert exc_info.value.code == "SNAPSHOT_SOURCE_MISMATCH"


def test_snapshot_validates_shape_and_secret_field_names(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text(
        json.dumps(
            {
                "spreadsheet_id": presubmit.SPREADSHEET_ID,
                "read_at": "2026-10-18T00:00:00Z",
                "tables": {"SCENES": [{"Scene_ID": "SCN_000001"}]},
            }
        ),
        encoding="utf-8",
    )
    tables = presubmit.load_snapshot(path, now=datetime(2026, 10, 18, tzinfo=UTC))
    assert tables["SCENES"] == [{"Scene_ID": "SCN_000001"}]

    path.write_text(
        json.dumps(
            {
                "spreadsheet_id": presubmit.SPREADSHEET_ID,
                "read_at": "2026-10-18T00:00:00Z",
                "tables": {"SCENES": "not rows"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(presubmit.PresubmitInputError) as exc_info:
        presubmit.load_snapshot(path, now=datetime(2026, 10, 18, tzinfo=UTC))
    assert exc_info.value.code == "SNAPSHOT_SHAPE_INVALID"


def test_snapshot_rejects_stale_read_time(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text(
        f'{{"spreadsheet_id":"{presubmit.SPREADSHEET_ID}","read_at":"2026-10-17T00:00:00Z","tables":{{}}}}',
        encoding="utf-8",
    )

    with pytest.raises(presubmit.PresubmitInputError) as exc_info:
        presubmit.load_snapshot(path, now=datetime(2026, 10, 18, tzinfo=UTC))
    assert exc_info.value.code == "SNAPSHOT_STALE"


def test_snapshot_rejects_secret_shaped_fields(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.json"
    path.write_text(
        f'{{"spreadsheet_id":"{presubmit.SPREADSHEET_ID}","read_at":"2026-10-18T00:00:00Z","session_token":"hidden","tables":{{}}}}',
        encoding="utf-8",
    )

    with pytest.raises(presubmit.PresubmitInputError) as exc_info:
        presubmit.load_snapshot(path, now=datetime(2026, 10, 18, tzinfo=UTC))
    assert exc_info.value.code == "SNAPSHOT_SECRET_FIELD"


def test_runner_calls_only_non_submitting_f5(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_prepare(request, **kwargs):
        calls.append({"request": request, **kwargs})
        return FlowPresubmitResult(
            "FLOW_PRESUBMIT_READY",
            request.provider_id,
            request.provider_account_id,
            request.provider_project_id,
            {
                "model_id": request.model_id,
                "generation_mode": request.generation_mode,
                "aspect_ratio": "9:16",
                "output_count": 1,
            },
            0,
            request.prompt_version.prompt_sha256,
            True,
            True,
        )

    monkeypatch.setattr("gflow_cli.v2_runtime.live_flow.prepare_live_flow", fake_prepare)
    tables = _prod_tables()
    _add_approved(tables)
    request, names = presubmit.build_prod_presubmit_request(
        _phase_e_rows(tables),
        scene_id="SCN_000001",
        profile_id="GPR_0001",
        settings=_settings(Path("/tmp")),
        expected_email="flow@example.com",
    )

    report = asyncio.run(
        presubmit.run_prod_presubmit(
            request, model_display_names=names, expected_email="flow@example.com"
        )
    )

    assert len(calls) == 1
    assert calls[0]["auth_profile_key"] == "GFLOW_PROD_AUTH"
    assert calls[0]["expected_email"] == "flow@example.com"
    assert report["generate_clicked"] is False
    assert report["credits_used"] == 0
    assert report["prepared_model_id"] == request.model_id
    assert report["prepared_generation_mode"] == "TEXT_TO_VIDEO"
    assert report["prepared_aspect_ratio"] == "9:16"
    assert report["prepared_output_count"] == 1
    assert report["prepared_reference_count"] == 0
    assert report["prepared_provider_ui_prompt_sha256"] == (
        presubmit.prompt_sha256(
            presubmit.build_agent_only_directive(
                request.prompt_version.compiled_prompt_text,
                request.preflight.technical_configuration,
            ),
            "",
        )
    )


def test_runner_holds_when_f5_does_not_reach_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_prepare(*_args, **_kwargs):
        return FlowPresubmitResult(
            "FLOW_PRESUBMIT_HOLD",
            "PRV_0001",
            "PVA_0002",
            "prod-project-id",
            {},
            0,
            "",
            False,
            False,
        )

    monkeypatch.setattr("gflow_cli.v2_runtime.live_flow.prepare_live_flow", fake_prepare)
    tables = _prod_tables()
    _add_approved(tables)
    request, names = presubmit.build_prod_presubmit_request(
        _phase_e_rows(tables),
        scene_id="SCN_000001",
        profile_id="GPR_0001",
        settings=_settings(Path("/tmp")),
        expected_email="flow@example.com",
    )

    with pytest.raises(presubmit.PresubmitInputError) as exc_info:
        asyncio.run(
            presubmit.run_prod_presubmit(
                request, model_display_names=names, expected_email="flow@example.com"
            )
        )
    assert exc_info.value.code == "F5_NOT_READY"


def test_main_redacts_unexpected_exception_text(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "vps_flow_presubmit.py",
            "--scene-id",
            "SCN_000001",
            "--profile-id",
            "GPR_0001",
        ],
    )

    def fail_settings():
        raise RuntimeError("password=do-not-print")

    monkeypatch.setattr(presubmit, "load_runtime_settings", fail_settings)

    assert presubmit.main() == 2
    output = capsys.readouterr().out
    assert "PRESUBMIT_FAILED" in output
    assert "RuntimeError" in output
    assert "do-not-print" not in output


def test_flow_failure_report_preserves_safe_code_and_root_exception_type() -> None:
    try:
        try:
            raise TimeoutError("cookie=must-not-print")
        except TimeoutError as cause:
            raise FlowPresubmitError("FLOW_PROJECT_MISMATCH", "safe") from cause
    except FlowPresubmitError as exc:
        report = presubmit._flow_failure_report(exc)

    assert report == {
        "state": "HOLD",
        "reason_code": "FLOW_PROJECT_MISMATCH",
        "failure_class": "FLOW_PROJECT_MISMATCH",
        "exception_type": "FlowPresubmitError",
        "original_exception_type": "TimeoutError",
        "phase": "FLOW_PREPARATION",
        "detail": "Flow preparation failed safely",
    }
    assert "cookie" not in json.dumps(report).casefold()


def test_main_holds_before_flow_when_v2_google_credential_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from gflow_cli.v2_runtime.config import RuntimeSettings

    monkeypatch.setattr(
        sys,
        "argv",
        ["vps_flow_presubmit.py", "--scene-id", "SCN_000001", "--profile-id", "GPR_0002"],
    )
    monkeypatch.delenv("GFLOW_GOOGLE_WORKSPACE_CREDENTIALS", raising=False)
    monkeypatch.setattr(
        presubmit,
        "load_runtime_settings",
        lambda: RuntimeSettings(
            "production", "google-drive", Path("/state"), Path("/tmp"), Path("/logs")
        ),
    )
    monkeypatch.setattr(
        presubmit,
        "run_prod_presubmit",
        lambda *_args, **_kwargs: pytest.fail("Flow must not open without V2 Sheets auth"),
    )

    assert presubmit.main() == 2
    report = json.loads(capsys.readouterr().out)
    assert report["reason_code"] == "GOOGLE_SHEETS_AUTH_NOT_CONFIGURED"
    assert report["state"] == "HOLD"

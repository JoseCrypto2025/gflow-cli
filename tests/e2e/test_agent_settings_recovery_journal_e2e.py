"""Live, non-submitting regression for Agent settings journal metadata."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, scenarios, then, when

from gflow_cli.v2_runtime.agent_recovery import AgentSettingsRecoveryJournal
from gflow_cli.v2_runtime.config import load_runtime_settings
from gflow_cli.v2_runtime.google_sheets import (
    GoogleSheetsSnapshot,
)
from scripts.operations.vps_flow_presubmit import (
    _read_live_snapshot,
    build_prod_presubmit_request,
    run_prod_presubmit,
)

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_auth]

scenarios("../features/agent_settings_recovery_journal_metadata.feature")

_ACCOUNT_EMAIL = "singleava508@gmail.com"
_ACCOUNT_ID = "PVA_0002"
_PROJECT_ID = "aafe9282-071f-4333-baa4-92b9f76a9c27"
_PROFILE_ID = "GPR_0002"
_PRODUCTION_JOB_ID = "PRD_000003"
_SCENE_JOB_ID = "SJB_000003"
_RUN_INTENT_KEY = (
    "PROD_SCENE_RUN_V2_f7167d8fe108dfc16e4b787bdbcdb13214ff13af3bf95fdec"
    "598b2d1cf77dea3_d2-certification-run-3"
)
_STATE_ROOT = Path("/var/lib/gflow")
_PROFILE_HOME = Path("/var/lib/gflow/browser-state")
_CREDENTIAL_PATH = Path("/etc/gflow-v2/google-authorized-user.json")


@pytest.fixture
def world() -> dict[str, Any]:
    return {}


@given("the existing Run 3 production job and pinned Flow profile")
def _existing_run_three(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name != "posix":
        pytest.skip("the canonical PROD browser profile is installed on the V2 runtime host")
    if os.environ.get("GFLOW_CLI_E2E_PROFILE", "").strip() != "gflow-prod":
        pytest.skip("set GFLOW_CLI_E2E_PROFILE=gflow-prod for this zero-credit live regression")
    if not _PROFILE_HOME.is_dir() or _PROFILE_HOME.is_symlink():
        pytest.skip("the canonical gflow-prod browser profile is unavailable")
    if not _CREDENTIAL_PATH.is_file() or _CREDENTIAL_PATH.is_symlink():
        pytest.skip("the canonical V2 Google Workspace credential is unavailable")

    monkeypatch.setenv("GFLOW_CLI_HOME", str(_PROFILE_HOME))
    monkeypatch.setenv("GFLOW_GOOGLE_WORKSPACE_CREDENTIALS", str(_CREDENTIAL_PATH))
    monkeypatch.setenv("GFLOW_EXPECTED_GOOGLE_EMAIL", _ACCOUNT_EMAIL)
    settings = load_runtime_settings()
    if settings.environment != "production" or settings.runtime_state_root != _STATE_ROOT:
        pytest.fail("the live F5 regression requires the canonical production runtime roots")

    source_root = Path(__file__).resolve().parents[2]
    snapshot = _read_live_snapshot(_CREDENTIAL_PATH, source_root / "schemas")
    productions = [
        row
        for row in snapshot.tables.get("PRODUCTION_JOBS", [])
        if row.get("Request_Idempotency_Key") == _RUN_INTENT_KEY
    ]
    assert len(productions) == 1
    production = productions[0]
    assert production.get("Production_Job_ID") == _PRODUCTION_JOB_ID
    assert str(production.get("Production_Run_No", "")).strip() == "3"
    scenes = [
        row
        for row in snapshot.tables.get("SCENE_JOBS", [])
        if row.get("Production_Job_ID") == _PRODUCTION_JOB_ID
    ]
    assert len(scenes) == 1
    assert scenes[0].get("Scene_Job_ID") == _SCENE_JOB_ID
    assert not any(
        row.get("Production_Job_ID") == "PRD_000004" or row.get("Scene_Job_ID") == "SJB_000004"
        for row in snapshot.tables.get("SCENE_JOBS", [])
    )

    runtime_root = settings.runtime_state_root
    world["snapshot"] = snapshot
    world["source_root"] = source_root
    world["runtime_root"] = runtime_root
    world["d2_ledger_before"] = _read_bytes(
        runtime_root / "production" / "phase-g" / "d2-credit-ledger.json"
    )
    world["credit_ledger_before"] = _read_bytes(
        runtime_root / "production-credit" / "shared-ledger.json"
    )


@when("the non-submitting F5 presubmit prepares the approved request")
def _run_f5(world: dict[str, Any]) -> None:
    snapshot = world["snapshot"]
    assert isinstance(snapshot, GoogleSheetsSnapshot)
    request, display_names = build_prod_presubmit_request(
        snapshot.tables,
        scene_id="SCN_000001",
        profile_id=_PROFILE_ID,
        settings=load_runtime_settings(),
        expected_email=_ACCOUNT_EMAIL,
    )
    assert request.provider_account_id == _ACCOUNT_ID
    assert request.provider_project_id == _PROJECT_ID
    assert display_names[request.model_id] == "Veo 3.1 - Lite"
    assert request.preflight.technical_configuration.get("duration_sec") == 8
    assert request.preflight.technical_configuration.get("aspect_ratio") == "9:16"
    assert request.preflight.technical_configuration.get("output_count") == 1
    report = asyncio.run(
        run_prod_presubmit(
            request,
            model_display_names=display_names,
            expected_email=_ACCOUNT_EMAIL,
        )
    )
    world["request"] = request
    world["report"] = report


@then("the Agent settings journal is clean and the request is ready")
def _journal_is_clean_and_request_ready(world: dict[str, Any]) -> None:
    request = world["request"]
    report = world["report"]
    assert report["state"] == "FLOW_PRESUBMIT_READY"
    assert report["provider_account_id"] == _ACCOUNT_ID
    assert report["provider_project_id"] == _PROJECT_ID
    assert report["prepared_generation_mode"] == "TEXT_TO_VIDEO"
    assert report["prepared_aspect_ratio"] == "9:16"
    assert report["prepared_output_count"] == 1
    assert report["prepared_model_id"] == request.model_id
    assert report["generate_present"] is True
    assert report["generate_enabled"] is True
    assert report["generate_clicked"] is False
    assert report["credits_used"] == 0

    journal = AgentSettingsRecoveryJournal(
        request.runtime_state_root, _ACCOUNT_ID, _PROJECT_ID
    ).read()
    assert journal is not None and journal.state == "CLEAN"
    assert "model_name" not in journal.original_settings
    assert "model_priority" not in journal.original_settings


@then("no attempt, reservation, Generate action, or provider credit is created")
def _no_provider_mutations(world: dict[str, Any]) -> None:
    runtime_root = world["runtime_root"]
    assert (
        _read_bytes(runtime_root / "production" / "phase-g" / "d2-credit-ledger.json")
        == world["d2_ledger_before"]
    )
    assert (
        _read_bytes(runtime_root / "production-credit" / "shared-ledger.json")
        == world["credit_ledger_before"]
    )


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes() if path.is_file() else b""

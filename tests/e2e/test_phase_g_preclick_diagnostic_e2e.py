"""Read-only live Phase G pre-click diagnosis for the existing legacy replacement."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gflow_cli.v2_runtime.config import load_runtime_settings
from scripts.operations.run_legacy_replacement import run_once

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_auth]

_OPT_IN = "GFLOW_CLI_E2E_RUN_PRECLICK_DIAGNOSTIC"
_PARENT_ATTEMPT_ID = "D2_13c62112d9881710019049d95cb6da57"
_ATTEMPT_ID = "D2_9c13df47d64c68de30cd036bcd1fec09"
_AUTHORIZATION_ID = "LRA_18A2C0EE3248A7C93E6D1991"
_PRODUCTION_JOB_ID = "PRD_000002"
_SCENE_JOB_ID = "SJB_000002"


def _file_bytes(path: Path) -> bytes:
    return path.read_bytes() if path.is_file() else b""


def test_read_only_diagnostic_reuses_the_exact_prepared_attempt() -> None:
    if os.environ.get(_OPT_IN) != "I_CONFIRM_READ_ONLY_PRECLICK_DIAGNOSTIC":
        pytest.skip(f"set {_OPT_IN}=I_CONFIRM_READ_ONLY_PRECLICK_DIAGNOSTIC to opt in")
    if os.environ.get("GFLOW_CLI_E2E_PROFILE", "").strip() != "gflow-prod":
        pytest.fail("Phase G replacement diagnosis accepts only the existing gflow-prod profile")

    runtime_root = load_runtime_settings().runtime_state_root
    d2_ledger_path = runtime_root / "production" / "phase-g" / "d2-credit-ledger.json"
    shared_ledger_path = runtime_root / "production-credit" / "shared-ledger.json"
    d2_before = _file_bytes(d2_ledger_path)
    shared_before = _file_bytes(shared_ledger_path)

    result = run_once(
        scene_id="SCN_000001",
        profile_id="GPR_0002",
        account_id="PVA_0002",
        project_id="aafe9282-071f-4333-baa4-92b9f76a9c27",
        expected_email="singleava508@gmail.com",
        parent_attempt_id=_PARENT_ATTEMPT_ID,
        execute=False,
        diagnose_preclick_mode=True,
        authorization_id=_AUTHORIZATION_ID,
        replacement_attempt_id=_ATTEMPT_ID,
        replacement_production_job_id=_PRODUCTION_JOB_ID,
        replacement_scene_job_id=_SCENE_JOB_ID,
    )

    saved = json.loads(d2_ledger_path.read_text(encoding="utf-8"))
    attempts = [row for row in saved["attempts"] if row.get("d2_run_id") == _ATTEMPT_ID]
    authorizations = [
        row
        for row in saved["legacy_replacement_authorizations"]
        if row.get("authorization_id") == _AUTHORIZATION_ID
    ]
    assert result["status"] in {"PRECLICK_DIAGNOSTIC_COMPLETE", "HOLD"}, result
    assert result["attempt_id"] == _ATTEMPT_ID
    assert result["generate_action_count"] == 0
    assert result["provider_submit_request_count"] == 0
    assert result["reservation_created"] is False
    assert result["flow_state_changed"] is False
    assert len(attempts) == len(authorizations) == 1
    assert attempts[0]["submission_state"] == "PREPARED"
    assert attempts[0]["generate_action_count"] == 0
    assert attempts[0]["provider_submit_request_count"] == 0
    assert authorizations[0]["authorization_status"] == "CONSUMED"
    assert authorizations[0]["replacement_attempt_id"] == _ATTEMPT_ID
    assert _file_bytes(d2_ledger_path) == d2_before
    assert _file_bytes(shared_ledger_path) == shared_before
    diagnostic_path = Path(str(result["diagnostic_path"]))
    events = [json.loads(line) for line in diagnostic_path.read_text(encoding="utf-8").splitlines()]
    if result["status"] == "HOLD":
        failure = result["preclick_diagnostic"]
        assert isinstance(failure, dict)
        assert failure["reason_code"] == "PHASE_G_EXECUTION_FAILED_BEFORE_CLICK"
        assert failure["exception_type"]
        assert failure["failed_stage"]
        assert failure["source_file"]
        assert isinstance(failure["source_line"], int)
        assert len(str(failure["traceback_digest"])) == 64
        assert events[-1]["event_type"] == "FAILURE"
    else:
        assert result["stopped_before_stage"] == "JOURNAL_BEGIN"
        assert events[-1]["event_type"] == "DIAGNOSTIC_COMPLETE"

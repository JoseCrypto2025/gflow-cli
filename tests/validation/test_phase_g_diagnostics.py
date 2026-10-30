from __future__ import annotations

import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from gflow_cli.v2_runtime.agent_recovery import AgentSettingsRecoveryJournal
from gflow_cli.v2_runtime.legacy_replacement import (
    ReplacementQuote,
    consume_legacy_replacement_authorization,
)
from gflow_cli.v2_runtime.phase_g import (
    D2DailyCreditLedger,
    FlowGenerationPort,
    GoogleFlowAgentOnlyGenerationExecutor,
    PhaseGExecutionRequest,
)
from gflow_cli.v2_runtime.phase_g_diagnostics import (
    PhaseGDiagnosticRecorder,
    PhaseGExecutionProgress,
)
from gflow_cli.v2_runtime.phase_g_prod_reconciliation import ProdAttemptIdentity
from gflow_cli.v2_runtime.production_execution import (
    CreditBudgetContext,
    ProductionCreditGuard,
    ProductionExecutionContext,
)
from scripts.operations import run_legacy_replacement
from tests.validation.test_legacy_replacement import (
    _approved_ledger,
    _replacement_request,
)
from tests.validation.test_phase_g import FakeGenerationPort, make_executor, request_for


def _recorder(runtime_root: Path) -> PhaseGDiagnosticRecorder:
    return PhaseGDiagnosticRecorder(
        runtime_root,
        attempt_id="D2_replacement-0001",
        authorization_id="LRA_" + "A" * 24,
        production_job_id="PRD_000002",
        scene_job_id="SJB_000002",
    )


def test_diagnostic_recorder_persists_redacted_failure_and_traceback_digest(
    tmp_path: Path,
) -> None:
    recorder = _recorder(tmp_path / "runtime")
    progress = PhaseGExecutionProgress(recorder=recorder, strict=True)
    progress.start("REQUEST_BINDING")
    progress.complete("REQUEST_BINDING")
    progress.start("CONSUMED_AUTHORIZATION_VALIDATION")
    private_prompt = "private prompt phrase that must not be persisted"
    try:
        raise RuntimeError(f"Bearer ya29.secret {private_prompt}")
    except RuntimeError as error:
        failure = recorder.record_failure(
            error,
            progress,
            repository_root=Path(__file__).resolve().parents[2],
            sensitive_values=(private_prompt,),
        )

    events = [json.loads(line) for line in recorder.path.read_text(encoding="utf-8").splitlines()]
    assert failure["exception_type"] == "RuntimeError"
    assert failure["failed_stage"] == "CONSUMED_AUTHORIZATION_VALIDATION"
    assert failure["last_completed_stage"] == "REQUEST_BINDING"
    assert failure["traceback_digest"] and len(str(failure["traceback_digest"])) == 64
    assert failure["source_file"] == "tests/validation/test_phase_g_diagnostics.py"
    assert isinstance(failure["source_line"], int)
    assert "ya29.secret" not in json.dumps(events)
    assert private_prompt not in json.dumps(events)
    assert "Bearer" not in str(failure["exception_message"])
    assert datetime.fromisoformat(str(failure["timestamp"]).replace("Z", "+00:00")).tzinfo


def test_post_click_reconciliation_failure_is_durably_recorded_without_provider_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "runtime"
    recorder = _recorder(runtime_root)
    progress = PhaseGExecutionProgress(recorder=recorder)
    identity = ProdAttemptIdentity(
        production_job_id="PRD_000002",
        scene_job_id="SJB_000002",
        d2_run_id="D2_replacement-0001",
        provider_account_id="PVA_0001",
        provider_project_id="project-1",
        scene_id="SCN_000001",
        scene_prompt_id="SPV_000001",
        generation_profile_id="GPR_0001",
        prompt_sha256="a" * 64,
    )
    context = ProductionExecutionContext(
        environment="PROD",
        provider_id="PRV_0001",
        provider_account_id=identity.provider_account_id,
        provider_project_id=identity.provider_project_id,
        auth_profile="GFLOW_PROD_AUTH",
        scene_id=identity.scene_id,
        generation_profile_id=identity.generation_profile_id,
        scene_prompt_id=identity.scene_prompt_id,
        attempt_id=identity.d2_run_id,
        credit_budget_context=CreditBudgetContext(),
        production_job_id=identity.production_job_id,
        scene_job_id=identity.scene_job_id,
    )
    private_prompt = "private post-click prompt that must be redacted"
    provider_actions: list[str] = []

    def fail_reconciliation(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(f"ledger mismatch {private_prompt}")

    def forbidden_generation(*_args: object, **_kwargs: object) -> None:
        provider_actions.append("generate")
        raise AssertionError("reconciliation must not execute generation")

    monkeypatch.setattr(run_legacy_replacement, "reconcile_prod_same_attempt", fail_reconciliation)
    monkeypatch.setattr(run_legacy_replacement, "execute_and_validate", forbidden_generation)

    with pytest.raises(RuntimeError, match="ledger mismatch"):
        run_legacy_replacement._reconcile_with_diagnostics(
            runtime_root,
            context,
            identity,
            credit_guard=cast(ProductionCreditGuard, object()),
            recorder=recorder,
            progress=progress,
            repository_root=Path(__file__).resolve().parents[2],
            sensitive_values=(private_prompt,),
        )

    events = [json.loads(line) for line in recorder.path.read_text(encoding="utf-8").splitlines()]
    failure = next(event for event in events if event["event_type"] == "FAILURE")
    assert failure["reason_code"] == "PHASE_G_POST_CLICK_RECONCILIATION_FAILED"
    assert failure["failed_stage"] == "SAME_ATTEMPT_RECONCILIATION"
    assert failure["attempt_id"] == identity.d2_run_id
    assert failure["exception_type"] == "RuntimeError"
    assert failure["source_file"] == "tests/validation/test_phase_g_diagnostics.py"
    assert len(str(failure["traceback_digest"])) == 64
    assert private_prompt not in json.dumps(events)
    assert provider_actions == []


@pytest.mark.asyncio
async def test_preclick_diagnostic_stops_before_journal_credit_or_provider_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = request_for(tmp_path)
    context = ProductionExecutionContext(
        environment="PROD",
        provider_id="PRV_0001",
        provider_account_id="PVA_0001",
        provider_project_id=base.provider_project_id,
        auth_profile="GFLOW_PROD_AUTH",
        scene_id="SCN_000001",
        generation_profile_id="GPR_0001",
        scene_prompt_id="SPV_000002",
        attempt_id="D2_replacement-0001",
        credit_budget_context=CreditBudgetContext(provider_remaining_free=Decimal("50")),
        production_job_id="PRD_000002",
        scene_job_id="SJB_000002",
        legacy_replacement_authorization_id="LRA_" + "A" * 24,
        legacy_replacement_parent_attempt_id="D2_parent-00000001",
        legacy_replacement_quote_credits=Decimal("10"),
        legacy_replacement_quote_input_digest="d" * 64,
        legacy_replacement_quote_source_version="PCT_000001:verified:source-hash",
    )
    request = PhaseGExecutionRequest.from_presubmit(
        base, context.attempt_id, execution_context=context
    )
    calls: list[str] = []

    class ReadOnlyPort:
        async def bind_request(self, _request: PhaseGExecutionRequest) -> None:
            calls.append("bind_request")

        async def snapshot_settings(self) -> dict[str, object]:
            calls.append("snapshot_settings")
            return {
                "model_id": request.model_id,
                "generation_mode": request.generation_mode,
                "aspect_ratio": request.aspect_ratio,
                "output_count": request.output_count,
                "confirm_before_generating": "Always",
            }

        async def apply_settings(self, _settings: object) -> None:
            raise AssertionError("diagnostic mode must not apply settings")

        async def fill_prompt(self, *_args: object) -> None:
            raise AssertionError("diagnostic mode must not fill the prompt")

        async def capture_submission_baseline(self) -> None:
            raise AssertionError("diagnostic mode must not capture a submit baseline")

        async def click_generate_once(self) -> None:
            raise AssertionError("diagnostic mode must not click Generate")

    runtime_root = tmp_path / "runtime"
    ledger = D2DailyCreditLedger(runtime_root / "production")
    journal = AgentSettingsRecoveryJournal(runtime_root, "PVA_0001", request.provider_project_id)
    recorder = _recorder(runtime_root)
    progress = PhaseGExecutionProgress(recorder=recorder, strict=True)

    def validate_consumed(_request: PhaseGExecutionRequest) -> dict[str, object]:
        calls.append("validate_consumed_authorization")
        return {"d2_run_id": request.d2_run_id, "generate_action_count": 0}

    monkeypatch.setattr(ledger, "validate_consumed_legacy_replacement", validate_consumed)
    result = await GoogleFlowAgentOnlyGenerationExecutor(
        cast(FlowGenerationPort, ReadOnlyPort()), ledger, journal
    ).diagnose_preclick(request, progress=progress)

    assert result.attempt_id == request.d2_run_id
    assert result.last_completed_stage == "SETTINGS_SNAPSHOT"
    assert result.stopped_before_stage == "JOURNAL_BEGIN"
    assert calls == [
        "validate_consumed_authorization",
        "bind_request",
        "snapshot_settings",
    ]
    assert ledger.attempts() == ()
    assert not journal.path.exists()
    assert not (runtime_root / "production-credit" / "shared-ledger.json").exists()
    assert all(
        json.loads(line)["event_type"] != "FAILURE"
        for line in recorder.path.read_text().splitlines()
    )


@pytest.mark.asyncio
async def test_preclick_diagnostic_reuses_consumed_prepared_attempt_without_lineage_mutation(
    tmp_path: Path,
) -> None:
    ledger, state = _approved_ledger(tmp_path)
    parent = cast(PhaseGExecutionRequest, state["parent"])
    authorization = cast(dict[str, object], state["authorization"])
    request = _replacement_request(
        tmp_path,
        parent,
        "D2_9c13df47d64c68de30cd036bcd1fec09",
        authorization=authorization,
    )
    consume_legacy_replacement_authorization(
        ledger,
        str(authorization["authorization_id"]),
        request,
        parent_quarantined=True,
        current_quote=cast(ReplacementQuote, state["quote"]),
    )
    attempts_before = ledger.attempts()
    ledger_before = ledger._read()  # noqa: SLF001 - verify the durable authorization is unchanged
    runtime_root = tmp_path / "runtime"
    journal = AgentSettingsRecoveryJournal(
        request.presubmit_request.runtime_state_root,
        request.provider_account_id,
        request.provider_project_id,
    )

    class ReadOnlyPort:
        async def bind_request(self, _request: PhaseGExecutionRequest) -> None:
            return None

        async def snapshot_settings(self) -> dict[str, object]:
            return {
                "model_id": request.model_id,
                "generation_mode": request.generation_mode,
                "aspect_ratio": request.aspect_ratio,
                "output_count": request.output_count,
                "confirm_before_generating": "Always",
            }

        async def apply_settings(self, _settings: object) -> None:
            raise AssertionError("diagnostic mode must not apply settings")

        async def fill_prompt(self, *_args: object) -> None:
            raise AssertionError("diagnostic mode must not fill the prompt")

        async def capture_submission_baseline(self) -> None:
            raise AssertionError("diagnostic mode must not capture a submit baseline")

        async def click_generate_once(self) -> None:
            raise AssertionError("diagnostic mode must not click Generate")

    recorder = PhaseGDiagnosticRecorder(
        runtime_root,
        attempt_id=request.d2_run_id,
        authorization_id=str(authorization["authorization_id"]),
        production_job_id="PRD_000002",
        scene_job_id="SJB_000002",
    )
    result = await GoogleFlowAgentOnlyGenerationExecutor(
        cast(FlowGenerationPort, ReadOnlyPort()), ledger, journal
    ).diagnose_preclick(
        request,
        progress=PhaseGExecutionProgress(recorder=recorder, strict=True),
    )

    auth_records = cast(
        list[dict[str, object]], ledger._read()["legacy_replacement_authorizations"]
    )  # noqa: SLF001
    saved_auth = auth_records[0]
    replacement = next(row for row in ledger.attempts() if row["d2_run_id"] == request.d2_run_id)
    assert result.attempt_id == request.d2_run_id
    assert result.stopped_before_stage == "JOURNAL_BEGIN"
    assert ledger.attempts() == attempts_before
    assert ledger._read() == ledger_before  # noqa: SLF001
    assert saved_auth["authorization_status"] == "CONSUMED"
    assert saved_auth["replacement_attempt_id"] == request.d2_run_id
    assert replacement["submission_state"] == "PREPARED"
    assert replacement["generate_action_count"] == 0
    assert replacement["approval_action_count"] == 0
    assert not journal.path.exists()
    assert not (runtime_root / "production-credit" / "shared-ledger.json").exists()


@pytest.mark.asyncio
async def test_optional_stage_tracking_preserves_normal_successful_phase_g_path(
    tmp_path: Path,
) -> None:
    port = FakeGenerationPort()
    request, ledger, journal, executor = make_executor(tmp_path, port)
    recorder = PhaseGDiagnosticRecorder(
        tmp_path / "runtime",
        attempt_id=request.d2_run_id,
        authorization_id="LRA_" + "B" * 24,
        production_job_id="PRD_000002",
        scene_job_id="SJB_000002",
    )
    progress = PhaseGExecutionProgress(recorder=recorder, strict=True)

    result = await executor.execute(request, progress=progress)
    events = [json.loads(line) for line in recorder.path.read_text(encoding="utf-8").splitlines()]
    stages_started = [event["stage"] for event in events if event["event_type"] == "STAGE_STARTED"]

    assert result.submission_state == "GENERATION_SUCCEEDED"
    assert port.calls.count("generate") == 1
    assert stages_started[-2:] == ["GENERATE_CHECKPOINT", "GENERATE_CLICK"]


def test_operator_cli_binds_diagnostic_mode_to_the_exact_existing_lineage(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = [
        "run_legacy_replacement.py",
        "--scene-id",
        "SCN_000001",
        "--profile-id",
        "GPR_0002",
        "--provider-account-id",
        "PVA_0002",
        "--provider-project-id",
        "aafe9282-071f-4333-baa4-92b9f76a9c27",
        "--expected-email",
        "singleava508@gmail.com",
        "--parent-attempt-id",
        "D2_13c62112d9881710019049d95cb6da57",
        "--diagnose-preclick",
        "--authorization-id",
        "LRA_18A2C0EE3248A7C93E6D1991",
        "--replacement-attempt-id",
        "D2_9c13df47d64c68de30cd036bcd1fec09",
        "--replacement-production-job-id",
        "PRD_000002",
        "--replacement-scene-job-id",
        "SJB_000002",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    calls: list[dict[str, object]] = []

    def run_once_spy(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"status": "PRECLICK_DIAGNOSTIC_COMPLETE"}

    monkeypatch.setattr(run_legacy_replacement, "run_once", run_once_spy)

    assert run_legacy_replacement.main() == 0
    assert calls == [
        {
            "scene_id": "SCN_000001",
            "profile_id": "GPR_0002",
            "account_id": "PVA_0002",
            "project_id": "aafe9282-071f-4333-baa4-92b9f76a9c27",
            "expected_email": "singleava508@gmail.com",
            "parent_attempt_id": "D2_13c62112d9881710019049d95cb6da57",
            "execute": False,
            "diagnose_preclick_mode": True,
            "authorization_id": "LRA_18A2C0EE3248A7C93E6D1991",
            "replacement_attempt_id": "D2_9c13df47d64c68de30cd036bcd1fec09",
            "replacement_production_job_id": "PRD_000002",
            "replacement_scene_job_id": "SJB_000002",
        }
    ]
    assert json.loads(capsys.readouterr().out) == {"status": "PRECLICK_DIAGNOSTIC_COMPLETE"}

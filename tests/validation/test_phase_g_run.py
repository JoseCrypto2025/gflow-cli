from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

import gflow_cli.v2_runtime.phase_g_run as phase_g_run
from gflow_cli.v2_runtime.phase_e import InMemorySceneCatalog, preflight_scene
from gflow_cli.v2_runtime.phase_g import (
    D2DailyCreditLedger,
    FlowConfirmationGate,
    FlowSubmissionBaseline,
    PhaseGAttemptEvidence,
    PhaseGError,
    PhaseGExecutionRequest,
)
from gflow_cli.v2_runtime.phase_g_artifacts import ValidatedVideo, sha256_file
from gflow_cli.v2_runtime.phase_g_run import (
    build_phase_g_request,
    finalize_remote_artifact,
    recover_and_validate_existing,
    recover_late_confirmation_same_attempt,
)
from gflow_cli.v2_runtime.production_execution import (
    CreditBudgetContext,
    ProductionExecutionContext,
)
from tests.validation.test_phase_e_prompt_preflight import _fixtures
from tests.validation.test_phase_g import request_for

_PROD_PROJECT = "aafe9282-071f-4333-baa4-92b9f76a9c27"


def _prompt_rows() -> list[dict[str, object]]:
    return [
        {
            "Scene_Prompt_ID": "SPV_000002",
            "Scene_ID": "SCN_000001",
            "Generation_Profile_ID": "GPR_0001",
            "Prompt_Status": "APPROVED",
            "Compiled_Prompt_Text": "A person enters the room.",
            "Compiled_Negative_Prompt": "",
            "Source_Context_SHA256": "0" * 64,
            "Prompt_SHA256": "0" * 64,
        }
    ]


def _d2_test_tables() -> dict[str, list[dict[str, object]]]:
    tables = _fixtures()
    tables["GENERATION_PROFILE_MASTER"][0]["Environment"] = "TEST"
    tables["PROVIDER_ACCOUNT_MASTER"][0]["Environment"] = "TEST"
    tables["PROVIDER_ACCOUNT_MASTER"][0]["Provider_Project_ID"] = (
        "34e60953-e534-46e9-ac65-993893e61065"
    )
    return tables


def test_d2_request_builder_fails_closed_on_nonmatching_live_prompt(tmp_path: Path) -> None:
    tables = _d2_test_tables()
    preflight = preflight_scene(InMemorySceneCatalog(tables), "SCN_000001", "GPR_0001", "TEST")
    prompt = _prompt_rows()[0]
    prompt["Compiled_Prompt_Text"] = preflight.compiled_prompt or ""
    prompt["Compiled_Negative_Prompt"] = preflight.compiled_negative_prompt or ""
    prompt["Source_Context_SHA256"] = preflight.source_context_sha256 or ""
    prompt["Prompt_SHA256"] = "0" * 64
    with pytest.raises(PhaseGError, match="hash differs"):
        build_phase_g_request(
            tables,
            [prompt],
            d2_run_id="D2_test-run-0001",
            runtime_state_root=tmp_path / "state",
            temp_root=tmp_path / "temp",
        )


def test_d2_request_builder_requires_exactly_one_approved_prompt(tmp_path: Path) -> None:
    prompt = _prompt_rows()[0]
    prompt["Prompt_Status"] = "SUPERSEDED"
    with pytest.raises(PhaseGError, match="exactly one current APPROVED"):
        build_phase_g_request(
            _d2_test_tables(),
            [prompt],
            d2_run_id="D2_test-run-0001",
            runtime_state_root=tmp_path / "state",
            temp_root=tmp_path / "temp",
        )


def test_prod_request_builder_binds_context_to_live_master_identity(tmp_path: Path) -> None:
    tables = _d2_test_tables()
    prod_account = deepcopy(tables["PROVIDER_ACCOUNT_MASTER"][0])
    prod_account.update(
        Provider_Account_ID="PVA_0002",
        Environment="PROD",
        Auth_Profile_Key="GFLOW_PROD_AUTH",
        Provider_Project_ID=_PROD_PROJECT,
    )
    tables["PROVIDER_ACCOUNT_MASTER"].append(prod_account)
    prod_profile = deepcopy(tables["GENERATION_PROFILE_MASTER"][0])
    prod_profile.update(
        Generation_Profile_ID="GPR_0002",
        Provider_Account_ID="PVA_0002",
        Environment="PROD",
        Purpose="production",
    )
    tables["GENERATION_PROFILE_MASTER"].append(prod_profile)
    preflight = preflight_scene(InMemorySceneCatalog(tables), "SCN_000001", "GPR_0002", "PROD")
    prompt = _prompt_rows()[0]
    prompt.update(
        Scene_Prompt_ID="SPV_000006",
        Generation_Profile_ID="GPR_0002",
        Compiled_Prompt_Text=preflight.compiled_prompt or "",
        Compiled_Negative_Prompt=preflight.compiled_negative_prompt or "",
        Source_Context_SHA256=preflight.source_context_sha256 or "",
        Prompt_SHA256=preflight.prompt_sha256 or "",
    )
    context = ProductionExecutionContext(
        "PROD",
        "PRV_0001",
        "PVA_0002",
        _PROD_PROJECT,
        "GFLOW_PROD_AUTH",
        "SCN_000001",
        "GPR_0002",
        "SPV_000006",
        "D6_test-run-0001",
        CreditBudgetContext(provider_remaining_free=50),
        prerequisite_d2_run_id="D2_test-run-0001",
        production_job_id="PRD_000001",
        scene_job_id="SJB_000001",
    )

    request = build_phase_g_request(
        tables,
        [prompt],
        d2_run_id=context.attempt_id,
        runtime_state_root=tmp_path / "state",
        temp_root=tmp_path / "temp",
        execution_context=context,
    )

    assert request.provider_account_id == "PVA_0002"
    assert request.provider_project_id == _PROD_PROJECT
    assert request.execution_context == context


def test_prod_request_builder_rejects_test_account_context(tmp_path: Path) -> None:
    tables = _d2_test_tables()
    context = ProductionExecutionContext(
        "PROD",
        "PRV_0001",
        "PVA_0001",
        "34e60953-e534-46e9-ac65-993893e61065",
        "GFLOW_PROD_AUTH",
        "SCN_000001",
        "GPR_0001",
        "SPV_000002",
        "D2_test-run-0002",
        CreditBudgetContext(provider_remaining_free=50),
        production_job_id="PRD_000001",
        scene_job_id="SJB_000001",
    )
    with pytest.raises(PhaseGError):
        build_phase_g_request(
            tables,
            _prompt_rows(),
            d2_run_id=context.attempt_id,
            runtime_state_root=tmp_path / "state",
            temp_root=tmp_path / "temp",
            execution_context=context,
        )


def test_prod_request_builder_rejects_date_stamped_project(tmp_path: Path) -> None:
    tables = _d2_test_tables()
    prod_account = deepcopy(tables["PROVIDER_ACCOUNT_MASTER"][0])
    prod_account.update(
        Provider_Account_ID="PVA_0002",
        Environment="PROD",
        Auth_Profile_Key="GFLOW_PROD_AUTH",
        Provider_Project_ID="date-stamped-project",
    )
    tables["PROVIDER_ACCOUNT_MASTER"].append(prod_account)
    prod_profile = deepcopy(tables["GENERATION_PROFILE_MASTER"][0])
    prod_profile.update(
        Generation_Profile_ID="GPR_0002",
        Provider_Account_ID="PVA_0002",
        Environment="PROD",
        Purpose="production",
    )
    tables["GENERATION_PROFILE_MASTER"].append(prod_profile)
    context = ProductionExecutionContext(
        "PROD",
        "PRV_0001",
        "PVA_0002",
        "date-stamped-project",
        "GFLOW_PROD_AUTH",
        "SCN_000001",
        "GPR_0002",
        "SPV_000006",
        "D6_test-run-0002",
        CreditBudgetContext(provider_remaining_free=50),
        production_job_id="PRD_000001",
        scene_job_id="SJB_000001",
    )
    preflight = preflight_scene(InMemorySceneCatalog(tables), "SCN_000001", "GPR_0002", "PROD")
    prompt = _prompt_rows()[0]
    prompt.update(
        Scene_Prompt_ID="SPV_000006",
        Generation_Profile_ID="GPR_0002",
        Compiled_Prompt_Text=preflight.compiled_prompt or "",
        Compiled_Negative_Prompt=preflight.compiled_negative_prompt or "",
        Source_Context_SHA256=preflight.source_context_sha256 or "",
        Prompt_SHA256=preflight.prompt_sha256 or "",
    )
    with pytest.raises(PhaseGError, match="canonical pinned project"):
        build_phase_g_request(
            tables,
            [prompt],
            d2_run_id=context.attempt_id,
            runtime_state_root=tmp_path / "state",
            temp_root=tmp_path / "temp",
            execution_context=context,
        )


@pytest.mark.asyncio
async def test_recovery_resumes_after_generation_success_without_resubmitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-run-0001")
    ledger = D2DailyCreditLedger(request.presubmit_request.runtime_state_root)
    ledger.create_attempt(request)
    ledger.capture_baseline(
        request.d2_run_id,
        FlowSubmissionBaseline(
            request.provider_project_id,
            frozenset(),
            frozenset(),
            0,
            frozenset(),
            "turn-before",
        ),
    )
    ledger.mark_generate_action(request.d2_run_id)
    ledger.observe_confirmation(
        request.d2_run_id,
        confirmation_id="confirmation-new",
        video_count=1,
        credit_cost=10,
        visible_balance=None,
    )
    ledger.approve_gate(
        request.d2_run_id,
        confirmation_id="confirmation-new",
        video_count=1,
        credit_cost=10,
    )
    ledger.record_state(
        request.d2_run_id,
        submission_state="GENERATION_SUCCEEDED",
        provider_media_id="provider-media-exact",
        provider_project_id=request.provider_project_id,
    )
    local_file = request.presubmit_request.temp_root / "recovered" / "exact.mp4"
    local_file.parent.mkdir(parents=True)
    local_file.write_bytes(b"already-generated-media")
    video = ValidatedVideo(
        local_file,
        local_file.stat().st_size,
        sha256_file(local_file),
        8.0,
        720,
        1280,
        "9:16",
        False,
        100,
    )

    class FakePort:
        @classmethod
        async def open(cls, *, expected_email: str) -> FakePort:
            assert expected_email == "flow-test@example.com"
            return cls()

        async def bind_request(self, bound_request: object) -> None:
            assert bound_request is request

        async def download_exact_media(self, temp_root: Path, media_id: str) -> object:
            assert media_id == "provider-media-exact"
            return SimpleNamespace(path=local_file)

        async def close(self) -> None:
            return None

    monkeypatch.setattr(phase_g_run, "GoogleFlowAgentOnlyGenerationPort", FakePort)
    monkeypatch.setattr(phase_g_run, "validate_video", lambda *_args, **_kwargs: video)

    recovered = await recover_and_validate_existing(request, expected_email="flow-test@example.com")

    assert recovered.result.provider_media_id == "provider-media-exact"
    assert recovered.result.submission_state == "GENERATION_SUCCEEDED"
    assert ledger.attempts()[0]["generate_action_count"] == 1
    assert ledger.attempts()[0]["local_artifact_sha256"] == video.sha256


@pytest.mark.asyncio
async def test_late_confirmation_operator_approves_without_generate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-run-0001")
    ledger = D2DailyCreditLedger(request.presubmit_request.runtime_state_root)
    ledger.create_attempt(request)
    ledger.capture_baseline(
        request.d2_run_id,
        FlowSubmissionBaseline(
            request.provider_project_id, frozenset(), frozenset(), 0, frozenset(), "0:"
        ),
    )
    ledger.mark_generate_action(request.d2_run_id)
    ledger.record_state(request.d2_run_id, submission_state="UNKNOWN_SUBMISSION_STATE")
    expected_sha = sha256_file(ledger.path)
    gate = FlowConfirmationGate("a" * 64, request.provider_project_id, 1, 10)
    calls: list[str] = []

    class FakePort:
        @classmethod
        async def open(cls, *, expected_email: str) -> FakePort:
            assert expected_email == "flow-test@example.com"
            return cls()

        async def bind_request(self, bound_request: PhaseGExecutionRequest) -> None:
            assert bound_request is request

        async def read_current_confirmation(
            self, baseline: FlowSubmissionBaseline
        ) -> FlowConfirmationGate:
            assert baseline.project_id == request.provider_project_id
            calls.append("read_gate")
            return gate

        async def approve_confirmation(self, confirmation_id: str) -> None:
            assert confirmation_id == gate.confirmation_id
            calls.append("approve")

        async def observe_attempt(
            self,
            baseline: FlowSubmissionBaseline,
            confirmation_id: str | None,
            *,
            observation_deadline: float,
        ) -> PhaseGAttemptEvidence:
            assert confirmation_id == gate.confirmation_id
            assert observation_deadline > 0
            calls.append("observe")
            return PhaseGAttemptEvidence(
                request.d2_run_id,
                "GENERATION_PENDING",
                provider_execution_id="exec-1",
                provider_project_id=request.provider_project_id,
                attributed_confirmation_id=confirmation_id,
            )

        async def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(phase_g_run, "GoogleFlowAgentOnlyGenerationPort", FakePort)
    result = await recover_late_confirmation_same_attempt(
        request,
        expected_email="flow-test@example.com",
        expected_state="UNKNOWN_SUBMISSION_STATE",
        expected_ledger_sha256=expected_sha,
        expected_confirmation_id=gate.confirmation_id,
        expected_video_count=1,
        expected_credit_cost=10,
    )

    attempt = ledger.attempts()[0]
    assert result.submission_state == "GENERATION_PENDING"
    assert attempt["generate_action_count"] == 1
    assert attempt["approval_action_count"] == 1
    assert attempt["submission_state"] == "GENERATION_PENDING"
    assert calls == ["read_gate", "approve", "observe", "close"]


@pytest.mark.asyncio
async def test_late_recovery_wrong_ledger_hash_does_not_open_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-run-0001")
    called = False

    class ForbiddenPort:
        @classmethod
        async def open(cls, **_kwargs: object) -> ForbiddenPort:
            nonlocal called
            called = True
            raise AssertionError("browser must not open on a ledger mismatch")

    monkeypatch.setattr(phase_g_run, "GoogleFlowAgentOnlyGenerationPort", ForbiddenPort)
    with pytest.raises(PhaseGError) as changed:
        await recover_late_confirmation_same_attempt(
            request,
            expected_email="flow-test@example.com",
            expected_state="UNKNOWN_SUBMISSION_STATE",
            expected_ledger_sha256="0" * 64,
            expected_confirmation_id="a" * 64,
            expected_video_count=1,
            expected_credit_cost=10,
        )
    assert changed.value.code == "PHASE_G_LEDGER_CHANGED"
    assert called is False


def test_drive_finalizer_requires_remote_hash_match_before_cleanup(tmp_path: Path) -> None:
    request = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-run-0001")
    ledger = D2DailyCreditLedger(request.presubmit_request.runtime_state_root)
    ledger.create_attempt(request)
    ledger.capture_baseline(
        request.d2_run_id,
        FlowSubmissionBaseline(
            request.provider_project_id,
            frozenset(),
            frozenset(),
            0,
            frozenset(),
            "turn-before",
        ),
    )
    ledger.mark_generate_action(request.d2_run_id)
    ledger.observe_confirmation(
        request.d2_run_id,
        confirmation_id="confirmation-new",
        video_count=1,
        credit_cost=10,
        visible_balance=None,
    )
    ledger.approve_gate(
        request.d2_run_id,
        confirmation_id="confirmation-new",
        video_count=1,
        credit_cost=10,
    )
    ledger.record_state(
        request.d2_run_id,
        submission_state="GENERATION_SUCCEEDED",
        provider_media_id="provider-media-exact",
        provider_project_id=request.provider_project_id,
    )
    local_file = request.presubmit_request.temp_root / request.d2_run_id / "clip.mp4"
    local_file.parent.mkdir(parents=True)
    local_file.write_bytes(b"validated-local-mp4")
    ledger.record_local_artifact_validated(
        request.d2_run_id,
        path=local_file,
        size_bytes=local_file.stat().st_size,
        sha256=sha256_file(local_file),
    )
    remote_download = tmp_path / "drive-redownload.mp4"
    remote_download.write_bytes(b"different-local-mp4")

    with pytest.raises(PhaseGError) as mismatch:
        finalize_remote_artifact(
            request.presubmit_request.runtime_state_root,
            request.d2_run_id,
            remote_download=remote_download,
            remote_file_id="drive-file-1",
            remote_filename="d2-run.mp4",
        )
    assert mismatch.value.code == "PHASE_G_REMOTE_VERIFICATION_FAILED"
    assert local_file.is_file()
    assert ledger.attempts()[0]["submission_state"] == "GENERATION_SUCCEEDED"

    remote_download.write_bytes(local_file.read_bytes())
    completed = finalize_remote_artifact(
        request.presubmit_request.runtime_state_root,
        request.d2_run_id,
        remote_download=remote_download,
        remote_file_id="drive-file-1",
        remote_filename="d2-run.mp4",
    )
    assert completed["submission_state"] == "D2_SUCCEEDED"
    assert completed["remote_verified"] is True
    assert not local_file.exists()

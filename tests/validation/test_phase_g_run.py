from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import gflow_cli.v2_runtime.phase_g_run as phase_g_run
from gflow_cli.v2_runtime.phase_e import InMemorySceneCatalog, preflight_scene
from gflow_cli.v2_runtime.phase_g import (
    D2DailyCreditLedger,
    FlowSubmissionBaseline,
    PhaseGError,
    PhaseGExecutionRequest,
)
from gflow_cli.v2_runtime.phase_g_artifacts import ValidatedVideo, sha256_file
from gflow_cli.v2_runtime.phase_g_run import (
    build_phase_g_request,
    finalize_remote_artifact,
    recover_and_validate_existing,
)
from tests.validation.test_phase_e_prompt_preflight import _fixtures
from tests.validation.test_phase_g import request_for


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


def test_d2_request_builder_fails_closed_on_nonmatching_live_prompt(tmp_path: Path) -> None:
    tables = _fixtures()
    preflight = preflight_scene(InMemorySceneCatalog(tables), "SCN_000001", "GPR_0001", "DEV")
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
            _fixtures(),
            [prompt],
            d2_run_id="D2_test-run-0001",
            runtime_state_root=tmp_path / "state",
            temp_root=tmp_path / "temp",
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

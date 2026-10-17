"""Bound D2 orchestration from a fresh read-only Master snapshot to one clip."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .agent_recovery import AgentSettingsRecoveryJournal
from .errors import AssetVerificationError, FlowPresubmitError
from .google_flow import (
    FlowPresubmitRequest,
    PersistedPromptVersion,
)
from .live_flow import prepare_live_flow
from .phase_e import InMemorySceneCatalog, Row, preflight_scene
from .phase_g import (
    D2DailyCreditLedger,
    GoogleFlowAgentOnlyGenerationExecutor,
    PhaseGError,
    PhaseGExecutionRequest,
    PhaseGExecutionResult,
)
from .phase_g_artifacts import (
    ValidatedVideo,
    validate_video,
)
from .phase_g_live_flow import GoogleFlowAgentOnlyGenerationPort

_SCENE_ID = "SCN_000001"
_PROFILE_ID = "GPR_0001"
_ACCOUNT_ID = "PVA_0001"
_PROJECT_ID = "34e60953-e534-46e9-ac65-993893e61065"
_PROJECT_NAME = "AI Video Automation V2 - TEST"
_AUTH_PROFILE = "GFLOW_V2_TEST_AUTH"
_MODEL_DISPLAY_NAMES = {"MDL_0001": "Veo 3.1 - Lite"}


@dataclass(frozen=True, slots=True)
class PhaseGD2Run:
    """One successfully attributed and locally validated D2 output."""

    request: PhaseGExecutionRequest
    result: PhaseGExecutionResult
    local_file: Path
    video: ValidatedVideo


def build_phase_g_request(
    tables: Mapping[str, list[Row]],
    prompt_versions: Sequence[Row],
    *,
    d2_run_id: str,
    runtime_state_root: Path,
    temp_root: Path,
) -> PhaseGExecutionRequest:
    """Re-run Phase E from live-read rows and require one exact APPROVED row."""
    catalog = InMemorySceneCatalog(tables)
    preflight = preflight_scene(catalog, _SCENE_ID, _PROFILE_ID, "development")
    if preflight.status != "PREFLIGHT_PASS" or not preflight.ready_for_provider_adapter:
        raise PhaseGError("PHASE_E_PREFLIGHT_REQUIRED", "fresh Phase E did not pass")
    current = [
        row
        for row in prompt_versions
        if row.get("Scene_ID") == _SCENE_ID and row.get("Prompt_Status") == "APPROVED"
    ]
    if len(current) != 1:
        raise PhaseGError(
            "PHASE_G_PROMPT_NOT_APPROVED", "exactly one current APPROVED Scene prompt is required"
        )
    row = current[0]
    prompt = PersistedPromptVersion(
        str(row.get("Scene_Prompt_ID", "")),
        str(row.get("Scene_ID", "")),
        str(row.get("Generation_Profile_ID", "")),
        str(row.get("Prompt_Status", "")),
        str(row.get("Compiled_Prompt_Text", "")),
        str(row.get("Compiled_Negative_Prompt", "") or ""),
        str(row.get("Source_Context_SHA256", "")),
        str(row.get("Prompt_SHA256", "")),
    )
    provider_id = str(preflight.resolved.get("provider_id", ""))
    model_id = str(preflight.resolved.get("model_id", ""))
    accounts = [
        row
        for row in tables.get("PROVIDER_ACCOUNT_MASTER", [])
        if row.get("Provider_Account_ID") == _ACCOUNT_ID and row.get("Status") == "ACTIVE"
    ]
    if len(accounts) != 1:
        raise PhaseGError(
            "PHASE_G_ACCOUNT_INVALID", "one active pinned TEST provider account is required"
        )
    sheet_project_id = accounts[0].get("Provider_Project_ID")
    if sheet_project_id and sheet_project_id != _PROJECT_ID:
        raise PhaseGError(
            "PHASE_G_PROJECT_MISMATCH", "pinned TEST project differs from Provider Account Master"
        )
    request = FlowPresubmitRequest(
        preflight,
        prompt,
        provider_id,
        _ACCOUNT_ID,
        model_id,
        "TEXT_TO_VIDEO",
        _PROJECT_ID,
        runtime_state_root,
        temp_root,
        (),
    )
    return PhaseGExecutionRequest.from_presubmit(request, d2_run_id)


async def run_fresh_presubmit(request: PhaseGExecutionRequest, *, expected_email: str) -> None:
    """Exercise F5 on the pinned TEST profile before the separate Phase G port."""
    result = await prepare_live_flow(
        request.presubmit_request,
        auth_profile_key=_AUTH_PROFILE,
        project_name=_PROJECT_NAME,
        model_display_names=_MODEL_DISPLAY_NAMES,
        expected_email=expected_email,
        headless=False,
    )
    if result.state != "FLOW_PRESUBMIT_READY":
        raise FlowPresubmitError("FLOW_PRESUBMIT_NOT_READY", "fresh F5 readback did not pass")


async def execute_and_validate(
    request: PhaseGExecutionRequest, *, expected_email: str
) -> PhaseGD2Run:
    """Perform one request, recover only its media ID, and fully validate the MP4."""
    ledger = D2DailyCreditLedger(request.presubmit_request.runtime_state_root)
    journal = AgentSettingsRecoveryJournal(
        request.presubmit_request.runtime_state_root,
        request.provider_account_id,
        request.provider_project_id,
    )
    port = await GoogleFlowAgentOnlyGenerationPort.open(expected_email=expected_email)
    try:
        result = await GoogleFlowAgentOnlyGenerationExecutor(port, ledger, journal).execute(request)
        if result.submission_state != "GENERATION_SUCCEEDED" or not result.provider_media_id:
            raise PhaseGError(
                "PHASE_G_OUTPUT_NOT_READY", "attempt has not produced one attributable media ID"
            )
        try:
            recovered = await port.download_exact_media(
                request.presubmit_request.temp_root, result.provider_media_id
            )
        except Exception as exc:
            _record_artifact_failure(ledger, request, result, "PERSISTENCE_FAILED")
            raise PhaseGError(
                "PHASE_G_MEDIA_DOWNLOAD_FAILED", "exact attributed media download failed"
            ) from exc
        local_file = recovered.path
        try:
            video = validate_video(
                local_file,
                expected_duration_sec=request.duration_sec,
                expected_aspect_ratio=request.aspect_ratio,
            )
        except AssetVerificationError as exc:
            _record_artifact_failure(ledger, request, result, "MEDIA_VALIDATION_FAILED")
            raise PhaseGError(
                "PHASE_G_MEDIA_VALIDATION_FAILED", "downloaded output failed its media contract"
            ) from exc
        ledger.record_local_artifact_validated(
            request.d2_run_id,
            path=local_file,
            size_bytes=video.size_bytes,
            sha256=video.sha256,
        )
        return PhaseGD2Run(request, result, local_file, video)
    finally:
        await port.close()


async def recover_and_validate_existing(
    request: PhaseGExecutionRequest, *, expected_email: str
) -> PhaseGD2Run:
    """Recover and validate the exact media ID already recorded for this run."""
    ledger = D2DailyCreditLedger(request.presubmit_request.runtime_state_root)
    attempt = next(
        (row for row in ledger.attempts() if row.get("d2_run_id") == request.d2_run_id),
        None,
    )
    if attempt is None or attempt.get("submission_state") not in {
        "GENERATION_SUCCEEDED",
        "PERSISTENCE_FAILED",
        "MEDIA_VALIDATION_FAILED",
    }:
        raise PhaseGError(
            "PHASE_G_ARTIFACT_RECOVERY_REQUIRED", "no recoverable generated D2 attempt exists"
        )
    if (
        attempt.get("scene_id") != request.scene_id
        or attempt.get("scene_prompt_id") != request.scene_prompt_id
        or attempt.get("provider_account_id") != request.provider_account_id
        or attempt.get("provider_project_id") != request.provider_project_id
        or attempt.get("prompt_sha256") != request.prompt_sha256
    ):
        raise PhaseGError(
            "PHASE_G_RECOVERY_IDENTITY_MISMATCH", "stored attempt differs from recovery request"
        )
    media_id = attempt.get("provider_media_id")
    if not isinstance(media_id, str) or not media_id:
        raise PhaseGError(
            "PHASE_G_ARTIFACT_RECOVERY_REQUIRED", "stored attempt has no exact provider media ID"
        )
    port = await GoogleFlowAgentOnlyGenerationPort.open(expected_email=expected_email)
    try:
        await port.bind_request(request)
        recovered = await port.download_exact_media(
            request.presubmit_request.temp_root / f"recovery-{uuid4().hex}", media_id
        )
        video = validate_video(
            recovered.path,
            expected_duration_sec=request.duration_sec,
            expected_aspect_ratio=request.aspect_ratio,
        )
        recovered_attempt = (
            attempt
            if attempt.get("submission_state") == "GENERATION_SUCCEEDED"
            else ledger.record_artifact_recovered(request.d2_run_id)
        )
        ledger.record_local_artifact_validated(
            request.d2_run_id,
            path=recovered.path,
            size_bytes=video.size_bytes,
            sha256=video.sha256,
        )
        result = PhaseGExecutionResult(
            request.d2_run_id,
            "GENERATION_SUCCEEDED",
            str(recovered_attempt.get("confirmation_id") or ""),
            str(recovered_attempt.get("provider_execution_id") or "") or None,
            media_id,
            request.provider_project_id,
        )
        return PhaseGD2Run(request, result, recovered.path, video)
    finally:
        await port.close()


def finalize_remote_artifact(
    runtime_state_root: Path,
    d2_run_id: str,
    *,
    remote_download: Path,
    remote_file_id: str,
    remote_filename: str,
) -> Mapping[str, object]:
    """Finalize a Drive connector transfer using the ledger's exact local clip."""
    ledger = D2DailyCreditLedger(runtime_state_root)
    attempt = next((row for row in ledger.attempts() if row.get("d2_run_id") == d2_run_id), None)
    if attempt is None or attempt.get("submission_state") != "GENERATION_SUCCEEDED":
        raise PhaseGError(
            "PHASE_G_ARTIFACT_RECOVERY_REQUIRED", "no validated generated clip is ready to persist"
        )
    local_path_value = attempt.get("local_artifact_path")
    temp_root_value = attempt.get("temp_root")
    if not isinstance(local_path_value, str) or not isinstance(temp_root_value, str):
        raise PhaseGError(
            "PHASE_G_ARTIFACT_RECOVERY_REQUIRED", "ledger has no validated local artifact path"
        )
    local_candidate = Path(local_path_value)
    if local_candidate.is_symlink() or not local_candidate.is_file():
        raise PhaseGError("PHASE_G_LOCAL_ARTIFACT_INVALID", "ledger artifact is missing or unsafe")
    local_path = local_candidate.resolve()
    if not local_path.is_relative_to(Path(temp_root_value).resolve()):
        raise PhaseGError(
            "PHASE_G_LOCAL_ARTIFACT_INVALID", "ledger artifact path is outside its D2 temp root"
        )
    completed = ledger.mark_artifact_verified(
        d2_run_id,
        remote_download=remote_download,
        remote_file_id=remote_file_id,
        remote_filename=remote_filename,
    )
    local_path.unlink()
    return completed


def _record_artifact_failure(
    ledger: D2DailyCreditLedger,
    request: PhaseGExecutionRequest,
    result: PhaseGExecutionResult,
    failure: str,
) -> None:
    ledger.record_state(
        request.d2_run_id,
        submission_state=failure,
        terminal_state=failure,
        provider_execution_id=result.provider_execution_id,
        provider_media_id=result.provider_media_id,
        provider_project_id=result.provider_project_id,
    )

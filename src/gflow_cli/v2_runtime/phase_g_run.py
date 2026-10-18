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
from .production_execution import ProductionCreditGuard, ProductionExecutionContext

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
    execution_context: ProductionExecutionContext | None = None,
) -> PhaseGExecutionRequest:
    """Re-run Phase E and require one exact approval for the chosen environment."""
    scene_id = execution_context.scene_id if execution_context else _SCENE_ID
    profile_id = execution_context.generation_profile_id if execution_context else _PROFILE_ID
    environment = execution_context.environment if execution_context else "TEST"
    catalog = InMemorySceneCatalog(tables)
    preflight = preflight_scene(catalog, scene_id, profile_id, environment)
    if preflight.status != "PREFLIGHT_PASS" or not preflight.ready_for_provider_adapter:
        raise PhaseGError("PHASE_E_PREFLIGHT_REQUIRED", "fresh Phase E did not pass")
    current = [
        row
        for row in prompt_versions
        if row.get("Scene_ID") == scene_id
        and row.get("Generation_Profile_ID") == profile_id
        and row.get("Prompt_Status") == "APPROVED"
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
    account_id = execution_context.provider_account_id if execution_context else _ACCOUNT_ID
    accounts = [
        row
        for row in tables.get("PROVIDER_ACCOUNT_MASTER", [])
        if row.get("Provider_Account_ID") == account_id
        and row.get("Status") == "ACTIVE"
        and row.get("Environment") == environment
    ]
    if len(accounts) != 1:
        raise PhaseGError(
            "PHASE_G_ACCOUNT_INVALID",
            "one active provider account for the execution environment is required",
        )
    account = accounts[0]
    sheet_project_id = account.get("Provider_Project_ID")
    project_id = execution_context.provider_project_id if execution_context else _PROJECT_ID
    context_identity: dict[str, str] = {}
    if (
        not isinstance(sheet_project_id, str)
        or not sheet_project_id
        or sheet_project_id != project_id
    ):
        raise PhaseGError(
            "PHASE_G_PROJECT_MISMATCH", "pinned TEST project differs from Provider Account Master"
        )
    if execution_context is not None:
        if account.get("Provider_ID") != execution_context.provider_id:
            raise PhaseGError(
                "PHASE_G_IDENTITY_MISMATCH", "provider account belongs to another provider"
            )
        if account.get("Auth_Profile_Key") != execution_context.auth_profile:
            raise PhaseGError(
                "PHASE_G_IDENTITY_MISMATCH", "auth profile differs from Provider Account Master"
            )
        context_identity = {
            "environment": environment,
            "provider_id": str(account.get("Provider_ID", "")),
            "provider_account_id": str(account.get("Provider_Account_ID", "")),
            "provider_project_id": str(account.get("Provider_Project_ID", "")),
            "auth_profile": str(account.get("Auth_Profile_Key", "")),
            "scene_id": scene_id,
            "generation_profile_id": profile_id,
        }
    generation_profiles = [
        row
        for row in tables.get("GENERATION_PROFILE_MASTER", [])
        if row.get("Generation_Profile_ID") == profile_id
        and row.get("Environment") == environment
        and row.get("Status") == "ACTIVE"
    ]
    if execution_context is not None and (
        len(generation_profiles) != 1
        or generation_profiles[0].get("Provider_Account_ID") != account_id
        or generation_profiles[0].get("Model_ID") != model_id
    ):
        raise PhaseGError(
            "PHASE_G_IDENTITY_MISMATCH", "generation profile is not bound to account/model"
        )
    request = FlowPresubmitRequest(
        preflight,
        prompt,
        provider_id,
        account_id,
        model_id,
        "TEXT_TO_VIDEO",
        project_id,
        runtime_state_root,
        temp_root,
        (),
    )
    if execution_context is not None:
        execution_context.validate(
            {
                "environment": context_identity["environment"],
                "provider_id": context_identity["provider_id"],
                "provider_account_id": context_identity["provider_account_id"],
                "provider_project_id": context_identity["provider_project_id"],
                "auth_profile": context_identity["auth_profile"],
                "scene_id": scene_id,
                "generation_profile_id": profile_id,
                "scene_prompt_id": prompt.scene_prompt_id,
            }
        )
        if execution_context.attempt_id != d2_run_id:
            raise PhaseGError(
                "PHASE_G_EXECUTION_CONTEXT_MISMATCH", "attempt ID differs from context"
            )
        return PhaseGExecutionRequest.from_presubmit(
            request, d2_run_id, execution_context=execution_context
        )
    return PhaseGExecutionRequest.from_presubmit(request, d2_run_id)


async def run_fresh_presubmit(request: PhaseGExecutionRequest, *, expected_email: str) -> None:
    """Exercise F5 on the repository-owned profile selected by execution context."""
    context = request.execution_context
    profile_key = context.auth_profile if context else _AUTH_PROFILE
    project_name = (
        "AI Video Automation V2 - PROD"
        if context and context.environment == "PROD"
        else _PROJECT_NAME
    )
    result = await prepare_live_flow(
        request.presubmit_request,
        auth_profile_key=profile_key,
        project_name=project_name,
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
    context = request.execution_context
    ledger_root = request.presubmit_request.runtime_state_root
    if context is not None and context.environment == "PROD":
        # Keep D2 historical evidence separate; the shared guard below owns the
        # provider/day ceiling across all new TEST and PROD attempts.
        ledger_root = ledger_root / "production"
    ledger = D2DailyCreditLedger(ledger_root)
    credit_guard = (
        ProductionCreditGuard(request.presubmit_request.runtime_state_root) if context else None
    )
    journal = AgentSettingsRecoveryJournal(
        request.presubmit_request.runtime_state_root,
        request.provider_account_id,
        request.provider_project_id,
    )
    port = await _open_generation_port(request, expected_email=expected_email)
    try:
        result = await GoogleFlowAgentOnlyGenerationExecutor(
            port, ledger, journal, production_credit_guard=credit_guard
        ).execute(request)
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


async def _open_generation_port(
    request: PhaseGExecutionRequest, *, expected_email: str
) -> GoogleFlowAgentOnlyGenerationPort:
    context = request.execution_context
    if context is None:
        return await GoogleFlowAgentOnlyGenerationPort.open(expected_email=expected_email)
    project_name = {
        "TEST": _PROJECT_NAME,
        "PROD": "AI Video Automation V2 - PROD",
    }.get(context.environment)
    if project_name is None:
        raise PhaseGError("PHASE_G_ENVIRONMENT_UNSUPPORTED", "only TEST and PROD are supported")
    return await GoogleFlowAgentOnlyGenerationPort.open(
        expected_email=expected_email,
        auth_profile_key=context.auth_profile,
        provider_account_id=context.provider_account_id,
        provider_project_id=context.provider_project_id,
        project_name=project_name,
        model_display_names=_MODEL_DISPLAY_NAMES,
    )


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

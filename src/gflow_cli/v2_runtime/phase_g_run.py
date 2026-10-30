"""Bound D2 orchestration from a fresh read-only Master snapshot to one clip."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from gflow_cli.errors import WafRejectionError

from .agent_recovery import AgentSettingsRecoveryJournal
from .errors import AssetVerificationError, FlowPresubmitError
from .google_flow import (
    FlowPresubmitRequest,
    PersistedPromptVersion,
)
from .google_sheets import APPROVED_SPREADSHEET_ID, SOURCE_LIVE_GOOGLE_SHEETS
from .live_flow import prepare_live_flow
from .phase_e import InMemorySceneCatalog, Row, preflight_scene
from .phase_g import (
    D2DailyCreditLedger,
    D2RetryAuthorization,
    FlowSubmissionBaseline,
    GoogleFlowAgentOnlyGenerationExecutor,
    PhaseGError,
    PhaseGExecutionRequest,
    PhaseGExecutionResult,
    PhaseGPreclickDiagnosticResult,
)
from .phase_g_artifacts import (
    ValidatedVideo,
    sha256_file,
    validate_video,
)
from .phase_g_diagnostics import PhaseGExecutionProgress
from .phase_g_live_flow import GoogleFlowAgentOnlyGenerationPort
from .phase_g_prod_reconciliation import (
    ProdAttemptIdentity,
    ProdReconciliationEvidence,
    ProdReconciliationResult,
    classify_prod_reconciliation,
    evidence_audit_record,
    prod_evidence_from_attempt,
)
from .production_execution import ProductionCreditGuard, ProductionExecutionContext
from .timestamps import parse_utc_timestamp

_SCENE_ID = "SCN_000001"
_PROFILE_ID = "GPR_0001"
_ACCOUNT_ID = "PVA_0001"
_PROJECT_ID = "34e60953-e534-46e9-ac65-993893e61065"
_PROJECT_NAME = "AI Video Automation V2 - TEST"
_AUTH_PROFILE = "GFLOW_V2_TEST_AUTH"
_PROD_ACCOUNT_ID = "PVA_0002"
_PROD_PROJECT_ID = "aafe9282-071f-4333-baa4-92b9f76a9c27"
_PROD_PROJECT_NAME = "AI Video Automation V2 - PROD"
_PROD_AUTH_PROFILE = "GFLOW_PROD_AUTH"
_MODEL_DISPLAY_NAMES = {"MDL_0001": "Veo 3.1 - Lite"}
_UUID_RE = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)


def reconcile_prod_same_attempt(
    runtime_state_root: Path,
    context: ProductionExecutionContext,
    identity: ProdAttemptIdentity,
    *,
    credit_guard: ProductionCreditGuard,
    evidence: ProdReconciliationEvidence | None = None,
) -> ProdReconciliationResult:
    """Reconcile one existing PROD attempt using persisted/observed facts only.

    This function has no browser, Flow, Generate, approval, or attempt-creation path.
    """
    if (
        context.environment != "PROD"
        or context.attempt_id != identity.d2_run_id
        or context.production_job_id != identity.production_job_id
        or context.scene_job_id != identity.scene_job_id
        or context.provider_account_id != identity.provider_account_id
        or context.provider_project_id != identity.provider_project_id
        or context.scene_id != identity.scene_id
        or context.scene_prompt_id != identity.scene_prompt_id
        or context.generation_profile_id != identity.generation_profile_id
    ):
        raise PhaseGError(
            "PHASE_G_RECOVERY_IDENTITY_MISMATCH", "PROD context does not match attempt identity"
        )
    if not runtime_state_root.is_absolute() or runtime_state_root == Path(
        runtime_state_root.anchor
    ):
        raise PhaseGError("PHASE_G_LEDGER_PATH_INVALID", "runtime state root is invalid")
    # Operator callers pass the runtime base (for example /var/lib/gflow); the
    # PROD ledger itself is rooted under /production/phase-g.
    ledger = D2DailyCreditLedger(runtime_state_root / "production")
    matches = [row for row in ledger.attempts() if row.get("d2_run_id") == identity.d2_run_id]
    if len(matches) != 1:
        raise PhaseGError(
            "PHASE_G_RECOVERY_IDENTITY_MISMATCH", "exactly one persisted D2 attempt is required"
        )
    attempt = matches[0]
    if evidence is not None and evidence.identity != identity:
        raise PhaseGError(
            "PHASE_G_RECOVERY_IDENTITY_MISMATCH", "evidence does not match PROD attempt identity"
        )
    resolved_evidence = evidence or prod_evidence_from_attempt(identity, attempt)
    if resolved_evidence is None:
        observed_at = attempt.get("updated_at") or attempt.get("generate_action_at")
        if not isinstance(observed_at, str):
            raise PhaseGError(
                "PHASE_G_RECOVERY_EVIDENCE_MISSING", "stable persisted observation time is missing"
            )
        resolved_evidence = ProdReconciliationEvidence(
            identity=identity,
            observed_at=observed_at,
        )
    result = classify_prod_reconciliation(identity, resolved_evidence, persisted_attempt=attempt)
    if result is ProdReconciliationResult.PROVEN_NOT_SUBMITTED:
        persisted_evidence = prod_evidence_from_attempt(identity, attempt)
        if (
            persisted_evidence is None
            or classify_prod_reconciliation(identity, persisted_evidence, persisted_attempt=attempt)
            is not ProdReconciliationResult.PROVEN_NOT_SUBMITTED
        ):
            result = ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
        else:
            resolved_evidence = persisted_evidence
    audit = evidence_audit_record(result, resolved_evidence)
    audit["contract_version"] = "D2_PROD_SAME_ATTEMPT_RECOVERY_V1"
    ledger.record_prod_reconciliation(identity.d2_run_id, audit)
    if result is ProdReconciliationResult.PROVEN_NOT_SUBMITTED:
        credit_guard.release_proven_not_submitted(
            identity.d2_run_id,
            evidence_sha256=resolved_evidence.digest(),
            production_job_id=identity.production_job_id,
            scene_job_id=identity.scene_job_id,
            provider_account_id=identity.provider_account_id,
            provider_project_id=identity.provider_project_id,
        )
    return result


@dataclass(frozen=True, slots=True)
class PhaseGD2Run:
    """One successfully attributed and locally validated D2 output."""

    request: PhaseGExecutionRequest
    result: PhaseGExecutionResult
    local_file: Path
    video: ValidatedVideo


def load_phase_g_master_snapshot(
    path: Path, *, now: datetime | None = None
) -> tuple[dict[str, list[Row]], list[Row]]:
    """Load the canonical live Master snapshot for the one-shot D2 test."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("Phase G snapshot file is unavailable")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Phase G snapshot could not be read") from exc
    if not isinstance(document, dict):
        raise ValueError("Phase G snapshot must be a JSON object")
    snapshot = cast(dict[str, Any], document)
    if snapshot.get("spreadsheet_id") != APPROVED_SPREADSHEET_ID:
        raise ValueError("Phase G snapshot spreadsheet ID is not approved")
    if snapshot.get("source") != SOURCE_LIVE_GOOGLE_SHEETS:
        raise ValueError("Phase G snapshot source is not LIVE_GOOGLE_SHEETS")
    try:
        read_at_value = snapshot.get("read_at")
        if not isinstance(read_at_value, str):
            raise ValueError("missing read_at")
        read_at = parse_utc_timestamp(read_at_value)
    except ValueError as exc:
        raise ValueError("Phase G snapshot read_at must be canonical UTC RFC3339") from exc
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    age = current.astimezone(UTC) - read_at
    if age < timedelta(seconds=-60) or age > timedelta(minutes=15):
        raise ValueError("Phase G Master snapshot is outside the freshness window")
    raw_tables = snapshot.get("tables")
    if not isinstance(raw_tables, dict):
        raise ValueError("Phase G snapshot tables are missing")
    typed_raw_tables = cast(dict[str, object], raw_tables)
    raw_prompt_versions = typed_raw_tables.get("SCENE_PROMPT_VERSION")
    if not isinstance(raw_prompt_versions, list):
        raise ValueError("Phase G snapshot SCENE_PROMPT_VERSION table must be a list")
    tables: dict[str, list[Row]] = {}
    for name, rows in cast(dict[object, object], typed_raw_tables).items():
        if (
            not isinstance(name, str)
            or not isinstance(rows, list)
            or any(not isinstance(row, dict) for row in cast(list[object], rows))
        ):
            raise ValueError("Phase G snapshot table rows are invalid")
        tables[name] = cast(list[Row], rows)
    prompt_versions = tables.get("SCENE_PROMPT_VERSION")
    if prompt_versions is None:
        raise ValueError("Phase G snapshot SCENE_PROMPT_VERSION table is missing")
    return tables, prompt_versions


def build_phase_g_request_from_snapshot(
    path: Path,
    *,
    d2_run_id: str,
    runtime_state_root: Path,
    temp_root: Path,
) -> PhaseGExecutionRequest:
    """Build the normal Phase G request from the canonical snapshot tables."""
    tables, prompt_versions = load_phase_g_master_snapshot(path)
    return build_phase_g_request(
        tables,
        prompt_versions,
        d2_run_id=d2_run_id,
        runtime_state_root=runtime_state_root,
        temp_root=temp_root,
    )


def build_phase_g_request(
    tables: Mapping[str, list[Row]],
    prompt_versions: Sequence[Row],
    *,
    d2_run_id: str,
    runtime_state_root: Path,
    temp_root: Path,
    execution_context: ProductionExecutionContext | None = None,
    prerequisite_d2_run_id: str | None = None,
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
    if environment == "PROD" and (
        account_id != _PROD_ACCOUNT_ID
        or execution_context is None
        or execution_context.auth_profile != _PROD_AUTH_PROFILE
        or project_id != _PROD_PROJECT_ID
    ):
        raise PhaseGError(
            "FLOW_PROJECT_MISMATCH", "PROD execution must use the canonical pinned project"
        )
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
        if (
            prerequisite_d2_run_id is not None
            and prerequisite_d2_run_id != execution_context.prerequisite_d2_run_id
        ):
            raise PhaseGError(
                "D6_PREREQUISITE_REQUIRED", "D6 prerequisite differs from execution context"
            )
        return PhaseGExecutionRequest.from_presubmit(
            request,
            d2_run_id,
            execution_context=execution_context,
            prerequisite_d2_run_id=execution_context.prerequisite_d2_run_id,
        )
    return PhaseGExecutionRequest.from_presubmit(
        request, d2_run_id, prerequisite_d2_run_id=prerequisite_d2_run_id
    )


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
    request: PhaseGExecutionRequest,
    *,
    expected_email: str,
    retry_authorization: D2RetryAuthorization | None = None,
    test_session_id: str | None = None,
    test_session_credits_before: Decimal | None = None,
    progress: PhaseGExecutionProgress | None = None,
) -> PhaseGD2Run:
    """Perform one request, recover only its media ID, and fully validate the MP4."""
    context = request.execution_context
    ledger_root = request.presubmit_request.runtime_state_root
    if context is not None and context.environment == "PROD":
        # Keep D2 historical evidence separate; the shared guard below owns the
        # provider/day ceiling across all new TEST and PROD attempts.
        ledger_root = ledger_root / "production"
    ledger = D2DailyCreditLedger(ledger_root)
    if test_session_id is not None:
        if test_session_credits_before is None:
            raise PhaseGError(
                "PHASE_G_TEST_SESSION_BALANCE_UNKNOWN",
                "pre-attempt provider balance is required for the authorized session",
            )
        ledger.validate_test_session_request(
            request,
            session_id=test_session_id,
            auth_profile_key=_AUTH_PROFILE,
            provider_credits_before=test_session_credits_before,
        )
    credit_guard = (
        ProductionCreditGuard(request.presubmit_request.runtime_state_root) if context else None
    )
    journal = AgentSettingsRecoveryJournal(
        request.presubmit_request.runtime_state_root,
        request.provider_account_id,
        request.provider_project_id,
    )
    progress = progress or PhaseGExecutionProgress()
    port = await _open_generation_port(request, expected_email=expected_email, progress=progress)
    try:
        result = await GoogleFlowAgentOnlyGenerationExecutor(
            port, ledger, journal, production_credit_guard=credit_guard
        ).execute(
            request,
            retry_authorization=retry_authorization,
            test_session_id=test_session_id,
            test_session_credits_before=test_session_credits_before,
            progress=progress,
        )
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


async def diagnose_preclick(
    request: PhaseGExecutionRequest,
    *,
    expected_email: str,
    progress: PhaseGExecutionProgress,
) -> PhaseGPreclickDiagnosticResult:
    """Open the exact Phase G lane and stop after its read-only pre-journal checks."""
    context = request.execution_context
    if context is None or context.environment != "PROD":
        raise PhaseGError(
            "PHASE_G_DIAGNOSTIC_LANE_INVALID", "pre-click diagnosis requires a PROD request"
        )
    ledger = D2DailyCreditLedger(request.presubmit_request.runtime_state_root / "production")
    journal = AgentSettingsRecoveryJournal(
        request.presubmit_request.runtime_state_root,
        request.provider_account_id,
        request.provider_project_id,
    )
    port = await _open_generation_port(request, expected_email=expected_email, progress=progress)
    try:
        return await GoogleFlowAgentOnlyGenerationExecutor(port, ledger, journal).diagnose_preclick(
            request, progress=progress
        )
    finally:
        await port.close()


async def _open_generation_port(
    request: PhaseGExecutionRequest,
    *,
    expected_email: str,
    progress: PhaseGExecutionProgress | None = None,
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
        progress=progress,
    )


async def recover_late_confirmation_same_attempt(
    request: PhaseGExecutionRequest,
    *,
    expected_email: str,
    expected_state: str,
    expected_ledger_sha256: str,
    expected_confirmation_id: str,
    expected_video_count: int,
    expected_credit_cost: int,
) -> PhaseGExecutionResult:
    """Approve one revalidated late gate and observe only the existing TEST submit."""
    if expected_state != "UNKNOWN_SUBMISSION_STATE":
        raise PhaseGError("PHASE_G_RECOVERY_STATE_MISMATCH", "recovery requires an unknown attempt")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", expected_confirmation_id)
        or expected_video_count != 1
        or expected_credit_cost <= 0
    ):
        raise PhaseGError("PHASE_G_CREDIT_GATE_UNPROVEN", "expected late gate quote is invalid")
    if (
        request.provider_id != "PRV_0001"
        or request.provider_account_id != "PVA_0001"
        or request.provider_project_id != "34e60953-e534-46e9-ac65-993893e61065"
        or (
            request.execution_context is not None
            and request.execution_context.environment != "TEST"
        )
    ):
        raise PhaseGError(
            "PHASE_G_RECOVERY_IDENTITY_MISMATCH", "recovery is restricted to the exact TEST lane"
        )
    ledger = D2DailyCreditLedger(request.presubmit_request.runtime_state_root)
    if not ledger.path.is_file() or sha256_file(ledger.path) != expected_ledger_sha256:
        raise PhaseGError("PHASE_G_LEDGER_CHANGED", "D2 ledger changed since recovery review")
    attempt = next(
        (row for row in ledger.attempts() if row.get("d2_run_id") == request.d2_run_id), None
    )
    if attempt is None or (
        attempt.get("submission_state") != expected_state
        or attempt.get("terminal_state") is not None
        or attempt.get("generate_action_count") != 1
        or attempt.get("approval_action_count") != 0
        or attempt.get("provider_execution_id") is not None
        or attempt.get("provider_media_id") is not None
        or attempt.get("provider_media_ids")
        or attempt.get("scene_id") != request.scene_id
        or attempt.get("scene_prompt_id") != request.scene_prompt_id
        or attempt.get("generation_profile_id") != request.generation_profile_id
        or attempt.get("provider_account_id") != request.provider_account_id
        or attempt.get("provider_project_id") != request.provider_project_id
        or attempt.get("prompt_sha256") != request.prompt_sha256
    ):
        raise PhaseGError("PHASE_G_RECOVERY_IDENTITY_MISMATCH", "stored attempt is not recoverable")
    baseline_ids = _string_list(attempt.get("baseline_media_ids"))
    execution_ids = _string_list(attempt.get("baseline_execution_ids"))
    confirmation_ids = _string_list(attempt.get("baseline_confirmation_ids"))
    chat_bubble_hashes = _string_list(attempt.get("baseline_agent_turn_bubble_hashes"))
    finished_tile_key_hashes = _string_list(attempt.get("baseline_finished_tile_key_hashes", []))
    chat_video_media_ids = _string_list(attempt.get("baseline_chat_video_media_ids", []))
    agent_turn_id = attempt.get("baseline_agent_turn_id")
    pending_count = attempt.get("baseline_pending_count")
    if (
        attempt.get("baseline_captured") is not True
        or baseline_ids is None
        or execution_ids is None
        or confirmation_ids is None
        or (
            chat_bubble_hashes is not None
            and any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in chat_bubble_hashes)
        )
        or finished_tile_key_hashes is None
        or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in finished_tile_key_hashes)
        or chat_video_media_ids is None
        or any(not _UUID_RE.fullmatch(value) for value in chat_video_media_ids)
        or isinstance(pending_count, bool)
        or not isinstance(pending_count, int)
        or pending_count != 0
        or agent_turn_id != "0:"
    ):
        raise PhaseGError("PHASE_G_BASELINE_INVALID", "persisted submit baseline is invalid")
    baseline = FlowSubmissionBaseline(
        request.provider_project_id,
        frozenset(baseline_ids),
        frozenset(execution_ids),
        pending_count,
        frozenset(confirmation_ids),
        str(agent_turn_id),
        tuple(chat_bubble_hashes or ()),
        tuple(finished_tile_key_hashes),
        frozenset(chat_video_media_ids),
    )
    journal = AgentSettingsRecoveryJournal(
        request.presubmit_request.runtime_state_root,
        request.provider_account_id,
        request.provider_project_id,
    )
    try:
        journal.require_clean_or_missing()
    except Exception as exc:
        raise PhaseGError(
            "PHASE_G_RECOVERY_REQUIRED", "settings recovery journal is not clean"
        ) from exc
    port = await _open_generation_port(request, expected_email=expected_email)
    try:
        await port.bind_request(request)
        gate = await port.read_current_confirmation(baseline)
        if (
            gate.confirmation_id != expected_confirmation_id
            or gate.project_id != request.provider_project_id
            or gate.video_count != expected_video_count
            or gate.credit_cost != expected_credit_cost
            or gate.progress_before_confirmation
        ):
            raise PhaseGError(
                "PHASE_G_LATE_GATE_CHANGED", "live gate differs from reviewed evidence"
            )
        ledger.record_late_confirmation(
            request.d2_run_id,
            expected_ledger_sha256=expected_ledger_sha256,
            confirmation_id=gate.confirmation_id,
            project_id=gate.project_id,
            video_count=gate.video_count,
            credit_cost=gate.credit_cost,
            visible_balance=gate.visible_balance,
        )
        ledger.approve_gate(
            request.d2_run_id,
            video_count=gate.video_count,
            credit_cost=gate.credit_cost,
            visible_balance=gate.visible_balance,
            confirmation_id=gate.confirmation_id,
        )
        try:
            await port.approve_confirmation(gate.confirmation_id)
            evidence = await port.observe_attempt(
                baseline,
                gate.confirmation_id,
                observation_deadline=time.monotonic() + 240,
            )
        except WafRejectionError:
            ledger.record_state(
                request.d2_run_id,
                submission_state="WAF_REJECTED",
                terminal_state="WAF_REJECTED",
                provider_project_id=request.provider_project_id,
            )
            raise
        except Exception:
            ledger.record_state(
                request.d2_run_id,
                submission_state="UNKNOWN_SUBMISSION_STATE",
                provider_project_id=request.provider_project_id,
            )
            raise
        if (
            evidence.d2_run_id != request.d2_run_id
            or evidence.provider_project_id != request.provider_project_id
            or evidence.attributed_confirmation_id != gate.confirmation_id
        ):
            ledger.record_state(
                request.d2_run_id,
                submission_state="UNKNOWN_SUBMISSION_STATE",
                provider_project_id=request.provider_project_id,
            )
            raise PhaseGError(
                "PHASE_G_ATTEMPT_ATTRIBUTION_FAILED", "recovered result is not attributable"
            )
        media_ids = evidence.new_provider_media_ids or (
            (evidence.provider_media_id,) if evidence.provider_media_id else ()
        )
        if any(media_id in baseline.media_ids for media_id in media_ids) or (
            evidence.provider_execution_id in baseline.execution_ids
        ):
            ledger.record_state(
                request.d2_run_id,
                submission_state="UNKNOWN_SUBMISSION_STATE",
                provider_project_id=request.provider_project_id,
            )
            raise PhaseGError(
                "PHASE_G_ATTEMPT_ATTRIBUTION_FAILED",
                "recovered provider identity existed in the submit baseline",
            )
        if len(media_ids) > 1:
            ledger.record_state(
                request.d2_run_id,
                submission_state="UNEXPECTED_MULTI_OUTPUT",
                terminal_state="UNEXPECTED_MULTI_OUTPUT",
                provider_execution_id=evidence.provider_execution_id,
                provider_media_ids=media_ids,
                provider_project_id=request.provider_project_id,
            )
            raise PhaseGError(
                "UNEXPECTED_MULTI_OUTPUT", "recovered result violates one-output attribution"
            )
        if evidence.submission_state == "GENERATION_SUCCEEDED" and len(media_ids) != 1:
            raise PhaseGError(
                "PHASE_G_ATTEMPT_ATTRIBUTION_FAILED", "success has no exact new media ID"
            )
        ledger.record_state(
            request.d2_run_id,
            submission_state=evidence.submission_state,
            terminal_state=evidence.terminal_state,
            provider_execution_id=evidence.provider_execution_id,
            provider_media_id=evidence.provider_media_id,
            provider_media_ids=media_ids,
            provider_project_id=evidence.provider_project_id,
            accounted_credit_cost=evidence.accounted_credit_cost,
        )
        return PhaseGExecutionResult(
            request.d2_run_id,
            evidence.submission_state,
            gate.confirmation_id,
            evidence.provider_execution_id,
            evidence.provider_media_id,
            evidence.provider_project_id,
        )
    finally:
        await port.close()


def _string_list(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    result: list[str] = []
    for item in cast(list[object], value):
        if not isinstance(item, str):
            return None
        result.append(item)
    return result


async def recover_and_validate_existing(
    request: PhaseGExecutionRequest, *, expected_email: str
) -> PhaseGD2Run:
    """Recover and validate the exact media ID already recorded for this run."""
    ledger_root = request.presubmit_request.runtime_state_root
    if request.execution_context is not None and request.execution_context.environment == "PROD":
        ledger_root = ledger_root / "production"
    ledger = D2DailyCreditLedger(ledger_root)
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

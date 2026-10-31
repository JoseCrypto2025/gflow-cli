"""Run one locked, approved production Scene through the canonical V2 runtime."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from gflow_cli.services.credits import inspect_profile
from gflow_cli.v2_runtime.config import load_runtime_settings
from gflow_cli.v2_runtime.google_drive_storage import GoogleDriveStorageAdapter
from gflow_cli.v2_runtime.google_sheets import (
    APPROVED_JOB_ARTIFACTS_FOLDER_ID,
    APPROVED_SPREADSHEET_ID,
    GoogleSheetsApiTransport,
    GoogleSheetsSnapshot,
    GoogleSheetsSnapshotSource,
    GoogleWorkspaceCredentialProvider,
)
from gflow_cli.v2_runtime.google_sheets_production_jobs import GoogleSheetsProductionJobStore
from gflow_cli.v2_runtime.google_sheets_transactions import (
    GoogleSheetsTransactionApiTransport,
    OptimisticGoogleSheetsJobStore,
)
from gflow_cli.v2_runtime.legacy_replacement import (
    ReplacementGenerationConfig,
    ReplacementQuote,
    ReplacementQuoteStatus,
    quote_generation_reservation,
)
from gflow_cli.v2_runtime.phase_e import InMemorySceneCatalog, Row, preflight_scene
from gflow_cli.v2_runtime.phase_g import PhaseGExecutionRequest
from gflow_cli.v2_runtime.phase_g_run import build_phase_g_request, run_fresh_presubmit
from gflow_cli.v2_runtime.production_execution import (
    CreditBudgetContext,
    ProductionExecutionContext,
)
from gflow_cli.v2_runtime.production_jobs import (
    ProductionJobCoordinator,
    ProductionRequest,
    SceneJob,
)
from gflow_cli.v2_runtime.production_orchestrator import (
    ProductionOrchestrator,
    scene_phase_g_run_id,
)
from gflow_cli.v2_runtime.timestamps import format_utc_timestamp, parse_utc_timestamp
from gflow_cli.v2_runtime.workspace import JobWorkspace, create_workspace, validate_workspace

_SCENE_ID_RE = re.compile(r"^SCN_[0-9]{6}$")
_PROFILE_ID_RE = re.compile(r"^GPR_[0-9]{4}$")
_ACCOUNT_ID_RE = re.compile(r"^PVA_[0-9]{4}$")
_PROJECT_ID_RE = re.compile(r"^[0-9a-f-]{36}$", re.I)
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_PROD_CREDITS_PROFILE = "gflow-prod"
_PROD_BROWSER_STATE_ROOT = Path("/var/lib/gflow/browser-state")
_RUN_INTENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PERSISTED_RUN_KEY_RE = re.compile(
    r"^PROD_SCENE_RUN_V2_[0-9a-f]{64}_[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
)


class ProductionSceneOperatorError(ValueError):
    """Safe, fail-closed production operator precondition failure."""


def _unique_row(rows: list[Row], key: str, value: str, label: str) -> Row:
    matches = [row for row in rows if row.get(key) == value]
    if len(matches) != 1:
        raise ProductionSceneOperatorError(f"{label} must resolve exactly once")
    return matches[0]


def _snapshot_digest(tables: dict[str, list[Row]]) -> str:
    payload = json.dumps(tables, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_current_production_credits(expected_email: str) -> Decimal:
    """Read the current balance through the existing profile-bound Flow client."""
    result = asyncio.run(inspect_profile(_PROD_CREDITS_PROFILE))
    credits = result.get("credits")
    email = result.get("email")
    if (
        result.get("status") != "ok"
        or result.get("authenticated") is not True
        or not isinstance(email, str)
        or email.casefold() != expected_email.casefold()
        or isinstance(credits, bool)
        or not isinstance(credits, int)
        or credits < 0
    ):
        raise ProductionSceneOperatorError(
            "fresh authenticated production Flow balance is unavailable or identity differs"
        )
    return Decimal(credits)


def _require_production_profile_home() -> Path:
    configured = os.environ.get("GFLOW_CLI_HOME")
    if configured is None:
        os.environ["GFLOW_CLI_HOME"] = _PROD_BROWSER_STATE_ROOT.as_posix()
    elif configured.replace("\\", "/") != _PROD_BROWSER_STATE_ROOT.as_posix():
        raise ProductionSceneOperatorError("production browser profile root differs")
    return _PROD_BROWSER_STATE_ROOT


def _request_key(
    scene_id: str,
    profile_id: str,
    prompt_id: str,
    prompt_sha: str,
    production_run_intent: str,
) -> str:
    workload = json.dumps(
        ["PRODUCTION_SCENE_WORKLOAD_V1", scene_id, profile_id, prompt_id, prompt_sha],
        separators=(",", ":"),
    )
    workload_digest = hashlib.sha256(workload.encode("utf-8")).hexdigest()
    key_prefix = f"PROD_SCENE_RUN_V2_{workload_digest}_"
    if production_run_intent.startswith("PROD_SCENE_RUN_V2_"):
        if not production_run_intent.startswith(key_prefix) or not _PERSISTED_RUN_KEY_RE.fullmatch(
            production_run_intent
        ):
            raise ProductionSceneOperatorError("persisted production run key has another workload")
        return production_run_intent
    if not _RUN_INTENT_RE.fullmatch(production_run_intent):
        raise ProductionSceneOperatorError("explicit production run intent is invalid")
    return key_prefix + production_run_intent


def _has_unresolved_different_workload(
    rows: Sequence[dict[str, object]], request: ProductionRequest
) -> bool:
    return any(
        row.get("Script_ID") == request.script_id
        and row.get("Request_Idempotency_Key") != request.request_idempotency_key
        and row.get("Scene_Set_SHA256") != request.scene_set_sha256
        and str(row.get("Job_Status", "")).upper() not in {"COMPLETED", "FAILED", "CANCELLED"}
        for row in rows
    )


def _quote_config(
    tables: dict[str, list[Row]],
    *,
    scene_id: str,
    profile_id: str,
    account_id: str,
    effective_config: Mapping[str, object],
) -> ReplacementGenerationConfig:
    account = _unique_row(
        tables.get("PROVIDER_ACCOUNT_MASTER", []),
        "Provider_Account_ID",
        account_id,
        "production provider account",
    )
    profile = _unique_row(
        tables.get("GENERATION_PROFILE_MASTER", []),
        "Generation_Profile_ID",
        profile_id,
        "production generation profile",
    )
    model_id = str(profile.get("Model_ID", ""))
    model = _unique_row(tables.get("MODEL_MASTER", []), "Model_ID", model_id, "active model")
    if (
        account.get("Provider_ID") != "PRV_0001"
        or account.get("Environment") != "PROD"
        or account.get("Status") != "ACTIVE"
        or account.get("Auth_Profile_Key") != "GFLOW_PROD_AUTH"
        or account.get("Provider_Project_ID") != "aafe9282-071f-4333-baa4-92b9f76a9c27"
        or profile.get("Provider_Account_ID") != account_id
        or profile.get("Environment") != "PROD"
        or profile.get("Status") != "ACTIVE"
        or profile.get("Generation_Mode") != "TEXT_TO_VIDEO"
        or model.get("Provider_ID") != "PRV_0001"
        or model.get("Status") != "ACTIVE"
        or re.sub(r"[^a-z0-9]+", "", str(model.get("Model_Name", "")).casefold()) != "veo31lite"
    ):
        raise ProductionSceneOperatorError("active quote identity differs from PROD workload")
    try:
        duration_sec = float(str(effective_config["duration_sec"]))
        output_count = int(str(effective_config["output_count"]))
        aspect_ratio = str(effective_config["aspect_ratio"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProductionSceneOperatorError("effective quote configuration is incomplete") from exc
    if output_count != 1 or duration_sec <= 0 or not aspect_ratio:
        raise ProductionSceneOperatorError("quote configuration is outside one-video scope")
    return ReplacementGenerationConfig(
        provider_id="PRV_0001",
        provider_account_id=account_id,
        model_id=model_id,
        generation_profile_id=profile_id,
        generation_mode="TEXT_TO_VIDEO",
        pricing_variant="STANDARD_LITE",
        account_pricing_class="NON_ULTRA",
        output_count=output_count,
        duration_sec=duration_sec,
        aspect_ratio=aspect_ratio,
    )


def _validated_quote(
    snapshot: GoogleSheetsSnapshot,
    tables: dict[str, list[Row]],
    *,
    scene_id: str,
    profile_id: str,
    account_id: str,
    effective_config: Mapping[str, object],
) -> ReplacementQuote:
    try:
        when = datetime.fromisoformat(snapshot.read_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductionSceneOperatorError("canonical quote timestamp is invalid") from exc
    quote = quote_generation_reservation(
        _quote_config(
            tables,
            scene_id=scene_id,
            profile_id=profile_id,
            account_id=account_id,
            effective_config=effective_config,
        ),
        snapshot=snapshot,
        quoted_at=when,
    )
    if (
        quote.status is not ReplacementQuoteStatus.VALID
        or quote.amount is None
        or quote.amount <= 0
        or quote.unit != "CREDIT"
        or not quote.input_digest
        or not quote.source_version
    ):
        raise ProductionSceneOperatorError("fresh canonical per-run quote is unavailable")
    assert quote.amount is not None
    return quote


def _execution_context(
    *,
    tables: dict[str, list[Row]],
    scene_id: str,
    profile_id: str,
    account_id: str,
    project_id: str,
    prompt_id: str,
    attempt_id: str,
    visible_free_credits: Decimal,
    quote: ReplacementQuote,
    production_job_id: str | None = None,
    scene_job_id: str | None = None,
) -> ProductionExecutionContext:
    if quote.amount is None:
        raise ProductionSceneOperatorError("validated canonical per-run quote is unavailable")
    account = _unique_row(
        tables.get("PROVIDER_ACCOUNT_MASTER", []),
        "Provider_Account_ID",
        account_id,
        "production provider account",
    )
    if (
        account.get("Environment") != "PROD"
        or account.get("Status") != "ACTIVE"
        or account.get("Auth_Profile_Key") != "GFLOW_PROD_AUTH"
        or account.get("Execution_Target") != "WEB_UI"
        or account.get("Base_URL") != "https://flow.google.com/"
        or account.get("Provider_Project_ID") != project_id
    ):
        raise ProductionSceneOperatorError("production account or project identity differs")
    return ProductionExecutionContext(
        environment="PROD",
        provider_id=str(account.get("Provider_ID", "")),
        provider_account_id=account_id,
        provider_project_id=project_id,
        auth_profile="GFLOW_PROD_AUTH",
        scene_id=scene_id,
        generation_profile_id=profile_id,
        scene_prompt_id=prompt_id,
        attempt_id=attempt_id,
        credit_budget_context=CreditBudgetContext(
            provider_remaining_free=visible_free_credits,
            is_free_quota=True,
            paid_fallback=False,
        ),
        # _validated_quote has already rejected a missing amount.
        max_reservation_credits=quote.amount,
        reservation_quote_input_digest=quote.input_digest,
        reservation_quote_source_version=quote.source_version,
        production_job_id=production_job_id,
        scene_job_id=scene_job_id,
    )


def _build_bound_request(
    tables: dict[str, list[Row]],
    *,
    scene_id: str,
    profile_id: str,
    context: ProductionExecutionContext,
    runtime_state_root: Path,
    temp_root: Path,
) -> PhaseGExecutionRequest:
    return build_phase_g_request(
        tables,
        tables.get("SCENE_PROMPT_VERSION", []),
        d2_run_id=context.attempt_id,
        runtime_state_root=runtime_state_root,
        temp_root=temp_root,
        execution_context=context,
    )


def _workspace_for_job(temp_root: Path, scene_job_id: str, status: str) -> JobWorkspace:
    root = temp_root / scene_job_id
    if root.exists():
        if root.is_symlink() or not root.is_dir():
            raise ProductionSceneOperatorError("existing Scene Job workspace is unsafe")
        workspace = JobWorkspace(
            scene_job_id,
            root,
            {
                name: root / name
                for name in ("input", "generated", "audio", "ffmpeg", "validated", "logs")
            },
        )
        validate_workspace(temp_root, workspace)
        return workspace
    if status != "PENDING":
        raise ProductionSceneOperatorError("unresolved Scene Job has no recoverable workspace")
    return create_workspace(temp_root, scene_job_id)


def run_once(
    *,
    scene_id: str,
    profile_id: str,
    account_id: str,
    project_id: str,
    expected_email: str,
    execute: bool,
    production_run_intent: str | None = None,
) -> dict[str, object]:
    _require_production_profile_home()
    if execute and (
        production_run_intent is None
        or not (
            _RUN_INTENT_RE.fullmatch(production_run_intent)
            or _PERSISTED_RUN_KEY_RE.fullmatch(production_run_intent)
        )
    ):
        raise ProductionSceneOperatorError(
            "execute requires an explicit stable production run intent"
        )
    if (
        not _SCENE_ID_RE.fullmatch(scene_id)
        or not _PROFILE_ID_RE.fullmatch(profile_id)
        or not _ACCOUNT_ID_RE.fullmatch(account_id)
        or not _PROJECT_ID_RE.fullmatch(project_id)
        or str(UUID(project_id)) != project_id.lower()
        or not _EMAIL_RE.fullmatch(expected_email)
    ):
        raise ProductionSceneOperatorError("explicit production identity is required")

    settings = load_runtime_settings()
    if settings.environment != "production" or settings.storage_provider != "google-drive":
        raise ProductionSceneOperatorError("production Google Drive runtime settings are required")
    credential_value = os.environ.get("GFLOW_GOOGLE_WORKSPACE_CREDENTIALS", "")
    if not credential_value:
        raise ProductionSceneOperatorError("Google Workspace credentials are not configured")
    credential_path = Path(credential_value)
    repository_root = Path(__file__).resolve().parents[2]
    token_provider = GoogleWorkspaceCredentialProvider(
        credential_path, repository_root=repository_root
    )
    reader = GoogleSheetsApiTransport(token_provider)
    writer_transport = GoogleSheetsTransactionApiTransport(token_provider)
    try:
        schema_dir = repository_root / "schemas"
        snapshot_source = GoogleSheetsSnapshotSource(reader, schema_dir)
        snapshot = snapshot_source.read()
        if snapshot.spreadsheet_id != APPROVED_SPREADSHEET_ID:
            raise ProductionSceneOperatorError("live snapshot spreadsheet identity differs")
        tables = {name: list(rows) for name, rows in snapshot.tables.items()}
        _unique_row(tables.get("SCENES", []), "Scene_ID", scene_id, "locked scene")
        scene = _unique_row(tables["SCENES"], "Scene_ID", scene_id, "locked scene")
        profile = _unique_row(
            tables.get("GENERATION_PROFILE_MASTER", []),
            "Generation_Profile_ID",
            profile_id,
            "production generation profile",
        )
        if (
            profile.get("Environment") != "PROD"
            or profile.get("Status") != "ACTIVE"
            or profile.get("Provider_Account_ID") != account_id
        ):
            raise ProductionSceneOperatorError(
                "generation profile is not bound to this PROD account"
            )
        preflight = preflight_scene(InMemorySceneCatalog(tables), scene_id, profile_id, "PROD")
        if preflight.status != "PREFLIGHT_PASS" or not preflight.ready_for_provider_adapter:
            raise ProductionSceneOperatorError("fresh Phase E did not pass")
        if preflight.references:
            raise ProductionSceneOperatorError("production references are outside this operator")
        if scene.get("Dialogue_Mode") != "NONE":
            raise ProductionSceneOperatorError(
                "this operator supports only the approved NO_AUDIO scene"
            )
        dialogue_rows = tuple(tables.get("SCENE_DIALOGUE_LINES", []))
        if any(
            row.get("Scene_ID") == scene_id and str(row.get("Status", "")).upper() == "ACTIVE"
            for row in dialogue_rows
        ):
            raise ProductionSceneOperatorError("NO_AUDIO scene has active dialogue rows")
        prompt_rows = [
            row
            for row in tables.get("SCENE_PROMPT_VERSION", [])
            if row.get("Scene_ID") == scene_id
            and row.get("Generation_Profile_ID") == profile_id
            and row.get("Prompt_Status") == "APPROVED"
        ]
        if len(prompt_rows) != 1:
            raise ProductionSceneOperatorError("one current approved prompt is required")
        approved_prompt = prompt_rows[0]
        prompt_id = str(approved_prompt.get("Scene_Prompt_ID", ""))
        prompt_sha = str(approved_prompt.get("Prompt_SHA256", ""))
        if not prompt_id or not re.fullmatch(r"[0-9a-f]{64}", prompt_sha):
            raise ProductionSceneOperatorError("approved prompt binding is invalid")
        quote = _validated_quote(
            snapshot,
            tables,
            scene_id=scene_id,
            profile_id=profile_id,
            account_id=account_id,
            effective_config=preflight.technical_configuration,
        )
        script_id = str(scene.get("Script_ID", ""))
        if not script_id:
            raise ProductionSceneOperatorError("scene script identity is missing")
        request = ProductionRequest(
            script_id,
            "PROD",
            _request_key(
                scene_id,
                profile_id,
                prompt_id,
                prompt_sha,
                production_run_intent or "READ_ONLY_PREFLIGHT",
            ),
            (prompt_id,),
        )
        starting_credits = _read_current_production_credits(expected_email)
        digest_before_f5 = _snapshot_digest(tables)
        context = _execution_context(
            tables=tables,
            scene_id=scene_id,
            profile_id=profile_id,
            account_id=account_id,
            project_id=project_id,
            prompt_id=prompt_id,
            attempt_id="D2_"
            + hashlib.sha256((request.request_idempotency_key + ":F5").encode()).hexdigest()[:32],
            visible_free_credits=starting_credits,
            quote=quote,
        )
        phase_g_for_f5 = _build_bound_request(
            tables,
            scene_id=scene_id,
            profile_id=profile_id,
            context=context,
            runtime_state_root=settings.runtime_state_root,
            temp_root=settings.temp_root,
        )
        if phase_g_for_f5.output_count != 1:
            raise ProductionSceneOperatorError("production output count must be exactly one")
        if not execute:
            return {
                "status": "READY_FOR_F5_AND_EXECUTION",
                "scene_id": scene_id,
                "generation_profile_id": profile_id,
                "provider_account_id": account_id,
                "provider_project_id": project_id,
                "scene_prompt_id": prompt_id,
                "audio_intent": "NO_AUDIO",
                "output_count": 1,
                "validated_quote_credits": str(quote.amount),
                "provider_credits_before_f5": str(starting_credits),
                "generate_clicked": False,
                "sheet_rows_written": False,
            }

        asyncio.run(run_fresh_presubmit(phase_g_for_f5, expected_email=expected_email))
        refreshed = snapshot_source.read()
        refreshed_tables = {name: list(rows) for name, rows in refreshed.tables.items()}
        if _snapshot_digest(refreshed_tables) != digest_before_f5:
            raise ProductionSceneOperatorError(
                "canonical Sheets data changed during F5; rerun gates"
            )
        tables = refreshed_tables
        quote = _validated_quote(
            refreshed,
            tables,
            scene_id=scene_id,
            profile_id=profile_id,
            account_id=account_id,
            effective_config=preflight.technical_configuration,
        )
        scene = _unique_row(tables["SCENES"], "Scene_ID", scene_id, "locked scene")
        approved_prompt = _unique_row(
            [
                row
                for row in tables.get("SCENE_PROMPT_VERSION", [])
                if row.get("Scene_ID") == scene_id
                and row.get("Generation_Profile_ID") == profile_id
                and row.get("Prompt_Status") == "APPROVED"
            ],
            "Scene_Prompt_ID",
            prompt_id,
            "current approved prompt",
        )
        preflight = preflight_scene(InMemorySceneCatalog(tables), scene_id, profile_id, "PROD")
        if (
            preflight.status != "PREFLIGHT_PASS"
            or not preflight.ready_for_provider_adapter
            or preflight.prompt_sha256 != prompt_sha
        ):
            raise ProductionSceneOperatorError("post-F5 Phase E or prompt binding changed")
        current_credits = _read_current_production_credits(expected_email)

        transaction_rows = OptimisticGoogleSheetsJobStore(
            writer_transport, schema_dir, writer_lock_root=settings.runtime_state_root
        )
        jobs = GoogleSheetsProductionJobStore(transaction_rows)
        existing_productions = transaction_rows.read_rows("PRODUCTION_JOBS")
        matching_requests = [
            row
            for row in existing_productions
            if row.get("Request_Idempotency_Key") == request.request_idempotency_key
        ]
        if len(matching_requests) > 1:
            raise ProductionSceneOperatorError("production idempotency key is duplicated")
        if _has_unresolved_different_workload(existing_productions, request):
            raise ProductionSceneOperatorError(
                "another unresolved Production Job exists for a different workload"
            )
        claim = ProductionJobCoordinator(jobs).submit_request(request, datetime.now(UTC))
        if len(claim.scene_jobs) != 1:
            raise ProductionSceneOperatorError("exactly one Scene Job must be claimed")
        scene_job = claim.scene_jobs[0]
        workspace = _workspace_for_job(
            settings.temp_root, scene_job.scene_job_id, scene_job.job_status
        )
        created_at = (
            parse_utc_timestamp(scene_job.created_at) if scene_job.created_at else datetime.now(UTC)
        )
        d2_run_id = scene_phase_g_run_id(scene_job)
        execution_context = _execution_context(
            tables=tables,
            scene_id=scene_id,
            profile_id=profile_id,
            account_id=account_id,
            project_id=project_id,
            prompt_id=prompt_id,
            attempt_id=d2_run_id,
            visible_free_credits=current_credits,
            quote=quote,
            production_job_id=claim.production_job.production_job_id,
            scene_job_id=scene_job.scene_job_id,
        )
        phase_g_request = _build_bound_request(
            tables,
            scene_id=scene_id,
            profile_id=profile_id,
            context=execution_context,
            runtime_state_root=settings.runtime_state_root,
            temp_root=workspace.directories["generated"],
        )

        def phase_g_request_factory(scene_job: SceneJob, d2_run_id: str) -> PhaseGExecutionRequest:
            if (
                scene_phase_g_run_id(scene_job) != d2_run_id
                or phase_g_request.d2_run_id != d2_run_id
            ):
                raise ProductionSceneOperatorError("D2 identity differs from claimed Scene Job")
            return phase_g_request

        drive = GoogleDriveStorageAdapter(
            token_provider,
            approved_folder_id=APPROVED_JOB_ARTIFACTS_FOLDER_ID,
        )
        orchestrator = ProductionOrchestrator(
            jobs,
            transaction_rows,
            drive,
            temp_root=settings.temp_root,
        )
        result = orchestrator.run_scene_once(
            request,
            scene=scene,
            approved_prompt=approved_prompt,
            preflight=preflight,
            dialogue_rows=dialogue_rows,
            expected_duration_sec=phase_g_request.duration_sec,
            expected_aspect_ratio=phase_g_request.aspect_ratio,
            workspace=workspace,
            workspace_created_at=created_at,
            verification_temp_dir=workspace.directories["validated"] / "remote-verification",
            occurred_at=format_utc_timestamp(datetime.now(UTC), timespec="seconds"),
            phase_g_request_factory=phase_g_request_factory,
            expected_email=expected_email,
        )
        return {
            "status": "PASS",
            "production_job_id": result.claim.production_job.production_job_id,
            "scene_job_id": result.scene_job.scene_job_id,
            "d2_run_id": d2_run_id,
            "final_output_id": result.final_output.final_output_id if result.final_output else None,
            "remote_file_id": result.final_output.remote_file_id if result.final_output else None,
            "output_sha256": result.final_output.output_sha256 if result.final_output else None,
            "audio_intent": result.audio_intent.value,
            "provider_credits_before_f5": str(starting_credits),
            "provider_credits_before_generate": str(current_credits),
            "generate_clicked": True,
        }
    finally:
        reader.close()
        writer_transport.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scene-id", required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--provider-account-id", required=True)
    parser.add_argument("--provider-project-id", required=True)
    parser.add_argument("--expected-email", required=True)
    parser.add_argument(
        "--production-run-intent",
        help=(
            "stable explicit identity for one separately authorized certification run; "
            "for replay, the persisted Request_Idempotency_Key may be supplied"
        ),
    )
    parser.add_argument("--execute", action="store_true", help="run F5 and the one-shot lifecycle")
    args = parser.parse_args()
    try:
        result = run_once(
            scene_id=args.scene_id,
            profile_id=args.profile_id,
            account_id=args.provider_account_id,
            project_id=args.provider_project_id,
            expected_email=args.expected_email,
            execute=args.execute,
            production_run_intent=args.production_run_intent,
        )
    except Exception as exc:
        code = type(exc).__name__
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", code):
            code = "ProductionSceneOperatorError"
        print(json.dumps({"status": "HOLD", "failure_class": code}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

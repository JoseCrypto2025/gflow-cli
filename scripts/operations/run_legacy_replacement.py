"""Quote, diagnose, or execute one exact legacy PROD replacement through Phase G only."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import cast
from uuid import UUID

from gflow_cli.services.credits import inspect_profile
from gflow_cli.v2_runtime.ambiguous_legacy_recovery_policy import (
    AUDIT_NAME as POLICY_AUDIT_NAME,
)
from gflow_cli.v2_runtime.ambiguous_legacy_recovery_policy import (
    ambiguous_legacy_policy_id,
    existing_policy_event,
)
from gflow_cli.v2_runtime.config import load_runtime_settings
from gflow_cli.v2_runtime.google_sheets import (
    APPROVED_SPREADSHEET_ID,
    SOURCE_LIVE_GOOGLE_SHEETS,
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
    LegacyReplacementAuthorizationRequest,
    ReplacementDecision,
    ReplacementGenerationConfig,
    ReplacementQuote,
    ReplacementQuoteStatus,
    consume_legacy_replacement_authorization,
    create_legacy_replacement_authorization,
    evaluate_replacement_decision,
    legacy_replacement_request_idempotency_key,
    quote_generation_reservation,
)
from gflow_cli.v2_runtime.phase_e import InMemorySceneCatalog, Row, preflight_scene
from gflow_cli.v2_runtime.phase_g import (
    D2DailyCreditLedger,
    PhaseGExecutionRequest,
)
from gflow_cli.v2_runtime.phase_g_artifacts import ValidatedVideo, validate_video
from gflow_cli.v2_runtime.phase_g_diagnostics import (
    PhaseGDiagnosticRecorder,
    PhaseGExecutionProgress,
    PhaseGFailureReason,
    PhaseGStage,
)
from gflow_cli.v2_runtime.phase_g_prod_legacy_reconciliation import (
    LegacyProdReconciliationDecision,
    build_legacy_prod_attempt_envelope,
    find_legacy_prod_reconciliation_audit,
    legacy_recovery_prerequisite_satisfied,
    reconcile_legacy_prod_same_attempt,
)
from gflow_cli.v2_runtime.phase_g_prod_reconciliation import (
    ProdAttemptIdentity,
    ProdReconciliationResult,
)
from gflow_cli.v2_runtime.phase_g_run import (
    PhaseGD2Run,
    _open_generation_port,
    build_phase_g_request,
    diagnose_preclick,
    execute_and_validate,
    reconcile_prod_same_attempt,
)
from gflow_cli.v2_runtime.production_execution import (
    CreditBudgetContext,
    ProductionCreditGuard,
    ProductionExecutionContext,
)
from gflow_cli.v2_runtime.production_jobs import (
    ProductionJobCoordinator,
    ProductionRequest,
    SceneJob,
)
from gflow_cli.v2_runtime.production_orchestrator import scene_phase_g_run_id
from gflow_cli.v2_runtime.timestamps import format_utc_timestamp
from gflow_cli.v2_runtime.workspace import JobWorkspace, create_workspace, validate_workspace
from scripts.operations.reconcile_legacy_prod_attempt import (
    _software_commit,
    build_legacy_sources_from_snapshot,
)

_PARENT_ATTEMPT_ID = "D2_13c62112d9881710019049d95cb6da57"
_ACCOUNT_ID = "PVA_0002"
_PROVIDER_ID = "PRV_0001"
_MODEL_ID = "MDL_0001"
_SCENE_ID = "SCN_000001"
_PROFILE_ID = "GPR_0002"
_PROJECT_ID = "aafe9282-071f-4333-baa4-92b9f76a9c27"
_EXPECTED_EMAIL = "singleava508@gmail.com"
_AUTH_PROFILE = "GFLOW_PROD_AUTH"
_MODEL_DISPLAY_NAMES = {_MODEL_ID: "Veo 3.1 - Lite"}
_CREDITS_PROFILE = "gflow-prod"
_MAX_CREDITS = Decimal("50")


class LegacyReplacementOperatorError(ValueError):
    """Safe, fail-closed precondition failure from this one-shot operator."""


class LegacyParentJobLinkageError(LegacyReplacementOperatorError):
    """The legacy parent does not have both persisted canonical job IDs."""


def _record_phase_g_failure(
    recorder: PhaseGDiagnosticRecorder,
    progress: PhaseGExecutionProgress,
    exc: Exception,
    *,
    repository_root: Path,
    reason_code: PhaseGFailureReason,
    failed_stage: PhaseGStage,
    sensitive_values: tuple[str, ...] = (),
) -> bool:
    """Persist a redacted failure without letting diagnostics change execution state."""
    try:
        recorder.record_failure(
            exc,
            progress,
            repository_root=repository_root,
            sensitive_values=sensitive_values,
            reason_code=reason_code,
            failed_stage=failed_stage,
        )
    except Exception:
        return False
    return True


def _reconcile_with_diagnostics(
    runtime_root: Path,
    context: ProductionExecutionContext,
    identity: ProdAttemptIdentity,
    *,
    credit_guard: ProductionCreditGuard,
    recorder: PhaseGDiagnosticRecorder,
    progress: PhaseGExecutionProgress,
    repository_root: Path,
    sensitive_values: tuple[str, ...] = (),
) -> ProdReconciliationResult:
    progress.start("SAME_ATTEMPT_RECONCILIATION")
    try:
        result = reconcile_prod_same_attempt(
            runtime_root, context, identity, credit_guard=credit_guard
        )
    except Exception as exc:
        _record_phase_g_failure(
            recorder,
            progress,
            exc,
            repository_root=repository_root,
            reason_code="PHASE_G_POST_CLICK_RECONCILIATION_FAILED",
            failed_stage="SAME_ATTEMPT_RECONCILIATION",
            sensitive_values=sensitive_values,
        )
        raise
    progress.complete("SAME_ATTEMPT_RECONCILIATION")
    return result


def is_non_subscriber_peak_window(now: datetime) -> bool:
    """Google warns non-subscribers may be unable to generate videos from 14–17 UTC."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("peak-window check requires an aware timestamp")
    utc = now.astimezone(UTC)
    return time(14) <= utc.time().replace(tzinfo=None) < time(17)


def _unique(rows: list[dict[str, object]], key: str, value: str, label: str) -> dict[str, object]:
    matches = [row for row in rows if row.get(key) == value]
    if len(matches) != 1:
        raise LegacyReplacementOperatorError(f"{label} must resolve exactly once")
    return matches[0]


def _require_parent_job_linkage(attempt: Mapping[str, object]) -> tuple[str, str]:
    """Require both persisted linkage fields before authorizing any replacement work."""
    production_job_id = attempt.get("production_job_id")
    scene_job_id = attempt.get("scene_job_id")
    if (
        not isinstance(production_job_id, str)
        or not re.fullmatch(r"PRD_[0-9]{6}", production_job_id)
        or not isinstance(scene_job_id, str)
        or not re.fullmatch(r"SJB_[0-9]{6}", scene_job_id)
    ):
        raise LegacyParentJobLinkageError("legacy D2 parent job linkage is incomplete")
    return production_job_id, scene_job_id


def _snapshot_tables(snapshot: GoogleSheetsSnapshot) -> dict[str, list[dict[str, object]]]:
    if (
        snapshot.source != SOURCE_LIVE_GOOGLE_SHEETS
        or snapshot.spreadsheet_id != APPROVED_SPREADSHEET_ID
    ):
        raise LegacyReplacementOperatorError("canonical live Sheets snapshot is not trusted")
    return {name: [dict(row) for row in rows] for name, rows in snapshot.tables.items()}


def _quote_config(tables: Mapping[str, list[dict[str, object]]]) -> ReplacementGenerationConfig:
    account = _unique(
        tables.get("PROVIDER_ACCOUNT_MASTER", []),
        "Provider_Account_ID",
        _ACCOUNT_ID,
        "PROD account",
    )
    profile = _unique(
        tables.get("GENERATION_PROFILE_MASTER", []),
        "Generation_Profile_ID",
        _PROFILE_ID,
        "active profile",
    )
    model = _unique(tables.get("MODEL_MASTER", []), "Model_ID", _MODEL_ID, "active model")
    if (
        account.get("Provider_ID") != _PROVIDER_ID
        or account.get("Environment") != "PROD"
        or account.get("Status") != "ACTIVE"
        or account.get("Auth_Profile_Key") != _AUTH_PROFILE
        or account.get("Execution_Target") != "WEB_UI"
        or account.get("Base_URL") != "https://flow.google.com/"
        or account.get("Provider_Project_ID") != _PROJECT_ID
        or profile.get("Provider_Account_ID") != _ACCOUNT_ID
        or profile.get("Environment") != "PROD"
        or profile.get("Status") != "ACTIVE"
        or profile.get("Model_ID") != _MODEL_ID
        or profile.get("Generation_Mode") != "TEXT_TO_VIDEO"
        or model.get("Provider_ID") != _PROVIDER_ID
        or model.get("Status") != "ACTIVE"
        or re.sub(r"[^a-z0-9]+", "", str(model.get("Model_Name", "")).casefold()) != "veo31lite"
    ):
        raise LegacyReplacementOperatorError("active account, model, or profile identity differs")
    return ReplacementGenerationConfig(
        provider_id=_PROVIDER_ID,
        provider_account_id=_ACCOUNT_ID,
        model_id=_MODEL_ID,
        generation_profile_id=_PROFILE_ID,
        generation_mode="TEXT_TO_VIDEO",
        pricing_variant="STANDARD_LITE",
        account_pricing_class="NON_ULTRA",
        output_count=1,
        duration_sec=8,
        aspect_ratio="9:16",
        resolution=None,
        audio_mode=None,
        quality_tier=None,
    )


def _quote(
    snapshot: GoogleSheetsSnapshot, tables: Mapping[str, list[dict[str, object]]]
) -> ReplacementQuote:
    try:
        when = datetime.fromisoformat(snapshot.read_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LegacyReplacementOperatorError("live snapshot timestamp is invalid") from exc
    if when.tzinfo is None:
        raise LegacyReplacementOperatorError("live snapshot timestamp has no timezone")
    return quote_generation_reservation(_quote_config(tables), snapshot=snapshot, quoted_at=when)


def _read_json(path: Path, runtime_root: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise LegacyReplacementOperatorError("required production ledger is unavailable")
    try:
        if not path.resolve(strict=True).is_relative_to(runtime_root.resolve(strict=True)):
            raise LegacyReplacementOperatorError("production ledger escaped runtime root")
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LegacyReplacementOperatorError("production ledger is unreadable") from exc
    if not isinstance(value, dict):
        raise LegacyReplacementOperatorError("production ledger has an invalid shape")
    return cast(dict[str, object], value)


def _read_balance(expected_email: str) -> Decimal:
    try:
        result = asyncio.run(inspect_profile(_CREDITS_PROFILE))
    except Exception as exc:
        raise LegacyReplacementOperatorError("profile-bound Flow balance read failed") from exc
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
        raise LegacyReplacementOperatorError(
            "profile-bound Flow balance or account identity differs"
        )
    return Decimal(credits)


def _parent_credit_state(
    runtime_root: Path, parent_attempt_id: str
) -> tuple[Decimal, dict[str, object], dict[str, object]]:
    ledger_path = runtime_root / "production-credit" / "shared-ledger.json"
    payload = _read_json(ledger_path, runtime_root)
    rows_value = payload.get("attempts")
    if not isinstance(rows_value, list) or any(not isinstance(row, dict) for row in rows_value):
        raise LegacyReplacementOperatorError("shared credit ledger has an invalid attempts list")
    rows = cast(list[dict[str, object]], rows_value)
    matches = [row for row in rows if row.get("attempt_id") == parent_attempt_id]
    if len(matches) != 1:
        raise LegacyReplacementOperatorError("legacy reservation must resolve exactly once")
    row = matches[0]
    try:
        exposure = Decimal(str(row.get("reserved_credits")))
    except Exception as exc:
        raise LegacyReplacementOperatorError("legacy reservation amount is invalid") from exc
    if (
        row.get("provider_id") != _PROVIDER_ID
        or row.get("provider_account_id") != _ACCOUNT_ID
        or row.get("environment") != "PROD"
        or row.get("state") != "UNKNOWN_SUBMISSION_STATE"
        or not exposure.is_finite()
        or exposure != _MAX_CREDITS
        or row.get("generate_click_count") != 1
    ):
        raise LegacyReplacementOperatorError("legacy 50-credit UNKNOWN reservation changed")
    return exposure, row, payload


def _legacy_parent(
    snapshot: GoogleSheetsSnapshot,
    runtime_root: Path,
    parent_attempt_id: str,
    *,
    software_commit: str,
) -> tuple[
    LegacyProdReconciliationDecision, dict[str, object], dict[str, object], dict[str, object]
]:
    sources = build_legacy_sources_from_snapshot(snapshot, runtime_root, runtime_namespace="PROD")
    envelope = build_legacy_prod_attempt_envelope(sources, parent_attempt_id)
    if not legacy_recovery_prerequisite_satisfied(envelope):
        raise LegacyReplacementOperatorError(
            "legacy parent no longer satisfies the frozen prerequisite"
        )
    now = format_utc_timestamp(datetime.now(UTC), timespec="seconds")
    late = reconcile_legacy_prod_same_attempt(
        sources,
        parent_attempt_id,
        observed_at=now,
        software_commit=software_commit,
        evidence=None,
    )
    if late.result.value != "AMBIGUOUS_AFTER_RECONCILIATION":
        raise LegacyReplacementOperatorError("late parent evidence changed; replacement is blocked")
    policy_id = ambiguous_legacy_policy_id(parent_attempt_id)
    policy_path = runtime_root / "production" / "phase-g" / POLICY_AUDIT_NAME
    policy = existing_policy_event(policy_path, event_type="POLICY_APPLIED", policy_id=policy_id)
    if not isinstance(policy, dict):
        raise LegacyReplacementOperatorError("applied parent quarantine policy is missing")
    reconciliation_id = policy.get("reconciliation_audit_sha256")
    if not isinstance(reconciliation_id, str) or not re.fullmatch(
        r"[0-9a-f]{64}", reconciliation_id
    ):
        raise LegacyReplacementOperatorError("policy-linked legacy reconciliation audit is invalid")
    audit = find_legacy_prod_reconciliation_audit(runtime_root, reconciliation_id)
    if not isinstance(audit, dict) or audit.get("result") != "AMBIGUOUS_AFTER_RECONCILIATION":
        raise LegacyReplacementOperatorError("policy-linked legacy reconciliation changed")
    return late, policy, audit, {"policy_id": policy_id, "reconciliation_id": reconciliation_id}


def _assert_parent_quarantined(
    tables: Mapping[str, list[dict[str, object]]],
    parent_production_job_id: str,
    parent_scene_job_id: str,
) -> tuple[dict[str, object], dict[str, object]]:
    parent_prod = _unique(
        tables.get("PRODUCTION_JOBS", []),
        "Production_Job_ID",
        parent_production_job_id,
        "parent Production Job",
    )
    parent_scene = _unique(
        tables.get("SCENE_JOBS", []),
        "Scene_Job_ID",
        parent_scene_job_id,
        "parent Scene Job",
    )
    if (
        parent_prod.get("Job_Status") != "QUARANTINED"
        or parent_scene.get("Job_Status") != "QUARANTINED"
        or parent_scene.get("Production_Job_ID") != parent_production_job_id
    ):
        raise LegacyReplacementOperatorError(
            "exact legacy parent Production/Scene Job pair is not quarantined"
        )
    return parent_prod, parent_scene


def _workspace(temp_root: Path, scene_job: SceneJob) -> JobWorkspace:
    root = temp_root / scene_job.scene_job_id
    if root.exists():
        if root.is_symlink() or not root.is_dir():
            raise LegacyReplacementOperatorError("replacement workspace is unsafe")
        workspace = JobWorkspace(
            scene_job.scene_job_id,
            root,
            {
                name: root / name
                for name in ("input", "generated", "audio", "ffmpeg", "validated", "logs")
            },
        )
        validate_workspace(temp_root, workspace)
        return workspace
    if scene_job.job_status != "PENDING":
        raise LegacyReplacementOperatorError("replacement Scene Job has no recoverable workspace")
    return create_workspace(temp_root, scene_job.scene_job_id)


def _daily_credit_ready(payload: Mapping[str, object], *, quote: Decimal, now: datetime) -> bool:
    bangkok_date = now.astimezone(timezone(timedelta(hours=7))).date().isoformat()
    rows_value = payload.get("attempts")
    if not isinstance(rows_value, list) or any(not isinstance(row, dict) for row in rows_value):
        return False
    rows = cast(list[dict[str, object]], rows_value)
    relevant = [
        row
        for row in rows
        if row.get("provider_id") == _PROVIDER_ID and row.get("date") == bangkok_date
    ]
    if any(
        row.get("state")
        not in {"SUCCESS", "PROVIDER_REJECTED", "FAILED_CONFIRMED", "WAF_REJECTION"}
        for row in relevant
    ):
        return False
    if any(row.get("state") in {"SUCCESS", "WAF_REJECTION"} for row in relevant):
        return False
    try:
        reserved = sum(
            (Decimal(str(row.get("reserved_credits", "0"))) for row in relevant), Decimal(0)
        )
    except Exception:
        return False
    return reserved + quote <= _MAX_CREDITS


def _replacement_attempt_count(attempts: list[Mapping[str, object]], parent_id: str) -> int:
    count = 0
    for row in attempts:
        replacement = row.get("legacy_replacement")
        if (
            isinstance(replacement, Mapping)
            and cast(Mapping[str, object], replacement).get("parent_attempt_id") == parent_id
        ):
            count += 1
    return count


def _attempt(ledger: D2DailyCreditLedger, attempt_id: str) -> Mapping[str, object] | None:
    rows = [row for row in ledger.attempts() if row.get("d2_run_id") == attempt_id]
    if len(rows) > 1:
        raise LegacyReplacementOperatorError("replacement D2 identity is duplicated")
    return rows[0] if rows else None


def _action_count(attempt: Mapping[str, object], key: str) -> int:
    value = attempt.get(key, 0)
    if type(value) is not int or value < 0:
        raise LegacyReplacementOperatorError("persisted action count is invalid")
    return value


def _replacement_reservation(
    runtime_root: Path, attempt_id: str, authorization_id: str, quote: Decimal
) -> dict[str, object]:
    payload = _read_json(runtime_root / "production-credit" / "shared-ledger.json", runtime_root)
    rows_value = payload.get("attempts")
    if not isinstance(rows_value, list) or any(not isinstance(row, dict) for row in rows_value):
        raise LegacyReplacementOperatorError("shared credit ledger has an invalid attempts list")
    matches = [
        row
        for row in cast(list[dict[str, object]], rows_value)
        if row.get("attempt_id") == attempt_id
    ]
    if len(matches) != 1:
        raise LegacyReplacementOperatorError("replacement reservation must resolve exactly once")
    row = matches[0]
    if (
        row.get("legacy_replacement_authorization_id") != authorization_id
        or row.get("quoted_credits") != str(quote)
        or row.get("reserved_credits") != str(quote)
    ):
        raise LegacyReplacementOperatorError("replacement reservation binding or amount differs")
    return row


def _attempt_report(attempt: Mapping[str, object]) -> dict[str, object]:
    return {
        "attempt_state": attempt.get("submission_state"),
        "confirmation_id": attempt.get("confirmation_id")
        or attempt.get("observed_confirmation_id"),
        "provider_execution_id": attempt.get("provider_execution_id"),
        "provider_media_id": attempt.get("provider_media_id"),
        "provider_media_ids": attempt.get("provider_media_ids"),
        "provider_credits_consumed": attempt.get("accounted_credit_cost"),
        "provider_submit_rpc_observed": attempt.get("provider_submit_rpc_observed"),
        "provider_submit_rpc_id": attempt.get("provider_submit_rpc_id"),
        "provider_submit_http_method": attempt.get("provider_submit_http_method"),
        "provider_submit_request_count": attempt.get("provider_submit_request_count"),
        "provider_submit_request_failed": attempt.get("provider_submit_request_failed"),
        "provider_submit_response_observed": attempt.get("provider_submit_response_observed"),
        "provider_submit_response_status": attempt.get("provider_submit_response_status"),
        "provider_submit_generation_record_observed": attempt.get(
            "provider_submit_generation_record_observed"
        ),
        "provider_submit_refusal_detected": attempt.get("provider_submit_refusal_detected"),
        "provider_submit_refusal_code": attempt.get("provider_submit_refusal_code"),
    }


def _diagnose_existing_preclick(
    *,
    runtime_root: Path,
    temp_root: Path,
    tables: dict[str, list[dict[str, object]]],
    quote: ReplacementQuote,
    expected_email: str,
    parent_attempt_id: str,
    authorization_id: str,
    attempt_id: str,
    production_job_id: str,
    scene_job_id: str,
    credit_balance: Decimal,
) -> dict[str, object]:
    """Validate one existing replacement and run only Phase G's read-only prefix."""
    expected_ids = (
        parent_attempt_id == _PARENT_ATTEMPT_ID
        and expected_email.casefold() == _EXPECTED_EMAIL.casefold()
        and authorization_id == "LRA_18A2C0EE3248A7C93E6D1991"
        and attempt_id == "D2_9c13df47d64c68de30cd036bcd1fec09"
        and production_job_id == "PRD_000002"
        and scene_job_id == "SJB_000002"
    )
    if not expected_ids:
        raise LegacyReplacementOperatorError("exact existing diagnostic identity is required")
    if quote.status is not ReplacementQuoteStatus.VALID or quote.amount is None:
        raise LegacyReplacementOperatorError("fresh replacement quote is not valid")

    ledger = D2DailyCreditLedger(runtime_root / "production")
    attempts = list(ledger.attempts())
    parent_rows = [row for row in attempts if row.get("d2_run_id") == parent_attempt_id]
    if len(parent_rows) != 1:
        raise LegacyReplacementOperatorError("legacy parent D2 attempt must resolve exactly once")
    parent_production_job_id, parent_scene_job_id = _require_parent_job_linkage(parent_rows[0])
    _assert_parent_quarantined(tables, parent_production_job_id, parent_scene_job_id)

    shared_payload = _read_json(
        runtime_root / "production-credit" / "shared-ledger.json", runtime_root
    )
    shared_rows_value = shared_payload.get("attempts")
    if not isinstance(shared_rows_value, list) or any(
        not isinstance(row, dict) for row in shared_rows_value
    ):
        raise LegacyReplacementOperatorError("shared credit ledger has an invalid attempts list")
    shared_rows = cast(list[dict[str, object]], shared_rows_value)
    if any(row.get("attempt_id") == attempt_id for row in shared_rows):
        raise LegacyReplacementOperatorError(
            "existing replacement already has a credit reservation"
        )

    payload = _read_json(ledger.path, runtime_root)
    auth_value = payload.get("legacy_replacement_authorizations")
    if not isinstance(auth_value, list) or any(not isinstance(row, dict) for row in auth_value):
        raise LegacyReplacementOperatorError("replacement authorization ledger is invalid")
    parent_authorizations = [
        row
        for row in cast(list[dict[str, object]], auth_value)
        if row.get("parent_attempt_id") == parent_attempt_id
    ]
    if len(parent_authorizations) != 1:
        raise LegacyReplacementOperatorError("one existing parent authorization is required")
    authorization = parent_authorizations[0]
    if (
        authorization.get("authorization_id") != authorization_id
        or authorization.get("authorization_status") != "CONSUMED"
        or authorization.get("replacement_created") is not True
        or authorization.get("replacement_attempt_id") != attempt_id
        or authorization.get("production_job_id") != parent_production_job_id
        or authorization.get("scene_job_id") != parent_scene_job_id
        or authorization.get("replacement_production_job_id") != production_job_id
        or authorization.get("replacement_scene_job_id") != scene_job_id
        or authorization.get("replacement_quote") != str(quote.amount)
        or authorization.get("replacement_quote_unit") != quote.unit
        or authorization.get("replacement_quote_source") != quote.source
        or authorization.get("replacement_quote_source_version") != quote.source_version
        or authorization.get("replacement_quote_input_digest") != quote.input_digest
    ):
        raise LegacyReplacementOperatorError("consumed authorization differs from fresh lineage")

    replacement_rows = [
        row
        for row in attempts
        if isinstance(row.get("legacy_replacement"), Mapping)
        and cast(Mapping[str, object], row["legacy_replacement"]).get("parent_attempt_id")
        == parent_attempt_id
    ]
    matching_attempts = [row for row in attempts if row.get("d2_run_id") == attempt_id]
    if (
        len(replacement_rows) != 1
        or len(matching_attempts) != 1
        or replacement_rows[0].get("d2_run_id") != attempt_id
    ):
        raise LegacyReplacementOperatorError("one exact existing replacement attempt is required")
    attempt = matching_attempts[0]
    if (
        attempt.get("submission_state") != "PREPARED"
        or attempt.get("generate_action_count") != 0
        or attempt.get("approval_action_count") != 0
        or attempt.get("generate_action_at") is not None
        or attempt.get("provider_submit_request_count") != 0
        or attempt.get("provider_submit_rpc_observed") is not None
        or attempt.get("provider_execution_id") is not None
        or attempt.get("provider_media_id") is not None
        or attempt.get("provider_media_ids") not in ([], None)
        or attempt.get("confirmation_id") is not None
        or attempt.get("accounted_credit_cost") != "0"
    ):
        raise LegacyReplacementOperatorError(
            "existing replacement is not a zero-action PREPARED attempt"
        )

    production = _unique(
        tables.get("PRODUCTION_JOBS", []),
        "Production_Job_ID",
        production_job_id,
        "replacement Production Job",
    )
    scene_job_row = _unique(
        tables.get("SCENE_JOBS", []), "Scene_Job_ID", scene_job_id, "replacement Scene Job"
    )
    lineage_value = attempt.get("legacy_replacement")
    if not isinstance(lineage_value, Mapping):
        raise LegacyReplacementOperatorError("replacement lineage is missing")
    if (
        production.get("Execution_Environment") != "PROD"
        or production.get("Request_Idempotency_Key")
        != authorization.get("replacement_request_idempotency_key")
        or production.get("Job_Status") != "REQUESTED"
        or scene_job_row.get("Production_Job_ID") != production_job_id
        or scene_job_row.get("Scene_Prompt_ID") != attempt.get("scene_prompt_id")
        or scene_job_row.get("Job_Status") != "PENDING"
        or attempt.get("production_job_id") != production_job_id
        or attempt.get("scene_job_id") != scene_job_id
        or lineage_value.get("authorization_id") != authorization_id
        or lineage_value.get("parent_attempt_id") != parent_attempt_id
    ):
        raise LegacyReplacementOperatorError("existing replacement job and attempt lineage differ")

    parent_exposure, parent_credit_row, _ = _parent_credit_state(runtime_root, parent_attempt_id)
    if (
        parent_credit_row.get("state") != "UNKNOWN_SUBMISSION_STATE"
        or parent_exposure != Decimal("50")
        or parent_rows[0].get("submission_state") != "UNKNOWN_SUBMISSION_STATE"
        or parent_rows[0].get("generate_action_count") != 1
    ):
        raise LegacyReplacementOperatorError("legacy parent reservation changed")

    prompt_id = str(attempt.get("scene_prompt_id", ""))
    prompt_sha = str(attempt.get("prompt_sha256", ""))
    context = ProductionExecutionContext(
        environment="PROD",
        provider_id=_PROVIDER_ID,
        provider_account_id=_ACCOUNT_ID,
        provider_project_id=_PROJECT_ID,
        auth_profile=_AUTH_PROFILE,
        scene_id=_SCENE_ID,
        generation_profile_id=_PROFILE_ID,
        scene_prompt_id=prompt_id,
        attempt_id=attempt_id,
        credit_budget_context=CreditBudgetContext(provider_remaining_free=credit_balance),
        production_job_id=production_job_id,
        scene_job_id=scene_job_id,
        legacy_replacement_authorization_id=authorization_id,
        legacy_replacement_parent_attempt_id=parent_attempt_id,
        legacy_replacement_quote_credits=quote.amount,
        legacy_replacement_quote_input_digest=quote.input_digest,
        legacy_replacement_quote_source_version=quote.source_version,
    )
    request = build_phase_g_request(
        cast(dict[str, list[Row]], tables),
        cast(list[Row], tables.get("SCENE_PROMPT_VERSION", [])),
        d2_run_id=attempt_id,
        runtime_state_root=runtime_root,
        temp_root=temp_root / scene_job_id / "generated",
        execution_context=context,
    )
    attempt_no_value = scene_job_row.get("Attempt_No")
    if isinstance(attempt_no_value, bool):
        raise LegacyReplacementOperatorError("replacement Scene Job attempt number is invalid")
    if isinstance(attempt_no_value, int):
        attempt_no = attempt_no_value
    elif isinstance(attempt_no_value, str) and attempt_no_value.isdecimal():
        attempt_no = int(attempt_no_value)
    else:
        raise LegacyReplacementOperatorError("replacement Scene Job attempt number is invalid")
    exact_scene_job = SceneJob(
        scene_job_id=scene_job_id,
        production_job_id=production_job_id,
        scene_prompt_id=prompt_id,
        attempt_no=attempt_no,
        job_status=str(scene_job_row.get("Job_Status", "")),
    )
    if (
        scene_phase_g_run_id(exact_scene_job) != attempt_id
        or request.scene_prompt_id != prompt_id
        or request.prompt_sha256 != prompt_sha
        or request.output_count != 1
        or request.duration_sec != 8
        or request.aspect_ratio != "9:16"
        or request.model_id != _MODEL_ID
        or request.generation_mode != "TEXT_TO_VIDEO"
    ):
        raise LegacyReplacementOperatorError("rebuilt diagnostic request differs from the attempt")

    recorder = PhaseGDiagnosticRecorder(
        runtime_root,
        attempt_id=attempt_id,
        authorization_id=authorization_id,
        production_job_id=production_job_id,
        scene_job_id=scene_job_id,
    )
    progress = PhaseGExecutionProgress(recorder=recorder, strict=True)
    try:
        diagnostic = asyncio.run(
            diagnose_preclick(request, expected_email=expected_email, progress=progress)
        )
        recorder.record_diagnostic_complete(progress)
    except Exception as exc:
        try:
            failure = recorder.record_failure(
                exc,
                progress,
                repository_root=Path(__file__).resolve().parents[2],
                sensitive_values=(request.compiled_prompt_text, request.compiled_negative_prompt),
            )
        except Exception as persist_exc:
            failure = {
                "diagnostics_persisted": False,
                "diagnostic_write_error": type(persist_exc).__name__,
            }
        return {
            "status": "HOLD",
            "attempt_id": attempt_id,
            "authorization_id": authorization_id,
            "production_job_id": production_job_id,
            "scene_job_id": scene_job_id,
            "failed_stage": failure.get("failed_stage"),
            "last_completed_stage": failure.get("last_completed_stage"),
            "failure_class": "PHASE_G_EXECUTION_FAILED_BEFORE_CLICK",
            "reason_code": "PHASE_G_EXECUTION_FAILED_BEFORE_CLICK",
            "preclick_diagnostic": failure,
            "diagnostic_path": str(recorder.path),
            "generate_action_count": 0,
            "provider_submit_request_count": 0,
            "reservation_created": False,
            "flow_state_changed": False,
        }
    return {
        "status": "PRECLICK_DIAGNOSTIC_COMPLETE",
        "attempt_id": diagnostic.attempt_id,
        "authorization_id": authorization_id,
        "production_job_id": production_job_id,
        "scene_job_id": scene_job_id,
        "failed_stage": None,
        "last_completed_stage": diagnostic.last_completed_stage,
        "stopped_before_stage": diagnostic.stopped_before_stage,
        "diagnostic_path": str(recorder.path),
        "generate_action_count": 0,
        "provider_submit_request_count": 0,
        "reservation_created": False,
        "flow_state_changed": False,
    }


def _download_validated_existing(
    request: PhaseGExecutionRequest,
    *,
    media_id: str,
    expected_email: str,
    ledger: D2DailyCreditLedger,
) -> tuple[Path, ValidatedVideo]:
    async def download() -> tuple[Path, ValidatedVideo]:
        port = await _open_generation_port(request, expected_email=expected_email)
        try:
            recovered = await port.download_exact_media(
                request.presubmit_request.temp_root, media_id
            )
            video = validate_video(
                recovered.path,
                expected_duration_sec=request.duration_sec,
                expected_aspect_ratio=request.aspect_ratio,
            )
            ledger.record_local_artifact_validated(
                request.d2_run_id,
                path=recovered.path,
                size_bytes=video.size_bytes,
                sha256=video.sha256,
            )
            return recovered.path, video
        finally:
            await port.close()

    return asyncio.run(download())


def run_once(
    *,
    scene_id: str,
    profile_id: str,
    account_id: str,
    project_id: str,
    expected_email: str,
    parent_attempt_id: str,
    execute: bool,
    diagnose_preclick_mode: bool = False,
    authorization_id: str | None = None,
    replacement_attempt_id: str | None = None,
    replacement_production_job_id: str | None = None,
    replacement_scene_job_id: str | None = None,
) -> dict[str, object]:
    if execute and diagnose_preclick_mode:
        raise LegacyReplacementOperatorError("execution and pre-click diagnosis are exclusive")
    if (scene_id, profile_id, account_id, project_id, expected_email, parent_attempt_id) != (
        _SCENE_ID,
        _PROFILE_ID,
        _ACCOUNT_ID,
        _PROJECT_ID,
        _EXPECTED_EMAIL,
        _PARENT_ATTEMPT_ID,
    ) or str(UUID(project_id)) != project_id:
        raise LegacyReplacementOperatorError("explicit exact PROD replacement identity is required")
    settings = load_runtime_settings()
    if settings.environment != "production" or settings.storage_provider != "google-drive":
        raise LegacyReplacementOperatorError("production runtime settings are required")
    credential_value = os.environ.get("GFLOW_GOOGLE_WORKSPACE_CREDENTIALS", "")
    if not credential_value:
        raise LegacyReplacementOperatorError("Google Workspace credentials are not configured")
    repository_root = Path(__file__).resolve().parents[2]
    software_commit = _software_commit(repository_root)
    credential_path = Path(credential_value)
    token_provider = GoogleWorkspaceCredentialProvider(
        credential_path, repository_root=repository_root
    )
    reader = GoogleSheetsApiTransport(token_provider)
    writer_transport = GoogleSheetsTransactionApiTransport(token_provider)
    try:
        snapshot_source = GoogleSheetsSnapshotSource(reader, repository_root / "schemas")
        snapshot = snapshot_source.read()
        tables = _snapshot_tables(snapshot)
        scene = _unique(tables.get("SCENES", []), "Scene_ID", scene_id, "Scene")
        approved_prompts = [
            row
            for row in tables.get("SCENE_PROMPT_VERSION", [])
            if row.get("Prompt_Status") == "APPROVED"
            and row.get("Scene_ID") == scene_id
            and row.get("Generation_Profile_ID") == profile_id
        ]
        if len(approved_prompts) != 1:
            raise LegacyReplacementOperatorError("one current approved Scene prompt is required")
        prompt = approved_prompts[0]
        prompt_id = str(prompt.get("Scene_Prompt_ID", ""))
        prompt_sha = str(prompt.get("Prompt_SHA256", ""))
        if not prompt_id or not re.fullmatch(r"[0-9a-f]{64}", prompt_sha):
            raise LegacyReplacementOperatorError("approved prompt identity is invalid")
        if scene.get("Dialogue_Mode") != "NONE":
            raise LegacyReplacementOperatorError(
                "replacement operator is limited to the approved no-audio scene"
            )
        if any(
            row.get("Scene_ID") == scene_id and str(row.get("Status", "")).upper() == "ACTIVE"
            for row in tables.get("SCENE_DIALOGUE_LINES", [])
        ):
            raise LegacyReplacementOperatorError("no-audio Scene has active dialogue rows")
        preflight = preflight_scene(
            InMemorySceneCatalog(cast(dict[str, list[Row]], tables)),
            scene_id,
            profile_id,
            "PROD",
        )
        if (
            preflight.status != "PREFLIGHT_PASS"
            or not preflight.ready_for_provider_adapter
            or preflight.references
        ):
            raise LegacyReplacementOperatorError(
                "fresh Phase E did not pass the replacement contract"
            )
        quote = _quote(snapshot, tables)
        if quote.status is not ReplacementQuoteStatus.VALID or quote.amount is None:
            return {
                "status": "HOLD",
                "quote_status": quote.status.value,
                "reason": quote.reason,
                "generate_clicked": False,
            }
        runtime_root = settings.runtime_state_root
        exposure, parent_credit_row, shared_payload = _parent_credit_state(
            runtime_root, parent_attempt_id
        )
        credit_balance = _read_balance(expected_email)
        if quote.amount > _MAX_CREDITS or quote.amount > credit_balance:
            raise LegacyReplacementOperatorError(
                "current quote exceeds the authorized cap or free balance"
            )
        result: dict[str, object] = {
            "status": "QUOTE_VALID",
            "parent_attempt_id": parent_attempt_id,
            "provider_id": _PROVIDER_ID,
            "provider_account_id": account_id,
            "provider_project_id": project_id,
            "model_id": _MODEL_ID,
            "official_model_name": _MODEL_DISPLAY_NAMES[_MODEL_ID],
            "generation_profile_id": profile_id,
            "scene_id": scene_id,
            "scene_prompt_id": prompt_id,
            "prompt_sha256": prompt_sha,
            "pricing_variant": "STANDARD_LITE",
            "account_pricing_class": "NON_ULTRA",
            "peak_window_advisory": is_non_subscriber_peak_window(datetime.now(UTC)),
            "generation_mode": "TEXT_TO_VIDEO",
            "duration_intent_sec": 8,
            "aspect_ratio_intent": "9:16",
            "output_count": 1,
            "quote_amount": str(quote.amount),
            "quote_unit": quote.unit,
            "quote_source": quote.source,
            "quote_source_version": quote.source_version,
            "quote_input_digest": quote.input_digest,
            "quote_at": quote.quoted_at,
            "matched_cost_row_id": quote.source_version.split(":", 1)[0]
            if quote.source_version
            else None,
            "existing_unresolved_exposure": str(exposure),
            "maximum_combined_exposure": str(exposure + quote.amount),
            "provider_credits_available": str(credit_balance),
            "generate_clicked": False,
            "attempt_created": False,
            "reservation_created": False,
        }
        if diagnose_preclick_mode:
            if not all(
                (
                    authorization_id,
                    replacement_attempt_id,
                    replacement_production_job_id,
                    replacement_scene_job_id,
                )
            ):
                raise LegacyReplacementOperatorError(
                    "diagnostic mode requires the exact existing authorization, attempt, and jobs"
                )
            assert authorization_id is not None
            assert replacement_attempt_id is not None
            assert replacement_production_job_id is not None
            assert replacement_scene_job_id is not None
            diagnostic_result = _diagnose_existing_preclick(
                runtime_root=runtime_root,
                temp_root=settings.temp_root,
                tables=tables,
                quote=quote,
                expected_email=expected_email,
                parent_attempt_id=parent_attempt_id,
                authorization_id=authorization_id,
                attempt_id=replacement_attempt_id,
                production_job_id=replacement_production_job_id,
                scene_job_id=replacement_scene_job_id,
                credit_balance=credit_balance,
            )
            result.update(diagnostic_result)
            return result
        if not execute:
            return result
        now = datetime.now(UTC)
        d2_ledger = D2DailyCreditLedger(runtime_root / "production")
        all_attempts = list(d2_ledger.attempts())
        parent_attempts = [row for row in all_attempts if row.get("d2_run_id") == parent_attempt_id]
        if len(parent_attempts) != 1:
            raise LegacyReplacementOperatorError("legacy D2 parent must resolve exactly once")
        parent_d2 = parent_attempts[0]
        parent_prod_id, parent_scene_id = _require_parent_job_linkage(parent_d2)
        parent_production, parent_scene_job = _assert_parent_quarantined(
            tables, parent_prod_id, parent_scene_id
        )
        ledger_payload = _read_json(d2_ledger.path, runtime_root)
        auth_value = ledger_payload.get("legacy_replacement_authorizations", [])
        if not isinstance(auth_value, list) or any(not isinstance(row, dict) for row in auth_value):
            raise LegacyReplacementOperatorError("replacement authorization ledger is invalid")
        authorizations = cast(list[dict[str, object]], auth_value)
        parent_authorizations = [
            row for row in authorizations if row.get("parent_attempt_id") == parent_attempt_id
        ]
        existing_replacement_count = _replacement_attempt_count(all_attempts, parent_attempt_id)
        if len(parent_authorizations) > 1:
            raise LegacyReplacementOperatorError(
                "multiple replacement authorizations exist for parent"
            )
        existing_auth = parent_authorizations[0] if parent_authorizations else None
        parent_quarantined = (
            parent_production.get("Job_Status") == "QUARANTINED"
            and parent_scene_job.get("Job_Status") == "QUARANTINED"
            and parent_credit_row.get("state") == "UNKNOWN_SUBMISSION_STATE"
            and parent_d2.get("submission_state") == "UNKNOWN_SUBMISSION_STATE"
            and parent_d2.get("generate_action_count") == 1
        )
        if not parent_quarantined:
            raise LegacyReplacementOperatorError(
                "legacy parent quarantine or UNKNOWN state changed"
            )
        decision: ReplacementDecision | None = None
        if existing_auth is not None:
            authorization_id = str(existing_auth.get("authorization_id", ""))
            policy_link = {
                "policy_id": existing_auth.get("policy_id"),
                "reconciliation_id": existing_auth.get("reconciliation_audit_id"),
            }
            decision_digest = str(existing_auth.get("decision_input_digest", ""))
            try:
                authorized_exposure = Decimal(str(existing_auth.get("legacy_exposure")))
            except Exception as exc:
                raise LegacyReplacementOperatorError(
                    "persisted replacement exposure is invalid"
                ) from exc
            expected_key = legacy_replacement_request_idempotency_key(
                parent_attempt_id=parent_attempt_id,
                policy_id=str(policy_link["policy_id"]),
                reconciliation_audit_id=str(policy_link["reconciliation_id"]),
                decision_input_digest=decision_digest,
            )
            consumed = existing_auth.get("authorization_status") == "CONSUMED"
            if (
                existing_auth.get("contract_version")
                != "D2_PROD_LEGACY_REPLACEMENT_AUTHORIZATION_V2"
                or not re.fullmatch(r"LRA_[0-9A-F]{24}", authorization_id)
                or expected_key != existing_auth.get("replacement_request_idempotency_key")
                or existing_auth.get("parent_attempt_id") != parent_attempt_id
                or existing_auth.get("production_job_id") != parent_prod_id
                or existing_auth.get("scene_job_id") != parent_scene_id
                or existing_auth.get("replacement_quote") != str(quote.amount)
                or existing_auth.get("replacement_quote_unit") != quote.unit
                or existing_auth.get("replacement_quote_source") != quote.source
                or existing_auth.get("replacement_quote_source_version") != quote.source_version
                or existing_auth.get("replacement_quote_input_digest") != quote.input_digest
                or existing_auth.get("maximum_combined_exposure") != str(exposure + quote.amount)
                or authorized_exposure != exposure
                or existing_auth.get("authorized_replacement_count") != 1
                or existing_auth.get("generate_authorized") is not False
                or existing_auth.get("authorization_status") not in {"APPROVED", "CONSUMED"}
                or existing_auth.get("replacement_created") is not consumed
                or not re.fullmatch(r"[0-9a-f]{64}", decision_digest)
                or existing_replacement_count != (1 if consumed else 0)
            ):
                raise LegacyReplacementOperatorError(
                    "existing authorization is not an exact replay"
                )
            replayed_late, replayed_policy, replayed_reconciliation, replayed_policy_link = (
                _legacy_parent(
                    snapshot, runtime_root, parent_attempt_id, software_commit=software_commit
                )
            )
            if (
                replayed_late.result.value != "AMBIGUOUS_AFTER_RECONCILIATION"
                or replayed_reconciliation.get("result") != "AMBIGUOUS_AFTER_RECONCILIATION"
                or replayed_policy.get("attempt_id") != parent_attempt_id
                or replayed_policy_link != policy_link
            ):
                raise LegacyReplacementOperatorError(
                    "fresh parent evidence or policy linkage changed during replay"
                )
            idem_key = expected_key
            result.update(
                late_parent_evidence=replayed_late.result.value,
                late_parent_check_sha256=replayed_late.audit_event.get("event_sha256"),
                gate_a="PASS",
                gate_b="PASS",
                gate_c="PASS",
                gate_d="PASS (EXACT REPLAY)",
                replacement_decision="APPROVE",
                decision_input_digest=decision_digest,
                exact_replay=True,
            )
        else:
            if existing_replacement_count != 0:
                raise LegacyReplacementOperatorError(
                    "orphan replacement attempt exists without its authorization"
                )
            late, policy, reconciliation, policy_link = _legacy_parent(
                snapshot, runtime_root, parent_attempt_id, software_commit=software_commit
            )
            if (
                late.result.value != "AMBIGUOUS_AFTER_RECONCILIATION"
                or reconciliation.get("result") != "AMBIGUOUS_AFTER_RECONCILIATION"
                or policy.get("attempt_id") != parent_attempt_id
            ):
                raise LegacyReplacementOperatorError(
                    "late parent evidence or policy linkage changed"
                )
            daily_ready = _daily_credit_ready(shared_payload, quote=quote.amount, now=now)
            gate_a = parent_quarantined
            gate_b = late.result.value == "AMBIGUOUS_AFTER_RECONCILIATION"
            gate_c = quote.valid and quote.amount <= _MAX_CREDITS
            gate_d = daily_ready and credit_balance >= quote.amount
            decision = evaluate_replacement_decision(
                gate_a_pass=gate_a,
                gate_b_pass=gate_b,
                quote=quote,
                existing_unresolved_exposure=exposure,
                replacement_authorization_ready=True,
                parent_linkage_ready=bool(
                    parent_prod_id and parent_scene_id and policy and reconciliation
                ),
                parent_quarantined=parent_quarantined,
                zero_existing_replacements=existing_replacement_count == 0,
                zero_conflicting_authorizations=True,
                evidence_capture_ready=gate_d,
            )
            result.update(
                late_parent_evidence=late.result.value,
                late_parent_check_sha256=late.audit_event.get("event_sha256"),
                gate_a="PASS" if gate_a else "HOLD",
                gate_b="PASS" if gate_b else "HOLD",
                gate_c="PASS" if gate_c else "HOLD",
                gate_d="PASS" if gate_d else "HOLD",
                replacement_decision=decision.status,
                decision_input_digest=decision.input_digest,
                exact_replay=False,
            )
            if decision.status != "APPROVE" or not gate_c:
                result.update(
                    status="HOLD",
                    failed_gate=next(
                        (
                            name
                            for name, passed in (
                                ("A", gate_a),
                                ("B", gate_b),
                                ("C", gate_c),
                                ("D", gate_d),
                            )
                            if not passed
                        ),
                        "FORMAL_DECISION",
                    ),
                )
                return result
            idem_key = legacy_replacement_request_idempotency_key(
                parent_attempt_id=parent_attempt_id,
                policy_id=str(policy_link["policy_id"]),
                reconciliation_audit_id=str(policy_link["reconciliation_id"]),
                decision_input_digest=decision.input_digest,
            )
        request = ProductionRequest(
            str(scene.get("Script_ID", "")),
            "PROD",
            idem_key,
            (prompt_id,),
        )
        if not request.script_id:
            raise LegacyReplacementOperatorError("locked Scene has no Script ID")
        tx_rows = OptimisticGoogleSheetsJobStore(
            writer_transport, repository_root / "schemas", writer_lock_root=runtime_root
        )
        productions = tx_rows.read_rows("PRODUCTION_JOBS")
        scenes = tx_rows.read_rows("SCENE_JOBS")
        existing_prod = [
            row for row in productions if row.get("Request_Idempotency_Key") == idem_key
        ]
        if len(existing_prod) > 1:
            raise LegacyReplacementOperatorError("replacement idempotency key is duplicated")
        if not existing_prod and any(
            row.get("Script_ID") == request.script_id
            and row.get("Production_Job_ID") != parent_prod_id
            and str(row.get("Job_Status", "")).upper() not in {"COMPLETED", "FAILED", "CANCELLED"}
            for row in productions
        ):
            raise LegacyReplacementOperatorError("another unresolved Production Job exists")
        if not existing_prod and any(
            row.get("Scene_Prompt_ID") == prompt_id
            and row.get("Scene_Job_ID") != parent_scene_id
            and str(row.get("Job_Status", "")).upper() not in {"COMPLETED", "FAILED", "CANCELLED"}
            for row in scenes
        ):
            raise LegacyReplacementOperatorError(
                "another unresolved Scene Job exists for the prompt"
            )
        job_claim = ProductionJobCoordinator(
            GoogleSheetsProductionJobStore(tx_rows)
        ).submit_request(request, now)
        if len(job_claim.scene_jobs) != 1:
            raise LegacyReplacementOperatorError("replacement must claim exactly one Scene Job")
        scene_job = job_claim.scene_jobs[0]
        child_prod_id = job_claim.production_job.production_job_id
        child_scene_id = scene_job.scene_job_id
        if child_prod_id == parent_prod_id or child_scene_id == parent_scene_id:
            raise LegacyReplacementOperatorError("replacement jobs must be new child identities")
        if existing_auth is not None and (
            existing_auth.get("replacement_production_job_id") != child_prod_id
            or existing_auth.get("replacement_scene_job_id") != child_scene_id
        ):
            raise LegacyReplacementOperatorError(
                "authorization child job IDs differ from exact replay"
            )
        workspace = _workspace(settings.temp_root, scene_job)
        attempt_id = scene_phase_g_run_id(scene_job)
        authorization_id = idem_key.removeprefix("LEGACY_REPLACEMENT_V2_")
        if existing_auth is not None and existing_auth.get("authorization_id") != authorization_id:
            raise LegacyReplacementOperatorError("authorization ID differs from idempotency key")
        current_balance = _read_balance(expected_email)
        context = ProductionExecutionContext(
            environment="PROD",
            provider_id=_PROVIDER_ID,
            provider_account_id=account_id,
            provider_project_id=project_id,
            auth_profile=_AUTH_PROFILE,
            scene_id=scene_id,
            generation_profile_id=profile_id,
            scene_prompt_id=prompt_id,
            attempt_id=attempt_id,
            credit_budget_context=CreditBudgetContext(
                provider_remaining_free=current_balance,
                is_free_quota=True,
                paid_fallback=False,
            ),
            production_job_id=child_prod_id,
            scene_job_id=child_scene_id,
            legacy_replacement_authorization_id=authorization_id,
            legacy_replacement_parent_attempt_id=parent_attempt_id,
            legacy_replacement_quote_credits=quote.amount,
            legacy_replacement_quote_input_digest=quote.input_digest,
            legacy_replacement_quote_source_version=quote.source_version,
        )
        phase_g_request = build_phase_g_request(
            cast(dict[str, list[Row]], tables),
            cast(list[Row], tables.get("SCENE_PROMPT_VERSION", [])),
            d2_run_id=attempt_id,
            runtime_state_root=runtime_root,
            temp_root=workspace.directories["generated"],
            execution_context=context,
        )
        if (
            phase_g_request.output_count != 1
            or phase_g_request.duration_sec != 8
            or phase_g_request.aspect_ratio != "9:16"
            or phase_g_request.model_id != _MODEL_ID
            or phase_g_request.generation_mode != "TEXT_TO_VIDEO"
            or phase_g_request.prompt_sha256 != prompt_sha
        ):
            raise LegacyReplacementOperatorError(
                "Phase G replacement configuration differs from the authorized quote"
            )
        # Re-read canonical inputs before creating the durable authorization.
        refreshed = snapshot_source.read()
        refreshed_tables = _snapshot_tables(refreshed)
        refreshed_quote = _quote(refreshed, refreshed_tables)
        if (
            refreshed_quote.status is not ReplacementQuoteStatus.VALID
            or refreshed_quote.input_digest != quote.input_digest
            or refreshed_quote.source_version != quote.source_version
        ):
            raise LegacyReplacementOperatorError(
                "fresh quote changed before authorization consumption"
            )
        (
            refreshed_late,
            refreshed_policy,
            refreshed_reconciliation,
            refreshed_policy_link,
        ) = _legacy_parent(
            refreshed, runtime_root, parent_attempt_id, software_commit=software_commit
        )
        if (
            refreshed_late.result.value != "AMBIGUOUS_AFTER_RECONCILIATION"
            or refreshed_reconciliation.get("result") != "AMBIGUOUS_AFTER_RECONCILIATION"
            or refreshed_policy.get("attempt_id") != parent_attempt_id
            or refreshed_policy_link != policy_link
        ):
            raise LegacyReplacementOperatorError(
                "fresh parent evidence or policy linkage changed before authorization consumption"
            )
        result.update(
            late_parent_evidence=refreshed_late.result.value,
            late_parent_check_sha256=refreshed_late.audit_event.get("event_sha256"),
            gate_b="PASS",
        )
        refreshed_exposure, refreshed_parent_credit, _ = _parent_credit_state(
            runtime_root, parent_attempt_id
        )
        _assert_parent_quarantined(refreshed_tables, parent_prod_id, parent_scene_id)
        refreshed_parent_rows = [
            row for row in d2_ledger.attempts() if row.get("d2_run_id") == parent_attempt_id
        ]
        if (
            len(refreshed_parent_rows) != 1
            or refreshed_exposure != exposure
            or refreshed_parent_credit.get("state") != "UNKNOWN_SUBMISSION_STATE"
            or refreshed_parent_rows[0].get("submission_state") != "UNKNOWN_SUBMISSION_STATE"
        ):
            raise LegacyReplacementOperatorError(
                "legacy parent or reservation changed before authorization consumption"
            )
        if existing_auth is None:
            if decision is None:
                raise LegacyReplacementOperatorError("formal replacement decision is unavailable")
            auth_request = LegacyReplacementAuthorizationRequest(
                parent_attempt_id=parent_attempt_id,
                production_job_id=parent_prod_id,
                scene_job_id=parent_scene_id,
                replacement_production_job_id=child_prod_id,
                replacement_scene_job_id=child_scene_id,
                policy_id=str(policy_link["policy_id"]),
                reconciliation_audit_id=str(policy_link["reconciliation_id"]),
                decision=decision,
                legacy_exposure=exposure,
                created_at=format_utc_timestamp(datetime.now(UTC), timespec="seconds"),
                created_by=expected_email,
                software_commit=software_commit,
            )
            existing_auth = create_legacy_replacement_authorization(d2_ledger, auth_request)
        elif existing_auth.get("authorization_id") != authorization_id:
            raise LegacyReplacementOperatorError("existing authorization is not an exact replay")
        consumed = consume_legacy_replacement_authorization(
            d2_ledger,
            authorization_id,
            phase_g_request,
            parent_quarantined=True,
            current_quote=quote,
        )
        result.update(
            status="AUTHORIZED_AND_CONSUMED",
            authorization_id=authorization_id,
            authorization_status=d2_ledger.get_legacy_replacement_authorization(
                authorization_id
            ).get("authorization_status"),
            replacement_attempt_id=attempt_id,
            parent_production_job_id=parent_prod_id,
            parent_scene_job_id=parent_scene_id,
            replacement_production_job_id=child_prod_id,
            replacement_scene_job_id=child_scene_id,
            attempt_state=consumed.get("submission_state"),
            attempt_created=True,
            generate_clicked=False,
            reservation_created=False,
        )
        guard = ProductionCreditGuard(runtime_root)
        prior_attempt = _attempt(d2_ledger, attempt_id)
        if prior_attempt is None:
            raise LegacyReplacementOperatorError("consumed replacement D2 attempt is missing")
        prior_clicks = _action_count(prior_attempt, "generate_action_count")
        prior_approvals = _action_count(prior_attempt, "approval_action_count")
        if prior_clicks > 1 or prior_approvals > 1 or (prior_clicks == 0 and prior_approvals != 0):
            raise LegacyReplacementOperatorError("persisted replacement action count is unsafe")
        phase_g_recorder = PhaseGDiagnosticRecorder(
            runtime_root,
            attempt_id=attempt_id,
            authorization_id=authorization_id,
            production_job_id=child_prod_id,
            scene_job_id=child_scene_id,
        )
        phase_g_progress = PhaseGExecutionProgress(recorder=phase_g_recorder, strict=False)
        sensitive_values = (
            phase_g_request.compiled_prompt_text,
            phase_g_request.compiled_negative_prompt,
        )
        if prior_clicks > 0:
            attempt_identity = ProdAttemptIdentity(
                production_job_id=child_prod_id,
                scene_job_id=child_scene_id,
                d2_run_id=attempt_id,
                provider_account_id=account_id,
                provider_project_id=project_id,
                scene_id=scene_id,
                scene_prompt_id=prompt_id,
                generation_profile_id=profile_id,
                prompt_sha256=prompt_sha,
            )
            recon = _reconcile_with_diagnostics(
                runtime_root,
                context,
                attempt_identity,
                credit_guard=guard,
                recorder=phase_g_recorder,
                progress=phase_g_progress,
                repository_root=repository_root,
                sensitive_values=sensitive_values,
            )
            reconciled_attempt = _attempt(d2_ledger, attempt_id)
            if reconciled_attempt is None:
                raise LegacyReplacementOperatorError("replacement D2 evidence disappeared")
            reservation = _replacement_reservation(
                runtime_root, attempt_id, authorization_id, quote.amount
            )
            parent_after, _, _ = _parent_credit_state(runtime_root, parent_attempt_id)
            result.update(
                status="HOLD",
                reconciliation_result=recon.value,
                generate_clicked=True,
                generate_action_count=prior_clicks,
                approval_action_count=_action_count(reconciled_attempt, "approval_action_count"),
                reservation_status=reservation.get("state"),
                reservation_credits=reservation.get("reserved_credits"),
                legacy_reservation_unchanged=parent_after == exposure,
                d7_entered=False,
                reservation_created=True,
                **_attempt_report(reconciled_attempt),
            )
            if parent_after != exposure:
                result.update(status="HOLD", blocker="LEGACY_PARENT_RESERVATION_CHANGED")
            if recon is ProdReconciliationResult.OUTPUT_ALREADY_EXISTS:
                media_ids = reconciled_attempt.get("provider_media_ids")
                media_id = (
                    str(media_ids[0])
                    if isinstance(media_ids, list) and len(media_ids) == 1
                    else str(reconciled_attempt.get("provider_media_id", ""))
                )
                if media_id:
                    path, video = _download_validated_existing(
                        phase_g_request,
                        media_id=media_id,
                        expected_email=expected_email,
                        ledger=d2_ledger,
                    )
                    result.update(
                        status="PASS" if parent_after == exposure else "HOLD",
                        output_found=True,
                        output_path=str(path),
                        output_sha256=video.sha256,
                        output_duration_sec=video.duration_sec,
                        output_aspect_ratio=video.aspect_ratio,
                    )
            return result
        # Require a fresh identity/balance checkpoint immediately before the sole Phase G run.
        result["peak_window_advisory"] = is_non_subscriber_peak_window(datetime.now(UTC))
        if _read_balance(expected_email) < quote.amount:
            raise LegacyReplacementOperatorError(
                "fresh provider free balance is below the exact quote"
            )
        try:
            generated: PhaseGD2Run = asyncio.run(
                execute_and_validate(
                    phase_g_request,
                    expected_email=expected_email,
                    progress=phase_g_progress,
                )
            )
        except Exception as exc:
            observed = _attempt(d2_ledger, attempt_id)
            clicks = _action_count(observed, "generate_action_count") if observed else 0
            if clicks == 1:
                _record_phase_g_failure(
                    phase_g_recorder,
                    phase_g_progress,
                    exc,
                    repository_root=repository_root,
                    reason_code="PHASE_G_EXECUTION_INTERRUPTED_AFTER_CLICK",
                    failed_stage="POST_CLICK_EXECUTION",
                    sensitive_values=sensitive_values,
                )
                identity = ProdAttemptIdentity(
                    production_job_id=child_prod_id,
                    scene_job_id=child_scene_id,
                    d2_run_id=attempt_id,
                    provider_account_id=account_id,
                    provider_project_id=project_id,
                    scene_id=scene_id,
                    scene_prompt_id=prompt_id,
                    generation_profile_id=profile_id,
                    prompt_sha256=prompt_sha,
                )
                reconciliation_result = _reconcile_with_diagnostics(
                    runtime_root,
                    context,
                    identity,
                    credit_guard=guard,
                    recorder=phase_g_recorder,
                    progress=phase_g_progress,
                    repository_root=repository_root,
                    sensitive_values=sensitive_values,
                )
                observed_after = _attempt(d2_ledger, attempt_id)
                if observed_after is None:
                    raise LegacyReplacementOperatorError(
                        "replacement D2 evidence disappeared"
                    ) from None
                reservation = _replacement_reservation(
                    runtime_root, attempt_id, authorization_id, quote.amount
                )
                parent_after, _, _ = _parent_credit_state(runtime_root, parent_attempt_id)
                result.update(
                    status="HOLD",
                    failure_class="PHASE_G_EXECUTION_INTERRUPTED_AFTER_CLICK",
                    reconciliation_result=reconciliation_result.value,
                    generate_clicked=True,
                    generate_action_count=_action_count(observed_after, "generate_action_count"),
                    approval_action_count=(_action_count(observed_after, "approval_action_count")),
                    reservation_status=reservation.get("state"),
                    reservation_credits=reservation.get("reserved_credits"),
                    legacy_reservation_unchanged=parent_after == exposure,
                    d7_entered=False,
                    reservation_created=True,
                    **_attempt_report(observed_after),
                )
                if parent_after != exposure:
                    result.update(blocker="LEGACY_PARENT_RESERVATION_CHANGED")
                if reconciliation_result is ProdReconciliationResult.OUTPUT_ALREADY_EXISTS:
                    media_ids = observed_after.get("provider_media_ids")
                    media_id = (
                        str(media_ids[0])
                        if isinstance(media_ids, list) and len(media_ids) == 1
                        else str(observed_after.get("provider_media_id", ""))
                    )
                    if media_id:
                        path, video = _download_validated_existing(
                            phase_g_request,
                            media_id=media_id,
                            expected_email=expected_email,
                            ledger=d2_ledger,
                        )
                        result.update(
                            status="PASS" if parent_after == exposure else "HOLD",
                            output_found=True,
                            output_path=str(path),
                            output_sha256=video.sha256,
                            output_duration_sec=video.duration_sec,
                            output_aspect_ratio=video.aspect_ratio,
                            output_dimensions=f"{video.width}x{video.height}",
                        )
                        if parent_after != exposure:
                            result.update(
                                status="HOLD", blocker="LEGACY_PARENT_RESERVATION_CHANGED"
                            )
            else:
                failure_class = (
                    "PERSISTED_GENERATE_ACTION_COUNT_INVALID"
                    if clicks > 1
                    else "PHASE_G_EXECUTION_FAILED_BEFORE_CLICK"
                )
                diagnostic: dict[str, object] = {}
                if clicks == 0:
                    try:
                        diagnostic = phase_g_recorder.record_failure(
                            exc,
                            phase_g_progress,
                            repository_root=repository_root,
                            sensitive_values=(
                                phase_g_request.compiled_prompt_text,
                                phase_g_request.compiled_negative_prompt,
                            ),
                        )
                    except Exception:
                        diagnostic = {"diagnostics_persisted": False}
                result.update(
                    status="HOLD",
                    failure_class=failure_class,
                    reason_code=(
                        "PHASE_G_EXECUTION_FAILED_BEFORE_CLICK" if clicks == 0 else failure_class
                    ),
                    generate_clicked=clicks > 0,
                    generate_action_count=clicks,
                    approval_action_count=0,
                    preclick_diagnostic=diagnostic,
                    diagnostic_path=str(phase_g_recorder.path),
                )
            return result
        attempt_after = _attempt(d2_ledger, attempt_id)
        if attempt_after is None:
            raise LegacyReplacementOperatorError(
                "replacement D2 evidence disappeared after Phase G"
            )
        identity = ProdAttemptIdentity(
            production_job_id=child_prod_id,
            scene_job_id=child_scene_id,
            d2_run_id=attempt_id,
            provider_account_id=account_id,
            provider_project_id=project_id,
            scene_id=scene_id,
            scene_prompt_id=prompt_id,
            generation_profile_id=profile_id,
            prompt_sha256=prompt_sha,
        )
        reconciliation_result = _reconcile_with_diagnostics(
            runtime_root,
            context,
            identity,
            credit_guard=guard,
            recorder=phase_g_recorder,
            progress=phase_g_progress,
            repository_root=repository_root,
            sensitive_values=sensitive_values,
        )
        attempt_after = _attempt(d2_ledger, attempt_id)
        if attempt_after is None:
            raise LegacyReplacementOperatorError(
                "replacement D2 evidence disappeared after reconciliation"
            )
        parent_after, _, _ = _parent_credit_state(runtime_root, parent_attempt_id)
        reservation = _replacement_reservation(
            runtime_root, attempt_id, authorization_id, quote.amount
        )
        result.update(
            status="PASS"
            if reconciliation_result is ProdReconciliationResult.OUTPUT_ALREADY_EXISTS
            else "HOLD",
            reconciliation_result=reconciliation_result.value,
            authorization_status="CONSUMED",
            reservation_status=reservation.get("state"),
            reservation_credits=reservation.get("reserved_credits"),
            reservation_created=True,
            generate_clicked=True,
            generate_action_count=attempt_after.get("generate_action_count"),
            approval_action_count=attempt_after.get("approval_action_count"),
            generate_action_at=attempt_after.get("generate_action_at"),
            final_model_readback=phase_g_request.model_id,
            final_aspect_ratio_readback=phase_g_request.aspect_ratio,
            final_output_count_readback=phase_g_request.output_count,
            final_duration_intent=phase_g_request.duration_sec,
            **_attempt_report(attempt_after),
            output_found=True,
            output_path=str(generated.local_file),
            output_sha256=generated.video.sha256,
            output_duration_sec=generated.video.duration_sec,
            output_aspect_ratio=generated.video.aspect_ratio,
            output_dimensions=f"{generated.video.width}x{generated.video.height}",
            legacy_reservation_unchanged=parent_after == exposure,
            d7_entered=False,
        )
        if parent_after != exposure:
            result.update(status="HOLD", blocker="LEGACY_PARENT_RESERVATION_CHANGED")
        return result
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
    parser.add_argument("--parent-attempt-id", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--execute", action="store_true", help="run the approved one-shot Phase G path"
    )
    mode.add_argument(
        "--diagnose-preclick",
        action="store_true",
        help="run read-only Phase G checks for one exact existing PREPARED attempt",
    )
    parser.add_argument("--authorization-id")
    parser.add_argument("--replacement-attempt-id")
    parser.add_argument("--replacement-production-job-id")
    parser.add_argument("--replacement-scene-job-id")
    args = parser.parse_args()
    try:
        result = run_once(
            scene_id=args.scene_id,
            profile_id=args.profile_id,
            account_id=args.provider_account_id,
            project_id=args.provider_project_id,
            expected_email=args.expected_email,
            parent_attempt_id=args.parent_attempt_id,
            execute=args.execute,
            diagnose_preclick_mode=args.diagnose_preclick,
            authorization_id=args.authorization_id,
            replacement_attempt_id=args.replacement_attempt_id,
            replacement_production_job_id=args.replacement_production_job_id,
            replacement_scene_job_id=args.replacement_scene_job_id,
        )
    except Exception as exc:
        failure_class = type(exc).__name__
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", failure_class):
            failure_class = "LegacyReplacementOperatorError"
        report: dict[str, str] = {"status": "HOLD", "failure_class": failure_class}
        if isinstance(exc, LegacyParentJobLinkageError):
            report["failed_gate"] = "LEGACY_PARENT_JOB_LINKAGE_INCOMPLETE"
        print(json.dumps(report, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return (
        0
        if result.get("status")
        in {
            "QUOTE_VALID",
            "PASS",
            "RECONCILED_EXISTING_ATTEMPT",
            "PRECLICK_DIAGNOSTIC_COMPLETE",
        }
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())

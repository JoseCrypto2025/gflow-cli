"""Apply the approved quarantine policy to one exact ambiguous legacy PROD attempt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from gflow_cli.v2_runtime.ambiguous_legacy_recovery_policy import (
    AUDIT_NAME,
    POLICY_VERSION,
    RecoveryPolicyError,
    append_policy_event,
    build_policy_record,
    existing_policy_event,
    validate_policy_preconditions,
)
from gflow_cli.v2_runtime.config import load_runtime_settings
from gflow_cli.v2_runtime.google_sheets import GoogleWorkspaceCredentialProvider
from gflow_cli.v2_runtime.google_sheets_production_jobs import GoogleSheetsProductionJobStore
from gflow_cli.v2_runtime.google_sheets_transactions import (
    GoogleSheetsTransactionApiTransport,
    OptimisticGoogleSheetsJobStore,
)
from gflow_cli.v2_runtime.timestamps import format_utc_timestamp


def _read_json_rows(path: Path, kind: str) -> list[dict[str, object]]:
    if path.is_symlink() or not path.is_file():
        raise RecoveryPolicyError(f"{kind} ledger is unavailable or unsafe")
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryPolicyError(f"{kind} ledger is unreadable") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("attempts"), list):
        raise RecoveryPolicyError(f"{kind} ledger has an invalid shape")
    rows = cast(list[object], payload["attempts"])
    if any(not isinstance(row, dict) for row in rows):
        raise RecoveryPolicyError(f"{kind} ledger rows are invalid")
    return cast(list[dict[str, object]], rows)


def _provider_evidence_present(attempt: Mapping[str, object]) -> bool:
    evidence_fields = (
        "provider_submit_rpc_observed",
        "provider_submit_rpc_id",
        "provider_submit_http_method",
        "provider_submit_observed_at",
        "provider_submit_request_count",
        "provider_submit_response_observed",
        "provider_submit_response_status",
        "provider_submit_generation_record_observed",
        "provider_submit_refusal_detected",
        "provider_submit_refusal_code",
        "provider_submit_request_failed",
        "provider_execution_id",
        "provider_media_id",
        "provider_media_ids",
        "confirmation_id",
        "observed_confirmation_id",
    )
    return any(attempt.get(field) not in (None, "", False, 0, []) for field in evidence_fields)


def apply_policy(
    *,
    attempt_id: str,
    runtime_state_root: Path,
    schema_dir: Path,
    credential_path: Path,
    now: str,
) -> dict[str, object]:
    """Read exact persisted identities, record intent, quarantine jobs, and verify readback."""
    if attempt_id != "D2_13c62112d9881710019049d95cb6da57":
        raise RecoveryPolicyError("this authorized operator is scoped to the named legacy attempt")
    if not runtime_state_root.is_absolute() or runtime_state_root.is_symlink():
        raise RecoveryPolicyError("runtime state root is invalid")
    root = runtime_state_root.resolve(strict=True)
    d2_rows = _read_json_rows(root / "production" / "phase-g" / "d2-credit-ledger.json", "D2")
    credit_rows = _read_json_rows(root / "production-credit" / "shared-ledger.json", "credit")
    attempts = [row for row in d2_rows if row.get("d2_run_id") == attempt_id]
    reservations = [row for row in credit_rows if row.get("attempt_id") == attempt_id]
    if len(attempts) != 1 or len(reservations) != 1:
        raise RecoveryPolicyError("exact D2 attempt and credit reservation must each resolve once")
    attempt = attempts[0]
    if attempt.get("submission_state") != "UNKNOWN_SUBMISSION_STATE":
        raise RecoveryPolicyError("D2 attempt submission state changed from unknown")
    provider_evidence_present = _provider_evidence_present(attempt)
    if provider_evidence_present:
        raise RecoveryPolicyError(
            "persisted D2 row contains provider evidence; reassessment required"
        )
    reservation = reservations[0]
    state = str(reservation.get("state", ""))
    credits_raw = reservation.get("reserved_credits")
    if isinstance(credits_raw, bool):
        raise RecoveryPolicyError("reservation amount is invalid")
    try:
        credits = int(credits_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise RecoveryPolicyError("reservation amount is invalid") from exc
    if credits != 50 or state != "UNKNOWN_SUBMISSION_STATE":
        raise RecoveryPolicyError("credit reservation differs from the approved preserved state")

    token_provider = GoogleWorkspaceCredentialProvider(
        credential_path, repository_root=Path(__file__).resolve().parents[2]
    )
    transport = GoogleSheetsTransactionApiTransport(token_provider)
    try:
        row_store = OptimisticGoogleSheetsJobStore(transport, schema_dir, writer_lock_root=root)
        jobs = GoogleSheetsProductionJobStore(row_store)
        scene_rows = row_store.read_rows("SCENE_JOBS")
        linked_scenes = []
        for row in scene_rows:
            scene_id_value = str(row.get("Scene_Job_ID", "")).strip()
            attempt_no = row.get("Attempt_No")
            if isinstance(attempt_no, str) and attempt_no.isdigit():
                attempt_no = int(attempt_no)
            if not scene_id_value.startswith("SJB_") or not isinstance(attempt_no, int):
                continue
            run_key = f"PRODUCTION_SCENE_D2_V1:{scene_id_value}:{attempt_no}"
            expected_run_id = "D2_" + hashlib.sha256(run_key.encode("utf-8")).hexdigest()[:32]
            if expected_run_id == attempt_id:
                linked_scenes.append(row)
        if len(linked_scenes) != 1:
            raise RecoveryPolicyError("exact linked Scene Job must resolve once")
        scene = linked_scenes[0]
        scene_id = str(scene.get("Scene_Job_ID", ""))
        production_id = str(scene.get("Production_Job_ID", ""))
        production = jobs.read_production_job_row(production_id)
        if production.get("Execution_Environment") != "PROD":
            raise RecoveryPolicyError("linked Production Job is not PROD")
        if scene.get("Job_Status") not in {"RUNNING", "QUARANTINED"}:
            raise RecoveryPolicyError("linked Scene Job is not the exact expected RUNNING state")
        if scene.get("Provider_Execution_ID") not in (None, ""):
            raise RecoveryPolicyError("Scene Job contains provider execution evidence")
        if production.get("Job_Status") not in {"RUNNING", "QUARANTINED"}:
            raise RecoveryPolicyError(
                "linked Production Job is not the exact expected RUNNING state"
            )

        policy = build_policy_record(
            attempt_id=attempt_id,
            production_job_id=production_id,
            scene_job_id=scene_id,
            reservation_id=attempt_id,
            observed_at=now,
        )
        audit_path = runtime_state_root / "production" / "phase-g" / AUDIT_NAME
        prior_intent = existing_policy_event(
            audit_path,
            event_type="POLICY_INTENT",
            policy_id=str(policy["policy_id"]),
        )
        if prior_intent is not None:
            prior_timestamp = prior_intent.get("observed_at")
            policy["observed_at"] = prior_timestamp
            if prior_intent != policy:
                raise RecoveryPolicyError("partial policy replay conflicts with durable intent")
        validate_policy_preconditions(
            policy,
            attempt_id=attempt_id,
            reconciliation="AMBIGUOUS_AFTER_RECONCILIATION",
            evidence_recovery="AMBIGUOUS_EVIDENCE_EXHAUSTED",
            reservation_state=state,
            reservation_credits=credits,
            provider_evidence_present=provider_evidence_present,
        )
        append_policy_event(audit_path, event_type="POLICY_INTENT", policy=policy)
        jobs.quarantine_scene_job(scene_id, occurred_at=now)
        jobs.quarantine_production_job(production_id, occurred_at=now)
        saved_scene = jobs.read_scene_job_row(scene_id)
        saved_production = jobs.read_production_job_row(production_id)
        if (
            saved_scene.get("Job_Status") != "QUARANTINED"
            or saved_production.get("Job_Status") != "QUARANTINED"
        ):
            raise RecoveryPolicyError("quarantine status readback did not match")
        saved_reservations = _read_json_rows(
            root / "production-credit" / "shared-ledger.json", "credit"
        )
        current_reservations = [
            row for row in saved_reservations if row.get("attempt_id") == attempt_id
        ]
        if len(current_reservations) != 1 or current_reservations[0] != reservation:
            raise RecoveryPolicyError(
                "reservation changed during quarantine; policy remains incomplete"
            )
        apply_event = append_policy_event(
            audit_path,
            event_type="POLICY_APPLIED",
            policy=policy,
        )
        return {
            "policy_version": POLICY_VERSION,
            "policy_id": policy["policy_id"],
            "attempt_id": attempt_id,
            "production_job_status": saved_production["Job_Status"],
            "scene_job_status": saved_scene["Job_Status"],
            "reservation_state": state,
            "reservation_credits": credits,
            "billing_state": "UNKNOWN",
            "replacement_eligible": True,
            "replacement_authorized": False,
            "generate_actions": 0,
            "audit_event_sha256": apply_event,
            "state": "APPLIED",
        }
    finally:
        transport.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--d2-run-id", required=True)
    args = parser.parse_args()
    try:
        settings = load_runtime_settings()
        if settings.environment != "production":
            raise RecoveryPolicyError("policy application requires production runtime")
        credential_value = os.environ.get("GFLOW_GOOGLE_WORKSPACE_CREDENTIALS", "").strip()
        if not credential_value:
            raise RecoveryPolicyError("Google Workspace credentials are not configured")
        result = apply_policy(
            attempt_id=args.d2_run_id,
            runtime_state_root=settings.runtime_state_root,
            schema_dir=Path(__file__).resolve().parents[2] / "schemas",
            credential_path=Path(credential_value),
            now=format_utc_timestamp(datetime.now(UTC), timespec="seconds"),
        )
    except Exception as exc:  # noqa: BLE001 — expose a bounded HOLD classification only
        print(json.dumps({"state": "HOLD", "failure_class": type(exc).__name__}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

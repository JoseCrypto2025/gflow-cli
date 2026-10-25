"""Apply the approved quarantine policy to one exact ambiguous legacy PROD attempt."""

from __future__ import annotations

import argparse
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
    ambiguous_legacy_policy_id,
    append_policy_event,
    build_policy_record,
    existing_policy_event,
    validate_policy_preconditions,
)
from gflow_cli.v2_runtime.config import load_runtime_settings
from gflow_cli.v2_runtime.google_sheets import (
    GoogleSheetsApiTransport,
    GoogleSheetsSnapshotSource,
    GoogleWorkspaceCredentialProvider,
)
from gflow_cli.v2_runtime.google_sheets_production_jobs import GoogleSheetsProductionJobStore
from gflow_cli.v2_runtime.google_sheets_transactions import (
    GoogleSheetsTransactionApiTransport,
    OptimisticGoogleSheetsJobStore,
)
from gflow_cli.v2_runtime.phase_g_prod_legacy_reconciliation import (
    build_legacy_prod_attempt_envelope,
    find_legacy_prod_reconciliation_audit,
)
from gflow_cli.v2_runtime.timestamps import format_utc_timestamp
from scripts.operations.reconcile_legacy_prod_attempt import build_legacy_sources_from_snapshot


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


def _prior_reconciliation_audit_sha256(
    runtime_state_root: Path,
    *,
    attempt_id: str,
    production_job_id: str,
    scene_job_id: str,
    prior_policy: Mapping[str, object] | None,
) -> str:
    """Bind policy to a validated prior ambiguous reconciliation event without rerunning it."""
    audit_path = runtime_state_root / "phase-g" / "prod-legacy-reconciliation-audit.json"
    if prior_policy is not None:
        digest = str(prior_policy.get("reconciliation_audit_sha256", ""))
        event = find_legacy_prod_reconciliation_audit(runtime_state_root, digest)
        if event is None:
            raise RecoveryPolicyError("prior policy reconciliation audit link is unavailable")
        candidates = [event]
    else:
        if audit_path.is_symlink() or not audit_path.is_file():
            raise RecoveryPolicyError("prior legacy reconciliation audit is unavailable")
        try:
            payload: object = json.loads(audit_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoveryPolicyError("prior legacy reconciliation audit is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise RecoveryPolicyError("prior legacy reconciliation audit schema is invalid")
        events = payload.get("events")
        if not isinstance(events, list) or any(not isinstance(row, dict) for row in events):
            raise RecoveryPolicyError("prior legacy reconciliation audit event list is invalid")
        candidates = [
            cast(dict[str, object], row)
            for row in events
            if row.get("legacy_attempt_id") == attempt_id
            and row.get("result") == "AMBIGUOUS_AFTER_RECONCILIATION"
        ]
    exact: list[dict[str, object]] = []
    for candidate in candidates:
        provenance = candidate.get("evidence_provenance")
        if not isinstance(provenance, dict):
            continue
        production = provenance.get("production_job_id")
        scene = provenance.get("scene_job_id")
        attempt = provenance.get("attempt_id")
        values_match = (
            isinstance(production, dict)
            and production.get("value") == production_job_id
            and isinstance(scene, dict)
            and scene.get("value") == scene_job_id
            and isinstance(attempt, dict)
            and attempt.get("value") == attempt_id
        )
        if values_match and candidate.get("reservation_action") == "UNCHANGED":
            digest = candidate.get("event_sha256")
            if isinstance(digest, str):
                validated = find_legacy_prod_reconciliation_audit(runtime_state_root, digest)
                if validated is not None:
                    exact.append(validated)
    if len(exact) != 1:
        raise RecoveryPolicyError("exact prior ambiguous reconciliation audit must resolve once")
    return str(exact[0]["event_sha256"])


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
    token_provider = GoogleWorkspaceCredentialProvider(
        credential_path, repository_root=Path(__file__).resolve().parents[2]
    )
    snapshot_transport = GoogleSheetsApiTransport(token_provider)
    try:
        snapshot = GoogleSheetsSnapshotSource(snapshot_transport, schema_dir).read()
    finally:
        snapshot_transport.close()
    sources = build_legacy_sources_from_snapshot(snapshot, root, runtime_namespace="PROD")
    envelope = build_legacy_prod_attempt_envelope(sources, attempt_id)
    if not envelope.recovery_prerequisite_satisfied or envelope.identity is None:
        raise RecoveryPolicyError("fresh legacy identity envelope is ambiguous; quarantine stopped")
    identity = envelope.identity
    if identity.d2_run_id != attempt_id:
        raise RecoveryPolicyError("legacy envelope does not bind the exact target attempt")
    d2_rows = [row for row in sources.d2_attempts if row.get("d2_run_id") == attempt_id]
    reservations = [
        row for row in sources.credit_reservations if row.get("attempt_id") == attempt_id
    ]
    if len(d2_rows) != 1 or len(reservations) != 1:
        raise RecoveryPolicyError("exact D2 attempt and credit reservation must each resolve once")
    attempt = d2_rows[0]
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

    transport = GoogleSheetsTransactionApiTransport(token_provider)
    try:
        row_store = OptimisticGoogleSheetsJobStore(transport, schema_dir, writer_lock_root=root)
        jobs = GoogleSheetsProductionJobStore(row_store)
        scene_rows = row_store.read_rows("SCENE_JOBS")
        linked_scenes = [
            row for row in scene_rows if row.get("Scene_Job_ID") == identity.scene_job_id
        ]
        if len(linked_scenes) != 1:
            raise RecoveryPolicyError("exact linked Scene Job must resolve once")
        scene = linked_scenes[0]
        scene_id = identity.scene_job_id
        production_id = identity.production_job_id
        snapshot_scene_rows = [
            row for row in sources.scene_jobs if row.get("Scene_Job_ID") == scene_id
        ]
        snapshot_production_rows = [
            row for row in sources.production_jobs if row.get("Production_Job_ID") == production_id
        ]
        if len(snapshot_scene_rows) != 1 or len(snapshot_production_rows) != 1:
            raise RecoveryPolicyError("legacy envelope job joins are missing or duplicated")
        snapshot_scene = snapshot_scene_rows[0]
        snapshot_production = snapshot_production_rows[0]
        production = jobs.read_production_job_row(production_id)
        try:
            snapshot_attempt_no = int(str(snapshot_scene.get("Attempt_No", "")))
            writer_attempt_no = int(str(scene.get("Attempt_No", "")))
        except ValueError as exc:
            raise RecoveryPolicyError("linked Scene Job attempt number is invalid") from exc
        if (
            any(
                scene.get(field) != snapshot_scene.get(field)
                for field in ("Production_Job_ID", "Scene_Prompt_ID", "Job_Status")
            )
            or any(
                production.get(field) != snapshot_production.get(field)
                for field in ("Execution_Environment", "Job_Status")
            )
            or writer_attempt_no != snapshot_attempt_no
        ):
            raise RecoveryPolicyError("job rows changed between canonical and transactional reads")
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

        policy_id = ambiguous_legacy_policy_id(attempt_id)
        prior_intent = existing_policy_event(
            runtime_state_root / "production" / "phase-g" / AUDIT_NAME,
            event_type="POLICY_INTENT",
            policy_id=policy_id,
        )
        reconciliation_digest = _prior_reconciliation_audit_sha256(
            root,
            attempt_id=attempt_id,
            production_job_id=production_id,
            scene_job_id=scene_id,
            prior_policy=prior_intent,
        )
        policy = build_policy_record(
            attempt_id=attempt_id,
            production_job_id=production_id,
            scene_job_id=scene_id,
            reservation_id=attempt_id,
            reconciliation_audit_sha256=reconciliation_digest,
            observed_at=now,
        )
        audit_path = runtime_state_root / "production" / "phase-g" / AUDIT_NAME
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

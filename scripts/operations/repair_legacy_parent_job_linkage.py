"""Apply the exact proof-bound repair for the named legacy PROD D2 parent."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from gflow_cli.v2_runtime.ambiguous_legacy_recovery_policy import (
    ambiguous_legacy_policy_id,
    find_policy_event,
)
from gflow_cli.v2_runtime.config import load_runtime_settings
from gflow_cli.v2_runtime.google_sheets import (
    GoogleSheetsApiTransport,
    GoogleSheetsSnapshotSource,
    GoogleWorkspaceCredentialProvider,
)
from gflow_cli.v2_runtime.phase_g import D2DailyCreditLedger, PhaseGError
from gflow_cli.v2_runtime.phase_g_prod_legacy_job_linkage import (
    LegacyJobLinkageProofError,
    build_legacy_job_linkage_repair_proof,
)
from gflow_cli.v2_runtime.phase_g_prod_legacy_reconciliation import (
    find_legacy_prod_reconciliation_audit,
)
from scripts.operations.reconcile_legacy_prod_attempt import (
    APPROVED_SPREADSHEET_ID,
    _software_commit,
    build_legacy_sources_from_snapshot,
)

_ATTEMPT_ID = "D2_13c62112d9881710019049d95cb6da57"
_PRODUCTION_JOB_ID = "PRD_000001"
_SCENE_JOB_ID = "SJB_000001"
_POLICY_AUDIT = "ambiguous-legacy-recovery-policy-audit.json"
_RECONCILIATION_AUDIT_SHA256 = "ffe9fc9561f818367f51ce044bfe6c094a4ee3a48eb34d6f65025abfe4daa1ec"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def repair_exact_parent() -> dict[str, object]:
    settings = load_runtime_settings()
    if settings.environment != "production":
        raise LegacyJobLinkageProofError("RUNTIME_NOT_PROD", "repair requires production runtime")
    runtime_root = settings.runtime_state_root
    if not runtime_root.is_absolute() or runtime_root.is_symlink():
        raise LegacyJobLinkageProofError("RUNTIME_ROOT_INVALID", "runtime state root is unsafe")
    credential_value = os.environ.get("GFLOW_GOOGLE_WORKSPACE_CREDENTIALS", "").strip()
    if not credential_value:
        raise LegacyJobLinkageProofError(
            "CREDENTIALS_MISSING", "canonical Sheets credentials are missing"
        )
    repository_root = Path(__file__).resolve().parents[2]
    credential_path = Path(credential_value)
    if credential_path.is_symlink() or not credential_path.is_file():
        raise LegacyJobLinkageProofError(
            "CREDENTIALS_INVALID", "canonical Sheets credentials are unavailable"
        )

    token_provider = GoogleWorkspaceCredentialProvider(
        credential_path, repository_root=repository_root
    )
    transport = GoogleSheetsApiTransport(token_provider)
    try:
        snapshot = GoogleSheetsSnapshotSource(transport, repository_root / "schemas").read()
    finally:
        transport.close()
    if snapshot.spreadsheet_id != APPROVED_SPREADSHEET_ID:
        raise LegacyJobLinkageProofError(
            "CANONICAL_SOURCE_MISMATCH", "canonical spreadsheet identity is not approved"
        )

    sources = build_legacy_sources_from_snapshot(snapshot, runtime_root, runtime_namespace="PROD")
    d2_runtime_root = runtime_root / "production"
    ledger = D2DailyCreditLedger(d2_runtime_root)
    if ledger.path.resolve(strict=True) != Path(sources.d2_ledger_path).resolve(strict=True):
        raise LegacyJobLinkageProofError(
            "D2_LEDGER_MISMATCH", "repair ledger differs from the proven source ledger"
        )
    policy_path = runtime_root / "production" / "phase-g" / _POLICY_AUDIT
    policy_event = find_policy_event(
        policy_path,
        event_type="POLICY_APPLIED",
        policy_id=ambiguous_legacy_policy_id(_ATTEMPT_ID),
    )
    if policy_event is None:
        raise LegacyJobLinkageProofError(
            "POLICY_AUDIT_MISSING", "exact applied recovery-policy audit is unavailable"
        )
    reconciliation_event = find_legacy_prod_reconciliation_audit(
        runtime_root, _RECONCILIATION_AUDIT_SHA256
    )
    if reconciliation_event is None:
        raise LegacyJobLinkageProofError(
            "RECONCILIATION_AUDIT_MISSING", "exact prior reconciliation audit is unavailable"
        )

    before_credit_sha256 = _sha256_file(runtime_root / "production-credit" / "shared-ledger.json")
    before_d2_sha256 = _sha256_file(ledger.path)
    if (
        before_credit_sha256 != sources.source_snapshots["CREDIT_LEDGER"].sha256
        or before_d2_sha256 != sources.source_snapshots["D2_LEDGER"].sha256
    ):
        raise LegacyJobLinkageProofError(
            "SOURCE_LEDGER_CHANGED", "D2 or reservation ledger changed after proof capture"
        )
    before_policy_sha256 = _sha256_file(policy_path)
    reconciliation_path = runtime_root / "phase-g" / "prod-legacy-reconciliation-audit.json"
    before_reconciliation_file_sha256 = _sha256_file(reconciliation_path)
    before_attempts = [
        dict(row) for row in sources.d2_attempts if row.get("d2_run_id") == _ATTEMPT_ID
    ]
    if len(before_attempts) != 1:
        raise LegacyJobLinkageProofError("ATTEMPT_NOT_UNIQUE", "exact D2 attempt is not unique")
    before_attempt = before_attempts[0]

    proof = build_legacy_job_linkage_repair_proof(
        sources,
        attempt_id=_ATTEMPT_ID,
        production_job_id=_PRODUCTION_JOB_ID,
        scene_job_id=_SCENE_JOB_ID,
        policy_event=policy_event,
        reconciliation_event=reconciliation_event,
        verified_at=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        software_commit=_software_commit(repository_root),
    )
    repair_audit = ledger.repair_legacy_job_linkage(proof)

    after_payload = ledger._read()  # noqa: SLF001
    after_rows = cast(list[dict[str, object]], after_payload["attempts"])
    after_matches = [row for row in after_rows if row.get("d2_run_id") == _ATTEMPT_ID]
    expected_rows = [
        {
            **dict(row),
            "production_job_id": _PRODUCTION_JOB_ID,
            "scene_job_id": _SCENE_JOB_ID,
        }
        if row.get("d2_run_id") == _ATTEMPT_ID
        else dict(row)
        for row in sources.d2_attempts
    ]
    if (
        len(after_matches) != 1
        or after_matches[0]
        != {
            **before_attempt,
            "production_job_id": _PRODUCTION_JOB_ID,
            "scene_job_id": _SCENE_JOB_ID,
        }
        or after_rows != expected_rows
    ):
        raise LegacyJobLinkageProofError(
            "READBACK_MISMATCH", "repair readback changed an unrelated legacy D2 field"
        )
    if (
        _sha256_file(runtime_root / "production-credit" / "shared-ledger.json")
        != before_credit_sha256
        or _sha256_file(policy_path) != before_policy_sha256
        or _sha256_file(reconciliation_path) != before_reconciliation_file_sha256
    ):
        raise LegacyJobLinkageProofError(
            "UNRELATED_STATE_CHANGED", "repair changed reservation or prior audit state"
        )
    return {
        "status": "PASS",
        "attempt_id": _ATTEMPT_ID,
        "before_production_job_id": before_attempt.get("production_job_id"),
        "after_production_job_id": after_matches[0].get("production_job_id"),
        "before_scene_job_id": before_attempt.get("scene_job_id"),
        "after_scene_job_id": after_matches[0].get("scene_job_id"),
        "linkage_confidence": proof["linkage_confidence"],
        "linkage_audit_id": repair_audit["linkage_audit_id"],
        "provenance_class": repair_audit["provenance_class"],
        "d2_ledger_before_sha256": before_d2_sha256,
        "d2_ledger_after_sha256": _sha256_file(ledger.path),
        "submission_state": after_matches[0].get("submission_state"),
        "reservation_credits": next(
            row.get("reserved_credits")
            for row in sources.credit_reservations
            if row.get("attempt_id") == _ATTEMPT_ID
        ),
        "generate_action_count": after_matches[0].get("generate_action_count"),
        "provider_evidence_unchanged": True,
        "credit_ledger_sha256_unchanged": True,
        "policy_audit_sha256_unchanged": True,
        "reconciliation_audit_file_sha256_unchanged": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--d2-run-id", required=True)
    parser.add_argument("--production-job-id", required=True)
    parser.add_argument("--scene-job-id", required=True)
    args = parser.parse_args()
    if (
        args.d2_run_id != _ATTEMPT_ID
        or args.production_job_id != _PRODUCTION_JOB_ID
        or args.scene_job_id != _SCENE_JOB_ID
    ):
        print(json.dumps({"status": "HOLD", "blocker": "EXACT_TARGET_MISMATCH"}, sort_keys=True))
        return 2
    try:
        result = repair_exact_parent()
    except LegacyJobLinkageProofError as exc:
        print(json.dumps({"status": "HOLD", "blocker": exc.code}, sort_keys=True))
        return 2
    except PhaseGError as exc:
        print(json.dumps({"status": "HOLD", "blocker": exc.code}, sort_keys=True))
        return 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "HOLD", "blocker": type(exc).__name__}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

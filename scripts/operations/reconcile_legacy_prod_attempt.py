"""Classify one existing legacy PROD D2 attempt from fresh canonical evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from gflow_cli.v2_runtime.config import load_runtime_settings
from gflow_cli.v2_runtime.google_sheets import (
    APPROVED_SPREADSHEET_ID,
    SOURCE_LIVE_GOOGLE_SHEETS,
    GoogleSheetsApiTransport,
    GoogleSheetsReadError,
    GoogleSheetsSnapshot,
    GoogleSheetsSnapshotSource,
    GoogleWorkspaceCredentialProvider,
)
from gflow_cli.v2_runtime.phase_g_prod_legacy_reconciliation import (
    LEGACY_PROD_CONTRACT_VERSION,
    LegacyProdAttemptSources,
    LegacyProdReconciliationDecision,
    LegacySourceSnapshot,
    build_legacy_prod_attempt_envelope,
    legacy_recovery_prerequisite_satisfied,
    reconcile_legacy_prod_same_attempt,
    write_legacy_prod_reconciliation_audit,
)
from gflow_cli.v2_runtime.phase_g_prod_reconciliation import ProdReconciliationEvidence
from gflow_cli.v2_runtime.timestamps import format_utc_timestamp

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
_SHEET_TABLES = {
    "production_jobs": "PRODUCTION_JOBS",
    "scene_jobs": "SCENE_JOBS",
    "scenes": "SCENES",
    "scene_prompt_versions": "SCENE_PROMPT_VERSION",
    "generation_profiles": "GENERATION_PROFILE_MASTER",
    "provider_accounts": "PROVIDER_ACCOUNT_MASTER",
}


class LegacyProdOperatorError(ValueError):
    """A fail-closed input or prerequisite error from the legacy operator."""


def _digest_json(value: object) -> str:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LegacyProdOperatorError("canonical source rows are not valid JSON data") from exc
    return hashlib.sha256(encoded).hexdigest()


def _read_ledger(path: Path, *, kind: str) -> tuple[tuple[Mapping[str, object], ...], str, str]:
    if path.is_symlink() or not path.is_file():
        raise LegacyProdOperatorError(f"{kind} ledger is unavailable or unsafe")
    try:
        raw = path.read_bytes()
        payload_value: object = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LegacyProdOperatorError(f"{kind} ledger is unreadable") from exc
    if not isinstance(payload_value, dict):
        raise LegacyProdOperatorError(f"{kind} ledger has an invalid shape")
    payload = cast(dict[str, object], payload_value)
    rows_value = payload.get("attempts")
    if not isinstance(rows_value, list) or any(not isinstance(row, dict) for row in rows_value):
        raise LegacyProdOperatorError(f"{kind} ledger has an invalid attempts list")
    if kind == "D2":
        schema_version = payload.get("schema_version")
        valid_v1 = schema_version == 1 and set(payload) == {
            "schema_version",
            "daily_budget",
            "attempts",
        }
        valid_v2 = (
            schema_version == 2
            and set(payload)
            == {
                "schema_version",
                "daily_budget",
                "attempts",
                "test_sessions",
            }
            and isinstance(payload.get("test_sessions"), list)
        )
        if not (valid_v1 or valid_v2) or payload.get("daily_budget") != 50:
            raise LegacyProdOperatorError("D2 ledger version or budget is invalid")
    if kind == "CREDIT" and payload.get("schema_version") != 1:
        raise LegacyProdOperatorError("shared credit ledger version is invalid")
    observed_at = format_utc_timestamp(datetime.now(UTC), timespec="seconds")
    return (
        tuple(
            {str(key): value for key, value in cast(dict[object, object], row).items()}
            for row in cast(list[object], rows_value)
        ),
        hashlib.sha256(raw).hexdigest(),
        observed_at,
    )


def build_legacy_sources_from_snapshot(
    snapshot: GoogleSheetsSnapshot,
    runtime_state_root: Path,
    *,
    runtime_namespace: str,
) -> LegacyProdAttemptSources:
    """Join a validated live Sheets snapshot with fresh, read-only PROD ledgers."""
    if (
        snapshot.source != SOURCE_LIVE_GOOGLE_SHEETS
        or snapshot.spreadsheet_id != APPROVED_SPREADSHEET_ID
    ):
        raise LegacyProdOperatorError("canonical Sheets snapshot source is not trusted")
    if not snapshot.read_at.strip() or runtime_namespace != "PROD":
        raise LegacyProdOperatorError("fresh PROD source context is unavailable")
    if not runtime_state_root.is_absolute() or runtime_state_root.is_symlink():
        raise LegacyProdOperatorError("runtime state root is invalid")
    try:
        root = runtime_state_root.resolve(strict=True)
    except OSError as exc:
        raise LegacyProdOperatorError("runtime state root is unavailable") from exc
    if not root.is_dir() or root == Path(root.anchor):
        raise LegacyProdOperatorError("runtime state root is invalid")

    table_rows: dict[str, tuple[Mapping[str, object], ...]] = {}
    source_snapshots: dict[str, LegacySourceSnapshot] = {}
    for field_name, table_name in _SHEET_TABLES.items():
        rows_value = snapshot.tables.get(table_name)
        if not isinstance(rows_value, list) or any(
            not isinstance(row, Mapping) for row in rows_value
        ):
            raise LegacyProdOperatorError(f"canonical {table_name} rows are missing or invalid")
        rows = tuple(cast(Mapping[str, object], dict(row)) for row in rows_value)
        table_rows[field_name] = rows
        source_snapshots[table_name] = LegacySourceSnapshot(
            source=table_name,
            sha256=_digest_json([dict(row) for row in rows]),
            observed_at=snapshot.read_at,
        )

    d2_path = root / "production" / "phase-g" / "d2-credit-ledger.json"
    credit_path = root / "production-credit" / "shared-ledger.json"
    for path in (
        root / "production",
        root / "production" / "phase-g",
        d2_path,
        root / "production-credit",
        credit_path,
    ):
        if path.is_symlink():
            raise LegacyProdOperatorError("ledger path may not contain a symlink")
        try:
            if not path.resolve(strict=True).is_relative_to(root):
                raise LegacyProdOperatorError("ledger path escaped the runtime state root")
        except OSError as exc:
            raise LegacyProdOperatorError("required runtime ledger is unavailable") from exc
    d2_rows, d2_sha, d2_observed_at = _read_ledger(d2_path, kind="D2")
    credit_rows, credit_sha, credit_observed_at = _read_ledger(credit_path, kind="CREDIT")
    source_snapshots["D2_LEDGER"] = LegacySourceSnapshot("D2_LEDGER", d2_sha, d2_observed_at)
    source_snapshots["CREDIT_LEDGER"] = LegacySourceSnapshot(
        "CREDIT_LEDGER", credit_sha, credit_observed_at
    )
    return LegacyProdAttemptSources(
        **table_rows,
        d2_attempts=d2_rows,
        credit_reservations=credit_rows,
        source_snapshots=source_snapshots,
        runtime_namespace=runtime_namespace,
        d2_ledger_path=str(d2_path),
    )


def run_legacy_reconciliation(
    snapshot: GoogleSheetsSnapshot,
    runtime_state_root: Path,
    d2_run_id: str,
    *,
    observed_at: str,
    software_commit: str,
    evidence: ProdReconciliationEvidence | None = None,
    persist_audit: bool = False,
    runtime_namespace: str = "PROD",
) -> LegacyProdReconciliationDecision:
    """Validate the legacy envelope, classify one attempt, and optionally persist its audit."""
    if not _COMMIT_RE.fullmatch(software_commit):
        raise LegacyProdOperatorError("running software commit is unavailable")
    sources = build_legacy_sources_from_snapshot(
        snapshot, runtime_state_root, runtime_namespace=runtime_namespace
    )
    envelope = build_legacy_prod_attempt_envelope(sources, d2_run_id)
    if not legacy_recovery_prerequisite_satisfied(envelope):
        raise LegacyProdOperatorError(
            "legacy recovery prerequisite failed: " + ",".join(envelope.reasons)
        )
    decision = reconcile_legacy_prod_same_attempt(
        sources,
        d2_run_id,
        observed_at=observed_at,
        software_commit=software_commit,
        evidence=evidence,
    )
    if decision.envelope.sha256 != envelope.sha256:
        raise LegacyProdOperatorError("legacy source envelope changed during classification")
    if persist_audit:
        write_legacy_prod_reconciliation_audit(runtime_state_root, decision.audit_event)
    return decision


def _read_live_snapshot(credential_path: Path, schema_dir: Path) -> GoogleSheetsSnapshot:
    repo_root = Path(__file__).resolve().parents[2]
    token_provider = GoogleWorkspaceCredentialProvider(credential_path, repository_root=repo_root)
    transport = GoogleSheetsApiTransport(token_provider)
    try:
        return GoogleSheetsSnapshotSource(transport, schema_dir).read()
    finally:
        transport.close()


def _software_commit(app_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(app_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0:
        commit = result.stdout.strip().lower()
        if _COMMIT_RE.fullmatch(commit):
            return commit
    try:
        manifest = json.loads((app_root / ".gflow-v2-build.json").read_text(encoding="utf-8"))
        commit = str(manifest["commit_sha"]).lower()
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise LegacyProdOperatorError("deployed software commit cannot be verified") from exc
    if not _COMMIT_RE.fullmatch(commit):
        raise LegacyProdOperatorError("deployed software commit is invalid")
    return commit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--d2-run-id", required=True)
    parser.add_argument(
        "--persist-audit",
        action="store_true",
        help="persist the classification event to the separate legacy audit file",
    )
    args = parser.parse_args()
    try:
        settings = load_runtime_settings()
        if settings.environment != "production":
            raise LegacyProdOperatorError("legacy PROD reconciliation requires production runtime")
        credential_value = os.environ.get("GFLOW_GOOGLE_WORKSPACE_CREDENTIALS", "").strip()
        if not credential_value:
            raise LegacyProdOperatorError("Google Workspace credentials are not configured")
        app_root = Path(__file__).resolve().parents[2]
        snapshot = _read_live_snapshot(Path(credential_value), app_root / "schemas")
        decision = run_legacy_reconciliation(
            snapshot,
            settings.runtime_state_root,
            args.d2_run_id,
            observed_at=format_utc_timestamp(datetime.now(UTC), timespec="seconds"),
            software_commit=_software_commit(app_root),
            persist_audit=args.persist_audit,
        )
    except (OSError, ValueError, GoogleSheetsReadError) as exc:
        print(
            json.dumps(
                {
                    "state": "HOLD",
                    "failure_class": type(exc).__name__,
                    "detail": "legacy PROD reconciliation prerequisites were not met",
                },
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "state": "CLASSIFIED",
                "contract_version": LEGACY_PROD_CONTRACT_VERSION,
                "attempt_id": args.d2_run_id,
                "result": decision.result.value,
                "identity_status": decision.envelope.identity_status,
                "envelope_sha256": decision.envelope.sha256,
                "audit_persisted": args.persist_audit,
                "mutation_performed": False,
                "reservation_action": "UNCHANGED",
                "operator_report_for_legacy": "NOT_APPLICABLE",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

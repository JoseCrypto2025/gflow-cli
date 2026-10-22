"""Export a fresh, validated canonical Master snapshot for the one-shot D2 test."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from gflow_cli.v2_runtime.config import load_runtime_settings
from gflow_cli.v2_runtime.google_sheets import (
    APPROVED_SPREADSHEET_ID,
    SOURCE_LIVE_GOOGLE_SHEETS,
    GoogleSheetsApiTransport,
    GoogleSheetsReadError,
    GoogleSheetsSnapshotSource,
    GoogleWorkspaceCredentialProvider,
    write_snapshot_atomic,
)


def export_d2_master_snapshot(credential_path: Path, runtime_root: Path) -> dict[str, object]:
    """Read the approved live Master and persist it with the canonical writer."""
    repository_root = Path(__file__).resolve().parents[2]
    schema_dir = repository_root / "schemas"
    token_provider = GoogleWorkspaceCredentialProvider(
        credential_path, repository_root=repository_root
    )
    transport = GoogleSheetsApiTransport(token_provider)
    try:
        snapshot = GoogleSheetsSnapshotSource(transport, schema_dir).read()
    finally:
        transport.close()
    if (
        snapshot.spreadsheet_id != APPROVED_SPREADSHEET_ID
        or snapshot.source != SOURCE_LIVE_GOOGLE_SHEETS
    ):
        raise GoogleSheetsReadError("snapshot did not come from the approved live source")

    root = runtime_root.resolve(strict=True)
    if runtime_root.is_symlink() or not root.is_dir():
        raise GoogleSheetsReadError("runtime state root is unavailable")
    phase_g_dir = root / "phase-g"
    if phase_g_dir.is_symlink():
        raise GoogleSheetsReadError("runtime snapshot path is invalid")
    phase_g_dir.mkdir(mode=0o700, exist_ok=True)
    snapshot_dir = phase_g_dir / "master-snapshots"
    if snapshot_dir.is_symlink():
        raise GoogleSheetsReadError("runtime snapshot path is invalid")
    snapshot_dir.mkdir(mode=0o700, exist_ok=True)
    if os.name == "posix":
        os.chmod(phase_g_dir, 0o700)
        os.chmod(snapshot_dir, 0o700)
    target = snapshot_dir / f"d2-master-snapshot-{uuid4().hex}.json"
    write_snapshot_atomic(snapshot, target, runtime_root=root)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    return {
        "output_path": str(target),
        "spreadsheet_id": snapshot.spreadsheet_id,
        "read_at": snapshot.read_at,
        "source": snapshot.source,
        "table_count": len(snapshot.tables),
        "file_sha256": digest,
        "write_performed": True,
    }


def main() -> int:
    credential_value = os.environ.get("GFLOW_GOOGLE_WORKSPACE_CREDENTIALS", "").strip()
    if not credential_value:
        print(
            json.dumps(
                {
                    "state": "HOLD",
                    "reason_code": "GOOGLE_SHEETS_AUTH_NOT_CONFIGURED",
                    "detail": "Google Workspace credential path is required",
                    "write_performed": False,
                },
                sort_keys=True,
            )
        )
        return 2
    try:
        settings = load_runtime_settings()
        result = export_d2_master_snapshot(
            Path(credential_value), settings.runtime_state_root
        )
    except Exception as exc:  # noqa: BLE001 — do not expose credential or API details
        print(
            json.dumps(
                {
                    "state": "HOLD",
                    "reason_code": "D2_MASTER_SNAPSHOT_EXPORT_FAILED",
                    "failure_class": type(exc).__name__,
                    "detail": "live Master snapshot export failed safely",
                    "write_performed": False,
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

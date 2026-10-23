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
    GoogleSheetsSnapshot,
    GoogleSheetsSnapshotSource,
    GoogleWorkspaceCredentialProvider,
    write_snapshot_atomic,
)

_BOOL_FALSE = {"false", "0", "no"}


def _normalized_cell(value: object) -> str:
    return "" if value is None else str(value).strip()


def _load_snapshot_row_rules(schema_dir: Path) -> dict[str, tuple[str, frozenset[str]]]:
    rules: dict[str, tuple[str, frozenset[str]]] = {}
    for subdir in ("master", "transaction", "relation"):
        for schema_path in sorted((schema_dir / subdir).glob("*.schema.json")):
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            table_name = str(schema["table_name"])
            primary_key = str(schema["primary_key"])
            fields = schema["fields"]
            field_names = {str(field["field_name"]) for field in fields}
            if primary_key not in field_names:
                raise GoogleSheetsReadError("snapshot schema primary key is invalid")
            boolean_fields = frozenset(
                str(field["field_name"])
                for field in fields
                if str(field.get("data_type", "")).upper() == "BOOLEAN"
            )
            if table_name in rules:
                raise GoogleSheetsReadError("duplicate snapshot schema table")
            rules[table_name] = (primary_key, boolean_fields)
    return rules


def _normalize_snapshot_rows(
    snapshot: GoogleSheetsSnapshot, schema_dir: Path
) -> tuple[GoogleSheetsSnapshot, int]:
    """Remove checkbox-only phantom rows while failing closed on real orphan data."""
    rules = _load_snapshot_row_rules(schema_dir)
    if set(snapshot.tables) != set(rules):
        raise GoogleSheetsReadError("snapshot table set does not match schema")

    normalized_tables: dict[str, list[dict[str, object]]] = {}
    removed = 0
    for table_name, rows in snapshot.tables.items():
        primary_key, boolean_fields = rules[table_name]
        kept_rows: list[dict[str, object]] = []
        for raw_row in rows:
            row = dict(raw_row)
            if _normalized_cell(row.get(primary_key)):
                kept_rows.append(row)
                continue

            has_substantive_value = False
            for field_name, raw_value in row.items():
                value = _normalized_cell(raw_value)
                if not value:
                    continue
                if field_name in boolean_fields and value.lower() in _BOOL_FALSE:
                    continue
                has_substantive_value = True
                break

            if has_substantive_value:
                raise GoogleSheetsReadError(
                    f"{table_name} contains data without its primary key"
                )
            removed += 1
        normalized_tables[table_name] = kept_rows

    return (
        GoogleSheetsSnapshot(
            spreadsheet_id=snapshot.spreadsheet_id,
            read_at=snapshot.read_at,
            source=snapshot.source,
            tables=normalized_tables,
        ),
        removed,
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

    snapshot, phantom_rows_removed = _normalize_snapshot_rows(snapshot, schema_dir)
    logical_row_count = sum(len(rows) for rows in snapshot.tables.values())

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
        "logical_row_count": logical_row_count,
        "phantom_rows_removed": phantom_rows_removed,
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

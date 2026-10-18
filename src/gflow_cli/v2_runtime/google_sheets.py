"""Read-only, provenance-bearing Google Sheets snapshot source for V2."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote

import httpx

from .timestamps import format_utc_timestamp

Row = Mapping[str, object]
TableRows = dict[str, list[Row]]
APPROVED_SPREADSHEET_ID = "1wtoIBfeHNdGYK6cyhDfKPG6W3TnnfL4QDvCAtJBX27Q"
SOURCE_LIVE_GOOGLE_SHEETS = "LIVE_GOOGLE_SHEETS"
GOOGLE_DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
_SECRET_FIELD_RE = re.compile(
    r"password|token|cookie|secret|private[_-]?key|api[_-]?key|oauth", re.I
)


class GoogleSheetsReadError(ValueError):
    """Safe failure raised without exposing credential or API response content."""


class SheetsReadTransport(Protocol):
    """Minimal read-only transport; the D5 source has no Sheet-write surface."""

    def read_values(
        self, spreadsheet_id: str, sheet_name: str, value_range: str
    ) -> Sequence[Sequence[object]]: ...


class AccessTokenProvider(Protocol):
    def access_token(self) -> str: ...


@dataclass(frozen=True)
class GoogleSheetsSnapshot:
    spreadsheet_id: str
    read_at: str
    source: str
    tables: TableRows


SnapshotValidator = Callable[[Path, TableRows], Sequence[str]]


class GoogleWorkspaceCredentialProvider:
    """Load an externally provisioned OAuth credential with a per-file scope."""

    def __init__(self, credential_path: Path, *, repository_root: Path) -> None:
        path = credential_path.expanduser()
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise GoogleSheetsReadError("Google Workspace credential file is unavailable")
        if path.resolve().is_relative_to(repository_root.resolve()):
            raise GoogleSheetsReadError("Google Workspace credential must be outside the checkout")
        if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise GoogleSheetsReadError("Google Workspace credential permissions are too broad")
        if os.name == "posix" and path.stat().st_uid != os.geteuid():
            raise GoogleSheetsReadError(
                "Google Workspace credential owner does not match service user"
            )
        try:
            from google.oauth2.credentials import Credentials

            self._credentials: Any = Credentials.from_authorized_user_file(  # pyright: ignore[reportUnknownMemberType]
                str(path), scopes=[GOOGLE_DRIVE_FILE_SCOPE]
            )
        except Exception as exc:  # noqa: BLE001 — OAuth parse detail may expose credential data
            raise GoogleSheetsReadError("Google Workspace credential is invalid") from exc

    def access_token(self) -> str:
        try:
            if not self._credentials.valid:
                from google.auth.transport.requests import Request

                self._credentials.refresh(Request())  # pyright: ignore[reportUnknownMemberType]
            token: str | None = cast(str | None, self._credentials.token)
        except Exception as exc:  # noqa: BLE001 — refresh detail may contain sensitive response data
            raise GoogleSheetsReadError("Google Workspace authorization failed") from exc
        if not token:
            raise GoogleSheetsReadError("Google Workspace authorization failed")
        return token


class GoogleSheetsApiTransport:
    """Small Sheets API v4 REST client; intentionally exposes reads only."""

    def __init__(
        self,
        token_provider: AccessTokenProvider,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._client = client or httpx.Client(timeout=20.0, follow_redirects=False)
        self._owns_client = client is None

    def read_values(
        self, spreadsheet_id: str, sheet_name: str, value_range: str
    ) -> Sequence[Sequence[object]]:
        if spreadsheet_id != APPROVED_SPREADSHEET_ID:
            raise GoogleSheetsReadError("unapproved spreadsheet ID")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", sheet_name):
            raise GoogleSheetsReadError("invalid Sheet tab name")
        if not re.fullmatch(r"A1:AZ1001", value_range):
            raise GoogleSheetsReadError("invalid bounded Sheet range")
        endpoint = (
            "https://sheets.googleapis.com/v4/spreadsheets/"
            f"{quote(spreadsheet_id, safe='')}/values/"
            f"{quote(sheet_name + '!' + value_range, safe='!:$')}"
        )
        try:
            response = self._client.get(
                endpoint,
                params={"valueRenderOption": "FORMATTED_VALUE"},
                headers={"Authorization": f"Bearer {self._token_provider.access_token()}"},
            )
            response.raise_for_status()
            payload: object = response.json()
        except Exception as exc:  # noqa: BLE001 — never echo HTTP/OAuth exception text
            raise GoogleSheetsReadError("Google Sheets read failed") from exc
        values: object = (
            cast(Mapping[str, object], payload).get("values", [])
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(values, list):
            raise GoogleSheetsReadError("Google Sheets response shape is invalid")
        rows: list[object] = cast(list[object], values)
        if any(not isinstance(row, list) for row in rows):
            raise GoogleSheetsReadError("Google Sheets response shape is invalid")
        return cast(Sequence[Sequence[object]], values)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


class GoogleSheetsSnapshotSource:
    """Read all local V2 tables and validate them before creating a snapshot."""

    def __init__(
        self,
        transport: SheetsReadTransport,
        schema_dir: Path,
        *,
        spreadsheet_id: str = APPROVED_SPREADSHEET_ID,
        validator: SnapshotValidator | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if spreadsheet_id != APPROVED_SPREADSHEET_ID:
            raise GoogleSheetsReadError("unapproved spreadsheet ID")
        self._transport = transport
        self._schema_dir = schema_dir
        self._validator = validator or validate_v2_snapshot
        self._clock = clock
        self.spreadsheet_id = spreadsheet_id

    def read(self) -> GoogleSheetsSnapshot:
        schemas = _load_schema_headers(self._schema_dir)
        tables: TableRows = {}
        for table_name, headers in schemas.items():
            try:
                values = self._transport.read_values(self.spreadsheet_id, table_name, "A1:AZ1001")
            except Exception as exc:  # noqa: BLE001 — transport details are sensitive
                raise GoogleSheetsReadError("Google Sheets read failed") from exc
            tables[table_name] = _rows_from_values(table_name, headers, values)
        if _contains_secret_field(tables):
            raise GoogleSheetsReadError("V2 snapshot contains a prohibited secret field")
        errors = tuple(self._validator(self._schema_dir, tables))
        if errors:
            raise GoogleSheetsReadError("Google Sheets snapshot failed V2 validation")
        read_at = self._clock()
        if read_at.tzinfo is None or read_at.utcoffset() != UTC.utcoffset(read_at):
            raise GoogleSheetsReadError("snapshot clock must return a UTC-aware timestamp")
        return GoogleSheetsSnapshot(
            spreadsheet_id=self.spreadsheet_id,
            read_at=format_utc_timestamp(read_at, timespec="seconds"),
            source=SOURCE_LIVE_GOOGLE_SHEETS,
            tables=tables,
        )


def validate_v2_snapshot(schema_dir: Path, tables: TableRows) -> Sequence[str]:
    """Use the repository's full schema, key, FK, and semantic validator."""
    repo_root = Path(__file__).resolve().parents[3]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    try:
        from scripts.validation.validate_v2_data_architecture import load_schemas, validate_rows

        load_schemas(schema_dir)
        validation_tables = {name: [dict(row) for row in rows] for name, rows in tables.items()}
        return validate_rows(schema_dir, validation_tables)
    except (ImportError, OSError, ValueError) as exc:
        raise GoogleSheetsReadError("local V2 schema validator is unavailable") from exc


def write_snapshot_atomic(
    snapshot: GoogleSheetsSnapshot, path: Path, *, runtime_root: Path
) -> None:
    """Atomically write a credential-free snapshot below runtime state."""
    root = runtime_root.resolve(strict=True)
    target = path.absolute()
    if path.is_symlink() or not target.resolve(strict=False).is_relative_to(root):
        raise GoogleSheetsReadError("snapshot path must be below the runtime root")
    if not target.parent.is_dir() or target.parent.is_symlink():
        raise GoogleSheetsReadError("snapshot parent must be a runtime directory")
    payload = json.dumps(
        {
            "spreadsheet_id": snapshot.spreadsheet_id,
            "read_at": snapshot.read_at,
            "source": snapshot.source,
            "tables": snapshot.tables,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".gflow-snapshot-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        if os.name == "posix":
            os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def snapshot_document(snapshot: GoogleSheetsSnapshot) -> Mapping[str, object]:
    """Return the persisted contract with provenance and no authentication data."""
    return {
        "spreadsheet_id": snapshot.spreadsheet_id,
        "read_at": snapshot.read_at,
        "source": snapshot.source,
        "tables": cast(dict[str, object], snapshot.tables),
    }


def _load_schema_headers(schema_dir: Path) -> dict[str, tuple[str, ...]]:
    headers: dict[str, tuple[str, ...]] = {}
    for subdir in ("master", "transaction", "relation"):
        for schema_path in sorted((schema_dir / subdir).glob("*.schema.json")):
            try:
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                table = schema["table_name"]
                fields = schema["fields"]
                names = tuple(field["field_name"] for field in fields)
            except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise GoogleSheetsReadError("local V2 schema is malformed") from exc
            if (
                not isinstance(table, str)
                or not names
                or any(not isinstance(x, str) for x in names)
            ):
                raise GoogleSheetsReadError("local V2 schema is malformed")
            if table in headers:
                raise GoogleSheetsReadError("duplicate local V2 schema table")
            headers[table] = names
    if not headers:
        raise GoogleSheetsReadError("local V2 schemas are missing")
    return headers


def _rows_from_values(
    table_name: str, headers: tuple[str, ...], values: Sequence[Sequence[object]]
) -> list[Row]:
    if not values or tuple(str(value) for value in values[0]) != headers:
        raise GoogleSheetsReadError(f"{table_name} headers do not match the local schema")
    rows: list[Row] = []
    for values_row in values[1:]:
        if len(values_row) > len(headers) and any(
            value not in (None, "") for value in values_row[len(headers) :]
        ):
            raise GoogleSheetsReadError(f"{table_name} has values beyond schema columns")
        cells = list(values_row[: len(headers)]) + [""] * max(0, len(headers) - len(values_row))
        if all(value in (None, "") for value in cells):
            continue
        rows.append(
            {
                header: value if value is not None else ""
                for header, value in zip(headers, cells, strict=True)
            }
        )
    return rows


def _contains_secret_field(value: object) -> bool:
    if isinstance(value, Mapping):
        items = cast(Mapping[object, object], value).items()
        return any(
            (isinstance(key, str) and _SECRET_FIELD_RE.search(key)) or _contains_secret_field(item)
            for key, item in items
        )
    if isinstance(value, list):
        return any(_contains_secret_field(item) for item in cast(list[object], value))
    return False

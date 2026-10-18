"""Read-only, provenance-bearing Google Sheets snapshot source for V2."""

from __future__ import annotations

import json
import os
import random
import re
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
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

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GoogleSheetsRateLimitExhaustedError(GoogleSheetsReadError):
    """A bounded Google API GET retry budget ended on HTTP 429."""

    def __init__(self) -> None:
        super().__init__("Google API rate limit retry budget exhausted", status_code=429)


_MAX_GET_ATTEMPTS = 7
_MAX_RETRY_WAIT_SECONDS = 90.0
_BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0)
_A1_CELL_RE = re.compile(r"\$?([A-Za-z]+)\$?([1-9][0-9]*)\Z")
_SAFE_SHEET_DIAGNOSTIC_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")


@dataclass(frozen=True)
class _ParsedA1Range:
    sheet_name: str
    bounds: tuple[int, int, int, int]


def _parse_a1_range(value: str) -> _ParsedA1Range | None:
    """Parse a bounded A1 range, including quoted titles and absolute cells."""
    if not value or len(value) > 512:
        return None
    if value.startswith("'"):
        title_chars: list[str] = []
        index = 1
        while index < len(value):
            char = value[index]
            if char == "'":
                if index + 1 < len(value) and value[index + 1] == "'":
                    title_chars.append("'")
                    index += 2
                    continue
                if index + 1 >= len(value) or value[index + 1] != "!":
                    return None
                sheet_name = "".join(title_chars)
                cell_range = value[index + 2 :]
                break
            title_chars.append(char)
            index += 1
        else:
            return None
    else:
        if value.count("!") != 1:
            return None
        sheet_name, cell_range = value.split("!", 1)
        if not re.fullmatch(r"[A-Za-z0-9_]+", sheet_name):
            return None
    if not sheet_name or not cell_range or "!" in cell_range:
        return None
    endpoints = cell_range.split(":")
    if len(endpoints) not in (1, 2):
        return None
    first_match = _A1_CELL_RE.fullmatch(endpoints[0])
    last_match = _A1_CELL_RE.fullmatch(endpoints[-1])
    if first_match is None or last_match is None:
        return None

    def column_number(letters: str) -> int:
        number = 0
        for letter in letters.upper():
            number = number * 26 + ord(letter) - ord("A") + 1
        return number

    bounds = (
        int(first_match.group(2)),
        column_number(first_match.group(1)),
        int(last_match.group(2)),
        column_number(last_match.group(1)),
    )
    if bounds[0] > bounds[2] or bounds[1] > bounds[3]:
        return None
    return _ParsedA1Range(sheet_name, bounds)


def _safe_range_diagnostic(value: object) -> str:
    if not isinstance(value, str):
        return "<missing>"
    parsed = _parse_a1_range(value)
    if parsed is None:
        return "<invalid>"
    if not _SAFE_SHEET_DIAGNOSTIC_RE.fullmatch(parsed.sheet_name):
        return "<unrecognized>"
    start_row, start_col, end_row, end_col = parsed.bounds

    def column_name(column: int) -> str:
        result = ""
        while column:
            column, remainder = divmod(column - 1, 26)
            result = chr(ord("A") + remainder) + result
        return result

    return (
        f"{parsed.sheet_name}!{column_name(start_col)}{start_row}:{column_name(end_col)}{end_row}"
    )


def _batch_range_error(
    *,
    response_count: int,
    expected_count: int,
    index: int | None,
    requested: str | None,
    returned: object,
    reason: str,
) -> GoogleSheetsReadError:
    requested_value = requested if requested is not None else "<none>"
    return GoogleSheetsReadError(
        "Google Sheets batch range mismatch: "
        f"BATCH_RESPONSE_COUNT={response_count} "
        f"EXPECTED_RANGE_COUNT={expected_count} "
        f"FAILED_INDEX={index if index is not None else '<none>'} "
        f"REQUESTED_RANGE={requested_value} "
        f"RETURNED_RANGE={_safe_range_diagnostic(returned)} "
        f"FAILURE_REASON={reason}"
    )


def google_api_get_with_retry(
    client: httpx.Client,
    endpoint: str,
    *,
    params: Mapping[str, str] | tuple[tuple[str, str], ...],
    headers: Mapping[str, str],
    sleeper: Callable[[float], None] = time.sleep,
    random_uniform: Callable[[float, float], float] = random.uniform,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> httpx.Response:
    """Issue a safe Google API GET with bounded 429/5xx retry handling."""
    started = monotonic()
    waited = 0.0
    for attempt in range(_MAX_GET_ATTEMPTS):
        response = client.get(endpoint, params=params, headers=headers)
        if response.status_code != 429 and not 500 <= response.status_code <= 599:
            return response
        if attempt == _MAX_GET_ATTEMPTS - 1:
            if response.status_code == 429:
                raise GoogleSheetsRateLimitExhaustedError()
            raise GoogleSheetsReadError(
                "Google API transient server error retry budget exhausted",
                status_code=response.status_code,
            )

        remaining = min(
            _MAX_RETRY_WAIT_SECONDS - waited,
            _MAX_RETRY_WAIT_SECONDS - (monotonic() - started),
        )
        retry_after = _retry_after_seconds(response.headers.get("Retry-After"), now())
        base = _BACKOFF_SECONDS[attempt]
        delay = retry_after if retry_after is not None else random_uniform(base * 0.8, base * 1.2)
        if delay > remaining:
            if response.status_code == 429:
                raise GoogleSheetsRateLimitExhaustedError()
            raise GoogleSheetsReadError(
                "Google API transient server error retry budget exhausted",
                status_code=response.status_code,
            )
        sleeper(delay)
        waited += delay
    raise AssertionError("bounded GET retry loop must return or raise")


def _retry_after_seconds(value: str | None, current_time: datetime) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - current_time).total_seconds())


def _values_from_payload(payload: object) -> Sequence[Sequence[object]]:
    payload_mapping = cast(Mapping[str, object], payload) if isinstance(payload, dict) else None
    values: object = payload_mapping.get("values", []) if payload_mapping is not None else None
    if not isinstance(values, list):
        raise GoogleSheetsReadError("Google Sheets response shape is invalid")
    rows = cast(list[object], values)
    if any(not isinstance(row, list) for row in rows):
        raise GoogleSheetsReadError("Google Sheets response shape is invalid")
    return cast(Sequence[Sequence[object]], rows)


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
        sleeper: Callable[[float], None] = time.sleep,
        random_uniform: Callable[[float, float], float] = random.uniform,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._token_provider = token_provider
        self._client = client or httpx.Client(timeout=20.0, follow_redirects=False)
        self._owns_client = client is None
        self._sleeper = sleeper
        self._random_uniform = random_uniform
        self._monotonic = monotonic
        self._now = now

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
            response = google_api_get_with_retry(
                self._client,
                endpoint,
                params={"valueRenderOption": "FORMATTED_VALUE"},
                headers={"Authorization": f"Bearer {self._token_provider.access_token()}"},
                sleeper=self._sleeper,
                random_uniform=self._random_uniform,
                monotonic=self._monotonic,
                now=self._now,
            )
            if not response.is_success:
                raise GoogleSheetsReadError(
                    "Google Sheets read failed", status_code=response.status_code
                )
            payload: object = response.json()
        except GoogleSheetsReadError:
            raise
        except Exception as exc:  # noqa: BLE001 — never echo HTTP/OAuth exception text
            raise GoogleSheetsReadError("Google Sheets read failed") from exc
        return _values_from_payload(payload)

    def read_values_batch(
        self, spreadsheet_id: str, ranges: Mapping[str, str]
    ) -> Mapping[str, Sequence[Sequence[object]]]:
        if spreadsheet_id != APPROVED_SPREADSHEET_ID:
            raise GoogleSheetsReadError("unapproved spreadsheet ID")
        if not ranges:
            raise GoogleSheetsReadError("empty Google Sheets batch range set")
        requested: dict[str, str] = {}
        for sheet_name, value_range in ranges.items():
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", sheet_name):
                raise GoogleSheetsReadError("invalid Sheet tab name")
            if value_range != "A1:AZ1001":
                raise GoogleSheetsReadError("invalid bounded Sheet range")
            if sheet_name in requested:
                raise GoogleSheetsReadError("duplicate Sheet tab in batch range set")
            requested[sheet_name] = value_range
        endpoint = (
            "https://sheets.googleapis.com/v4/spreadsheets/"
            f"{quote(spreadsheet_id, safe='')}/values:batchGet"
        )
        params = tuple(
            [
                ("ranges", f"{sheet_name}!{value_range}")
                for sheet_name, value_range in requested.items()
            ]
            + [("valueRenderOption", "FORMATTED_VALUE")]
        )
        try:
            response = google_api_get_with_retry(
                self._client,
                endpoint,
                params=params,
                headers={"Authorization": f"Bearer {self._token_provider.access_token()}"},
                sleeper=self._sleeper,
                random_uniform=self._random_uniform,
                monotonic=self._monotonic,
                now=self._now,
            )
            if not response.is_success:
                raise GoogleSheetsReadError(
                    "Google Sheets batch read failed", status_code=response.status_code
                )
            payload: object = response.json()
        except GoogleSheetsReadError:
            raise
        except Exception as exc:  # noqa: BLE001 — response details may contain secrets
            raise GoogleSheetsReadError("Google Sheets batch read failed") from exc
        if not isinstance(payload, dict):
            raise GoogleSheetsReadError("Google Sheets batch response shape is invalid")
        payload_mapping = cast(Mapping[str, object], payload)
        raw_value_ranges = payload_mapping.get("valueRanges")
        if not isinstance(raw_value_ranges, list):
            raise GoogleSheetsReadError("Google Sheets batch response shape is invalid")
        value_ranges = cast(list[object], raw_value_ranges)
        if len(value_ranges) != len(requested):
            raise _batch_range_error(
                response_count=len(value_ranges),
                expected_count=len(requested),
                index=None,
                requested=None,
                returned=None,
                reason="range_count_mismatch",
            )
        result: dict[str, Sequence[Sequence[object]]] = {}
        requested_items = list(requested.items())
        for index, (item, (sheet_name, value_range)) in enumerate(
            zip(value_ranges, requested_items, strict=True)
        ):
            if not isinstance(item, dict):
                raise _batch_range_error(
                    response_count=len(value_ranges),
                    expected_count=len(requested),
                    index=index,
                    requested=f"{sheet_name}!{value_range}",
                    returned=None,
                    reason="invalid_range_entry",
                )
            item_mapping = cast(Mapping[str, object], item)
            returned_range = item_mapping.get("range")
            parsed_returned = (
                _parse_a1_range(returned_range) if isinstance(returned_range, str) else None
            )
            parsed_requested = _parse_a1_range(f"{sheet_name}!{value_range}")
            if parsed_returned is None or parsed_requested is None:
                raise _batch_range_error(
                    response_count=len(value_ranges),
                    expected_count=len(requested),
                    index=index,
                    requested=f"{sheet_name}!{value_range}",
                    returned=returned_range,
                    reason="invalid_a1_range",
                )
            returned_bounds = parsed_returned.bounds
            requested_bounds = parsed_requested.bounds
            if parsed_returned.sheet_name != sheet_name:
                reason = "sheet_mismatch"
            elif not (
                requested_bounds[0]
                <= returned_bounds[0]
                <= returned_bounds[2]
                <= requested_bounds[2]
                and requested_bounds[1]
                <= returned_bounds[1]
                <= returned_bounds[3]
                <= requested_bounds[3]
            ):
                reason = "range_out_of_bounds"
            else:
                reason = ""
            if reason:
                raise _batch_range_error(
                    response_count=len(value_ranges),
                    expected_count=len(requested),
                    index=index,
                    requested=f"{sheet_name}!{value_range}",
                    returned=returned_range,
                    reason=reason,
                )
            raw_values = item_mapping.get("values", [])
            if not isinstance(raw_values, list):
                raise GoogleSheetsReadError("Google Sheets batch response shape is invalid")
            rows = cast(list[object], raw_values)
            if any(not isinstance(row, list) for row in rows):
                raise GoogleSheetsReadError("Google Sheets batch response shape is invalid")
            result[sheet_name] = cast(Sequence[Sequence[object]], rows)
        return {sheet_name: result[sheet_name] for sheet_name in requested}

    def read_metadata(self) -> Mapping[str, object]:
        """Read approved spreadsheet identity using the same bounded GET policy."""
        endpoint = (
            "https://sheets.googleapis.com/v4/spreadsheets/"
            f"{quote(APPROVED_SPREADSHEET_ID, safe='')}"
        )
        try:
            response = google_api_get_with_retry(
                self._client,
                endpoint,
                params={"fields": "spreadsheetId,properties(title)"},
                headers={"Authorization": f"Bearer {self._token_provider.access_token()}"},
                sleeper=self._sleeper,
                random_uniform=self._random_uniform,
                monotonic=self._monotonic,
                now=self._now,
            )
            if not response.is_success:
                raise GoogleSheetsReadError(
                    "Google Sheets metadata read failed", status_code=response.status_code
                )
            payload: object = response.json()
        except GoogleSheetsReadError:
            raise
        except Exception as exc:  # noqa: BLE001 — response details may contain secrets
            raise GoogleSheetsReadError("Google Sheets metadata read failed") from exc
        if not isinstance(payload, dict):
            raise GoogleSheetsReadError("Google Sheets metadata response shape is invalid")
        return cast(Mapping[str, object], payload)
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
        schemas = load_schema_headers(self._schema_dir)
        tables: TableRows = {}
        batch_reader = cast(
            Callable[
                [str, Mapping[str, str]],
                Mapping[str, Sequence[Sequence[object]]],
            ]
            | None,
            getattr(self._transport, "read_values_batch", None),
        )
        if callable(batch_reader):
            try:
                batch_values_raw = batch_reader(
                    self.spreadsheet_id,
                    {table_name: "A1:AZ1001" for table_name in schemas},
                )
            except GoogleSheetsReadError:
                raise
            except Exception as exc:  # noqa: BLE001 — transport details are sensitive
                raise GoogleSheetsReadError("Google Sheets read failed") from exc
            if set(batch_values_raw) != set(schemas):
                raise GoogleSheetsReadError("Google Sheets batch response is incomplete")
            batch_values = batch_values_raw
            for table_name, headers in schemas.items():
                tables[table_name] = _rows_from_values(
                    table_name, headers, batch_values[table_name]
                )
        else:
            for table_name, headers in schemas.items():
                try:
                    values = self._transport.read_values(
                        self.spreadsheet_id, table_name, "A1:AZ1001"
                    )
                except GoogleSheetsReadError:
                    raise
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


def load_schema_headers(schema_dir: Path) -> dict[str, tuple[str, ...]]:
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

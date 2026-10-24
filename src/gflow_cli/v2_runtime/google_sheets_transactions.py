"""Bounded optimistic transaction operations for the existing V2 job tabs.

Google Sheets values APIs do not provide compare-and-swap. These operations
serialize this VPS's writers, compare a canonical pre-read, write one bounded
row, and verify readback. A concurrent external editor can still race between
the pre-read and API update; callers must treat any ambiguity as HOLD.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote

import httpx

from .google_sheets import (
    APPROVED_SPREADSHEET_ID,
    AccessTokenProvider,
    GoogleSheetsReadError,
    google_api_get_with_retry,
    load_schema_headers,
)

_ALLOWED_KEYS = {
    "PRODUCTION_JOBS": "Production_Job_ID",
    "SCENE_JOBS": "Scene_Job_ID",
    "TTS_JOBS": "TTS_Job_ID",
    "FINAL_OUTPUT": "Final_Output_ID",
}
_READ_ONLY_KEYS = {"SCENE_DIALOGUE_LINES": "Dialogue_Line_ID"}
_READABLE_KEYS = {**_ALLOWED_KEYS, **_READ_ONLY_KEYS}
_MAX_ROWS = 1001
_MAX_COLUMNS = 52


class SheetsTransactionError(ValueError):
    """Safe conflict or transport failure from bounded V2 Sheet writes."""


class SheetsTransactionTransport(Protocol):
    def read_values(self, tab: str, a1_range: str) -> Sequence[Sequence[object]]: ...
    def update_values(
        self, tab: str, a1_range: str, values: Sequence[Sequence[object]]
    ) -> None: ...
    def append_values(self, tab: str, values: Sequence[Sequence[object]]) -> None: ...


class GoogleSheetsTransactionApiTransport:
    """OAuth-injected Sheets v4 values reader/writer, restricted to job tabs."""

    def __init__(self, token_provider: AccessTokenProvider, *, client: httpx.Client | None = None):
        self._token_provider = token_provider
        self._client = client or httpx.Client(timeout=30.0, follow_redirects=False)
        self._owns_client = client is None

    def read_values(self, tab: str, a1_range: str) -> Sequence[Sequence[object]]:
        self._validate_range(tab, a1_range)
        try:
            response = self._client.get(
                self._values_url(tab, a1_range),
                params={"valueRenderOption": "UNFORMATTED_VALUE"},
                headers=self._headers(),
            )
            response.raise_for_status()
            value: object = response.json()
        except Exception as exc:  # noqa: BLE001
            raise SheetsTransactionError("Google Sheets read failed") from exc
        if not isinstance(value, dict):
            raise SheetsTransactionError("Google Sheets response shape is invalid")
        payload = cast(dict[str, object], value)
        if not isinstance(payload.get("values", []), list):
            raise SheetsTransactionError("Google Sheets response shape is invalid")
        rows = cast(list[object], payload.get("values", []))
        if any(not isinstance(row, list) for row in rows):
            raise SheetsTransactionError("Google Sheets response shape is invalid")
        return cast(Sequence[Sequence[object]], rows)

    def update_values(self, tab: str, a1_range: str, values: Sequence[Sequence[object]]) -> None:
        if tab not in _ALLOWED_KEYS:
            raise SheetsTransactionError("only V2 job transaction tabs are writable")
        self._validate_range(tab, a1_range)
        self._write("PUT", self._values_url(tab, a1_range), {"values": values})

    def append_values(self, tab: str, values: Sequence[Sequence[object]]) -> None:
        if tab not in _ALLOWED_KEYS:
            raise SheetsTransactionError("Sheet tab is not writable through V2 job transactions")
        self._write(
            "POST",
            self._values_url(tab, f"A1:AZ{_MAX_ROWS}:append"),
            {"values": values},
            params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _write(
        self,
        method: str,
        url: str,
        body: dict[str, object],
        *,
        params: Mapping[str, str] | None = None,
    ) -> None:
        try:
            response = self._client.request(
                method,
                url,
                params={"valueInputOption": "RAW", **(params or {})},
                headers=self._headers(),
                json=body,
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — suppress auth and response details
            raise SheetsTransactionError(
                "Google Sheets write acknowledgement is uncertain"
            ) from exc

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token_provider.access_token()}"}

    @staticmethod
    def _validate_range(tab: str, a1_range: str) -> None:
        bounded = a1_range == f"A1:AZ{_MAX_ROWS}" or bool(
            re.fullmatch(
                r"A([2-9]|[1-9][0-9]{1,2}|1000|1001):AZ([2-9]|[1-9][0-9]{1,2}|1000|1001)",
                a1_range,
            )
        )
        if tab not in _READABLE_KEYS or not bounded:
            raise SheetsTransactionError("Sheet range is outside bounded V2 job operations")

    @staticmethod
    def _values_url(tab: str, a1_range: str) -> str:
        encoded = quote(f"{tab}!{a1_range}", safe="!:$")
        return (
            f"https://sheets.googleapis.com/v4/spreadsheets/{APPROVED_SPREADSHEET_ID}"
            f"/values/{encoded}"
        )


class OptimisticGoogleSheetsJobStore:
    """Read-before-write job rows with local writer serialization and readback."""

    def __init__(
        self,
        transport: SheetsTransactionTransport,
        schema_dir: Path,
        *,
        writer_lock_root: Path,
    ) -> None:
        if not writer_lock_root.is_absolute() or writer_lock_root == Path(writer_lock_root.anchor):
            raise SheetsTransactionError("writer lock root must be absolute and non-root")
        try:
            self._headers = load_schema_headers(schema_dir)
        except GoogleSheetsReadError as exc:
            raise SheetsTransactionError("V2 transaction schemas are unavailable") from exc
        if not set(_READABLE_KEYS).issubset(self._headers):
            raise SheetsTransactionError("V2 transaction schemas are missing")
        self._transport = transport
        self.writer_lock_path = writer_lock_root / ".v2-sheets-writer.lock"
        self.thread_lock = threading.RLock()

    def append_if_absent(self, tab: str, row: dict[str, object]) -> dict[str, object]:
        key = self._key(tab)
        self._validate_row(tab, row)
        with _SheetWriterLock(self):
            headers, rows = self._read_table(tab)
            matches = [item for item in rows if item.get(key) == row[key]]
            if len(matches) > 1:
                raise SheetsTransactionError("duplicate stable job key already exists")
            if matches:
                if self._digest(matches[0], headers) != self._digest(row, headers):
                    raise SheetsTransactionError("stable job key is bound to different row data")
                return dict(matches[0])
            try:
                self._transport.append_values(tab, [self._as_values(row, headers)])
            except SheetsTransactionError:
                # A lost response may follow a successful append. Resolve by
                # stable key and exact content before considering any retry.
                pass
            _new_headers, new_rows = self._read_table(tab)
            matched = [item for item in new_rows if item.get(key) == row[key]]
            if len(matched) != 1 or self._digest(matched[0], headers) != self._digest(row, headers):
                raise SheetsTransactionError("append readback is missing, duplicated, or different")
            return dict(matched[0])

    def append_if_unique(
        self,
        tab: str,
        row: dict[str, object],
        *,
        unique_constraints: tuple[tuple[str, ...], ...],
        identity_fields: tuple[str, ...],
    ) -> dict[str, object]:
        """Append a row under schema unique keys and reconcile uncertain writes."""
        key = self._key(tab)
        self._validate_row(tab, row)
        headers = self._headers[tab]
        constraints = (tuple((key,)), *unique_constraints)
        if (
            not identity_fields
            or any(
                not fields or any(field not in headers for field in fields)
                for fields in constraints
            )
            or any(field not in headers for field in identity_fields)
        ):
            raise SheetsTransactionError("unique-key fields do not match the local schema")
        with _SheetWriterLock(self):
            actual_headers, rows = self._read_table(tab)
            matching_rows: dict[str, dict[str, object]] = {}
            for existing in rows:
                if any(
                    all(existing.get(field) == row.get(field) for field in fields)
                    for fields in constraints
                ):
                    matching_rows[self._digest(existing, actual_headers)] = existing
            if len(matching_rows) > 1:
                raise SheetsTransactionError(
                    "multiple rows match the unique request key; HOLD required"
                )
            if matching_rows:
                existing = next(iter(matching_rows.values()))
                if any(existing.get(field) != row.get(field) for field in identity_fields):
                    raise SheetsTransactionError(
                        "unique request key is bound to different input data"
                    )
                return dict(existing)
            try:
                self._transport.append_values(tab, [self._as_values(row, actual_headers)])
            except SheetsTransactionError:
                # The append may have committed even though its acknowledgement was lost.
                pass
            _new_headers, new_rows = self._read_table(tab)
            matches = [
                existing
                for existing in new_rows
                if any(
                    all(existing.get(field) == row.get(field) for field in fields)
                    for fields in constraints
                )
            ]
            if len(matches) != 1 or any(
                matches[0].get(field) != row.get(field) for field in identity_fields
            ):
                raise SheetsTransactionError("unique append readback is ambiguous or different")
            return dict(matches[0])

    def update_expected(
        self,
        tab: str,
        *,
        key_value: str,
        expected_digest: str,
        replacement: dict[str, object],
    ) -> dict[str, object]:
        key = self._key(tab)
        self._validate_row(tab, replacement)
        if replacement.get(key) != key_value:
            raise SheetsTransactionError("replacement row cannot change its stable key")
        with _SheetWriterLock(self):
            headers, rows = self._read_table(tab)
            located = [
                (index, row) for index, row in enumerate(rows, start=2) if row.get(key) == key_value
            ]
            if len(located) != 1:
                raise SheetsTransactionError("stable job key is missing or duplicated")
            row_number, current = located[0]
            if self._digest(current, headers) != expected_digest:
                raise SheetsTransactionError("job row changed since the caller's expected snapshot")
            a1_range = f"A{row_number}:AZ{row_number}"
            try:
                self._transport.update_values(
                    tab, a1_range, [self._as_values(replacement, headers)]
                )
            except SheetsTransactionError:
                pass
            _new_headers, new_rows = self._read_table(tab)
            updated = [row for row in new_rows if row.get(key) == key_value]
            if len(updated) != 1 or self._digest(updated[0], headers) != self._digest(
                replacement, headers
            ):
                raise SheetsTransactionError("update readback is missing, duplicated, or different")
            return dict(updated[0])

    def row_digest(self, tab: str, row: dict[str, object]) -> str:
        return self._digest(row, self._headers[tab])

    def schema_headers(self, tab: str) -> tuple[str, ...]:
        if tab not in _READABLE_KEYS or tab not in self._headers:
            raise SheetsTransactionError("V2 data tab is not available")
        return self._headers[tab]

    def read_rows(self, tab: str) -> tuple[dict[str, object], ...]:
        """Read a schema-checked snapshot of one existing V2 job tab."""
        if tab not in _READABLE_KEYS:
            raise SheetsTransactionError("only approved V2 data tabs may be read")
        _headers, rows = self._read_table(tab)
        return tuple(dict(row) for row in rows)

    def _read_table(self, tab: str) -> tuple[tuple[str, ...], list[dict[str, object]]]:
        headers = self._headers[tab]
        values = self._transport.read_values(tab, f"A1:AZ{_MAX_ROWS}")
        if not values or tuple(str(value) for value in values[0]) != headers:
            raise SheetsTransactionError("job tab headers differ from the local schema")
        rows: list[dict[str, object]] = []
        for values_row in values[1:]:
            cells = list(values_row[: len(headers)]) + [""] * max(0, len(headers) - len(values_row))
            if all(value in (None, "") for value in cells):
                continue
            rows.append(dict(zip(headers, cells, strict=True)))
        return headers, rows

    def _validate_row(self, tab: str, row: dict[str, object]) -> None:
        headers = self._headers.get(tab)
        if headers is None or tuple(row.keys()) != headers:
            raise SheetsTransactionError("job row fields must exactly match the ordered schema")
        if not isinstance(row.get(self._key(tab)), str) or not row[self._key(tab)]:
            raise SheetsTransactionError("stable job key is required")

    @staticmethod
    def _as_values(row: dict[str, object], headers: tuple[str, ...]) -> list[object]:
        return [row[name] for name in headers]

    @staticmethod
    def _digest(row: dict[str, object], headers: tuple[str, ...]) -> str:
        encoded = json.dumps(
            [row.get(name, "") for name in headers],
            ensure_ascii=False,
            sort_keys=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _key(tab: str) -> str:
        if tab not in _ALLOWED_KEYS:
            raise SheetsTransactionError("only existing V2 job tabs may be written")
        return _ALLOWED_KEYS[tab]


class _SheetWriterLockOwner(Protocol):
    writer_lock_path: Path
    thread_lock: Any


class _SheetWriterLock:
    def __init__(self, store: _SheetWriterLockOwner) -> None:
        self.store = store
        self.fd: int | None = None

    def __enter__(self):
        self.store.writer_lock_path.parent.mkdir(parents=True, exist_ok=True)
        if (
            self.store.writer_lock_path.is_symlink()
            or self.store.writer_lock_path.parent.is_symlink()
        ):
            raise SheetsTransactionError("V2 Sheet writer lock path is unsafe")
        self.store.thread_lock.acquire()
        try:
            self.fd = os.open(self.store.writer_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            if os.name == "nt":
                import msvcrt

                if os.fstat(self.fd).st_size == 0:
                    os.write(self.fd, b"0")
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_EX)
            return self
        except BaseException:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            self.store.thread_lock.release()
            raise

    def __exit__(self, *_args: object) -> None:
        if self.fd is not None:
            if os.name == "nt":
                import msvcrt

                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
        self.store.thread_lock.release()


class PromptVersionSheetsTransport(Protocol):
    """Fixed-surface transport for the approved prompt-version tab."""

    def read_prompt_values(self) -> Sequence[Sequence[object]]: ...
    def append_prompt_values(self, values: Sequence[Sequence[object]]) -> None: ...
    def batch_update_prompt_cells(self, cells: Sequence[tuple[int, str, object]]) -> None: ...


class GoogleSheetsPromptVersionApiTransport:
    """Google Sheets API access pinned to SCENE_PROMPT_VERSION in the approved file."""

    _TAB = "SCENE_PROMPT_VERSION"
    _A1_TABLE = "'SCENE_PROMPT_VERSION'!A1:N1001"
    _BASE = f"https://sheets.googleapis.com/v4/spreadsheets/{APPROVED_SPREADSHEET_ID}"

    def __init__(self, token_provider: AccessTokenProvider, *, client: httpx.Client | None = None):
        self._token_provider = token_provider
        self._client = client or httpx.Client(timeout=30.0, follow_redirects=False)
        self._owns_client = client is None

    def read_prompt_values(self) -> Sequence[Sequence[object]]:
        try:
            response = google_api_get_with_retry(
                self._client,
                self._values_url(self._A1_TABLE),
                params={"valueRenderOption": "UNFORMATTED_VALUE"},
                headers=self._headers(),
            )
            if not response.is_success:
                raise SheetsTransactionError("prompt-version Sheet read failed safely")
            payload: object = response.json()
        except Exception as exc:  # noqa: BLE001 — suppress OAuth/API details
            raise SheetsTransactionError("prompt-version Sheet read failed safely") from exc
        if not isinstance(payload, dict):
            raise SheetsTransactionError("prompt-version Sheet response shape is invalid")
        data = cast(dict[str, object], payload)
        values_value = data.get("values", [])
        if not isinstance(values_value, list):
            raise SheetsTransactionError("prompt-version Sheet response shape is invalid")
        values = cast(list[object], values_value)
        if any(not isinstance(row, list) for row in values):
            raise SheetsTransactionError("prompt-version Sheet response shape is invalid")
        return cast(Sequence[Sequence[object]], values)

    def append_prompt_values(self, values: Sequence[Sequence[object]]) -> None:
        if len(values) != 1 or len(values[0]) != 14:
            raise SheetsTransactionError("prompt-version append must contain one exact schema row")
        url = self._values_url("'SCENE_PROMPT_VERSION'!A1:N1001:append")
        self._write("POST", url, {"values": values}, params={"insertDataOption": "INSERT_ROWS"})

    def batch_update_prompt_cells(self, cells: Sequence[tuple[int, str, object]]) -> None:
        if len(cells) not in (3, 6):
            raise SheetsTransactionError("prompt lifecycle batch has an invalid cell count")
        row_columns: dict[int, set[str]] = {}
        for row, column, value in cells:
            if type(row) is not int or not 2 <= row <= 1001:
                raise SheetsTransactionError("prompt lifecycle row is outside the bounded tab")
            if column not in {"J", "K", "L", "N"} or not isinstance(value, str):
                raise SheetsTransactionError("prompt lifecycle cell is outside the approved fields")
            columns = row_columns.setdefault(row, set())
            if column in columns:
                raise SheetsTransactionError("prompt approval batch repeats a cell")
            columns.add(column)
        column_sets = {frozenset(value) for value in row_columns.values()}
        if len(cells) == 3:
            if len(row_columns) != 1 or column_sets != {frozenset(("J", "K", "N"))}:
                raise SheetsTransactionError(
                    "prompt validation batch must update only one candidate lifecycle"
                )
        elif len(row_columns) != 2 or column_sets != {
            frozenset(("J", "N")),
            frozenset(("J", "K", "L", "N")),
        }:
            raise SheetsTransactionError(
                "prompt approval batch must update one prior and one candidate row"
            )
        sheet_id = self._prompt_sheet_id()
        column_indexes = {"J": 9, "K": 10, "L": 11, "N": 13}
        requests: list[dict[str, object]] = []
        for row, column, value in cells:
            column_index = column_indexes[column]
            requests.append(
                {
                    "updateCells": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row - 1,
                            "endRowIndex": row,
                            "startColumnIndex": column_index,
                            "endColumnIndex": column_index + 1,
                        },
                        "rows": [{"values": [{"userEnteredValue": {"stringValue": value}}]}],
                        "fields": "userEnteredValue",
                    }
                }
            )
        self._post_batch(requests)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _write(
        self,
        method: str,
        url: str,
        body: dict[str, object],
        *,
        params: Mapping[str, str] | None = None,
    ) -> None:
        try:
            response = self._client.request(
                method,
                url,
                params={"valueInputOption": "RAW", **(params or {})},
                headers=self._headers(),
                json=body,
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — suppress auth/API details
            raise SheetsTransactionError(
                "prompt-version Sheet write acknowledgement is uncertain"
            ) from exc

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token_provider.access_token()}"}

    def _prompt_sheet_id(self) -> int:
        try:
            response = google_api_get_with_retry(
                self._client,
                self._BASE,
                params={"fields": "sheets(properties(sheetId,title))"},
                headers=self._headers(),
            )
            if not response.is_success:
                raise SheetsTransactionError("prompt-version Sheet metadata read failed safely")
            payload: object = response.json()
        except Exception as exc:  # noqa: BLE001 — suppress OAuth/API details
            raise SheetsTransactionError(
                "prompt-version Sheet metadata read failed safely"
            ) from exc
        if not isinstance(payload, dict):
            raise SheetsTransactionError("prompt-version Sheet metadata is invalid")
        data = cast(dict[str, object], payload)
        sheets = data.get("sheets")
        if not isinstance(sheets, list):
            raise SheetsTransactionError("prompt-version Sheet metadata is invalid")
        matches: list[object] = []
        for item in cast(list[object], sheets):
            if not isinstance(item, dict):
                continue
            item_data = cast(dict[str, object], item)
            properties = item_data.get("properties")
            if not isinstance(properties, dict):
                continue
            properties_data = cast(dict[str, object], properties)
            if properties_data.get("title") == self._TAB:
                matches.append(properties_data.get("sheetId"))
        if len(matches) != 1 or type(matches[0]) is not int:
            raise SheetsTransactionError("prompt-version tab identity is missing or ambiguous")
        return matches[0]

    def _post_batch(self, requests: Sequence[Mapping[str, object]]) -> None:
        try:
            response = self._client.post(
                f"{self._BASE}:batchUpdate",
                headers=self._headers(),
                json={"requests": requests},
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — suppress OAuth/API details
            raise SheetsTransactionError(
                "prompt lifecycle batch acknowledgement is uncertain"
            ) from exc

    @classmethod
    def _values_url(cls, a1_range: str) -> str:
        encoded = quote(a1_range, safe="'!:$")
        return f"{cls._BASE}/values/{encoded}"


class OptimisticGoogleSheetsPromptVersionStore:
    """Dedicated optimistic writer for prompt versions; never writes other tabs."""

    _TAB = "SCENE_PROMPT_VERSION"
    _HEADERS = (
        "Scene_Prompt_ID",
        "Scene_ID",
        "Prompt_Version",
        "Generation_Profile_ID",
        "Compiled_Prompt_Text",
        "Compiled_Negative_Prompt",
        "Compiler_Version",
        "Source_Context_SHA256",
        "Prompt_SHA256",
        "Prompt_Status",
        "Validation_Message",
        "Approved_At",
        "Created_At",
        "Updated_At",
    )

    def __init__(
        self,
        transport: PromptVersionSheetsTransport,
        schema_dir: Path,
        *,
        writer_lock_root: Path,
    ) -> None:
        if not writer_lock_root.is_absolute() or writer_lock_root == Path(writer_lock_root.anchor):
            raise SheetsTransactionError("writer lock root must be absolute and non-root")
        try:
            headers = load_schema_headers(schema_dir)
        except GoogleSheetsReadError as exc:
            raise SheetsTransactionError("V2 transaction schemas are unavailable") from exc
        if headers.get(self._TAB) != self._HEADERS:
            raise SheetsTransactionError("prompt-version schema differs from the approved contract")
        self._transport = transport
        self.writer_lock_path = writer_lock_root / ".v2-sheets-writer.lock"
        self.thread_lock = threading.RLock()

    def read_prompt_rows(self) -> list[dict[str, object]]:
        return self._read_table()[1]

    def append_candidate(
        self,
        row: dict[str, object],
        *,
        expected_snapshot_sha256: str,
        fresh_phase_e: object,
    ) -> dict[str, object]:
        from .phase_e import PreflightResult
        from .prompt_approval import PromptApprovalIntegrityError, guard_scene_prompt_version_update
        from .prompt_version_lifecycle import (
            prompt_row_sha256,
            prompt_snapshot_sha256,
        )

        if not isinstance(fresh_phase_e, PreflightResult):
            raise SheetsTransactionError(
                "fresh Phase E evidence is required for candidate creation"
            )
        self._validate_row(row)
        try:
            guard_scene_prompt_version_update(None, row)
        except PromptApprovalIntegrityError as exc:
            raise SheetsTransactionError("prompt candidate lifecycle state is invalid") from exc
        self._require_exact_phase_e(row, fresh_phase_e)
        with _SheetWriterLock(self):
            headers, rows = self._read_table()
            if prompt_snapshot_sha256(rows) != expected_snapshot_sha256:
                raise SheetsTransactionError("prompt table changed during candidate allocation")
            if any(item.get("Scene_Prompt_ID") == row["Scene_Prompt_ID"] for item in rows):
                raise SheetsTransactionError("prompt ID already exists")
            if any(
                item.get("Scene_ID") == row["Scene_ID"]
                and self._as_version(item.get("Prompt_Version")) == row["Prompt_Version"]
                for item in rows
            ):
                raise SheetsTransactionError("prompt version already exists")
            try:
                self._transport.append_prompt_values([[row[name] for name in headers]])
            except SheetsTransactionError:
                pass
            _new_headers, new_rows = self._read_table()
            matches = [
                item for item in new_rows if item.get("Scene_Prompt_ID") == row["Scene_Prompt_ID"]
            ]
            if len(matches) != 1 or prompt_row_sha256(matches[0]) != prompt_row_sha256(row):
                raise SheetsTransactionError(
                    "prompt candidate append readback is missing, duplicated, or different"
                )
            return matches[0]

    def update_candidate(
        self,
        row: dict[str, object],
        *,
        expected_row_sha256: str,
        fresh_phase_e: object,
    ) -> dict[str, object]:
        from .phase_e import PreflightResult
        from .prompt_approval import PromptApprovalIntegrityError, guard_scene_prompt_version_update
        from .prompt_version_lifecycle import prompt_row_sha256

        if not isinstance(fresh_phase_e, PreflightResult):
            raise SheetsTransactionError("fresh Phase E evidence is required for validation")
        self._validate_row(row)
        with _SheetWriterLock(self):
            _headers, rows = self._read_table()
            located = self._locate(rows, str(row["Scene_Prompt_ID"]))
            if len(located) != 1:
                raise SheetsTransactionError("prompt candidate is missing or duplicated")
            row_number, current = located[0]
            if prompt_row_sha256(current) != expected_row_sha256:
                raise SheetsTransactionError("prompt candidate changed since validation preflight")
            try:
                guard_scene_prompt_version_update(current, row, fresh_phase_e=fresh_phase_e)
            except PromptApprovalIntegrityError as exc:
                raise SheetsTransactionError("prompt validation transition is invalid") from exc
            cells = [
                (row_number, "J", str(row["Prompt_Status"])),
                (row_number, "K", str(row["Validation_Message"])),
                (row_number, "N", str(row["Updated_At"])),
            ]
            try:
                self._transport.batch_update_prompt_cells(cells)
            except SheetsTransactionError:
                pass
            _new_headers, new_rows = self._read_table()
            updated = [
                item for item in new_rows if item.get("Scene_Prompt_ID") == row["Scene_Prompt_ID"]
            ]
            if len(updated) != 1 or prompt_row_sha256(updated[0]) != prompt_row_sha256(row):
                raise SheetsTransactionError("prompt validation readback is missing or different")
            return updated[0]

    def approve_pair(
        self,
        previous: dict[str, object],
        candidate: dict[str, object],
        *,
        expected_previous_sha256: str,
        expected_candidate_sha256: str,
        fresh_phase_e: object,
    ) -> tuple[dict[str, object], dict[str, object]]:
        from .phase_e import PreflightResult
        from .prompt_approval import (
            PromptApprovalIntegrityError,
            guard_scene_prompt_version_update,
            require_single_current_approval,
        )
        from .prompt_version_lifecycle import prompt_row_sha256

        if not isinstance(fresh_phase_e, PreflightResult):
            raise SheetsTransactionError("fresh Phase E evidence is required for approval")
        self._validate_row(previous)
        self._validate_row(candidate)
        with _SheetWriterLock(self):
            _headers, rows = self._read_table()
            old_located = self._locate(rows, str(previous["Scene_Prompt_ID"]))
            new_located = self._locate(rows, str(candidate["Scene_Prompt_ID"]))
            if len(old_located) != 1 or len(new_located) != 1:
                raise SheetsTransactionError("approval rows are missing or duplicated")
            old_row_number, old_current = old_located[0]
            new_row_number, new_current = new_located[0]
            if old_row_number == new_row_number:
                raise SheetsTransactionError("approval rows resolve to the same row")
            if old_current.get("Scene_ID") != new_current.get("Scene_ID") or old_current.get(
                "Generation_Profile_ID"
            ) != new_current.get("Generation_Profile_ID"):
                raise SheetsTransactionError(
                    "approval rows belong to different Scene/profile identities"
                )
            if (
                prompt_row_sha256(old_current) != expected_previous_sha256
                or prompt_row_sha256(new_current) != expected_candidate_sha256
            ):
                raise SheetsTransactionError("prompt lifecycle changed since approval preflight")
            try:
                guard_scene_prompt_version_update(old_current, previous)
                guard_scene_prompt_version_update(
                    new_current, candidate, fresh_phase_e=fresh_phase_e
                )
                require_single_current_approval(
                    rows,
                    scene_id=str(candidate["Scene_ID"]),
                    generation_profile_id=str(candidate["Generation_Profile_ID"]),
                )
            except PromptApprovalIntegrityError as exc:
                raise SheetsTransactionError("prompt approval transition is invalid") from exc
            cells = [
                (old_row_number, "J", str(previous["Prompt_Status"])),
                (old_row_number, "N", str(previous["Updated_At"])),
                (new_row_number, "J", str(candidate["Prompt_Status"])),
                (new_row_number, "K", str(candidate["Validation_Message"])),
                (new_row_number, "L", str(candidate["Approved_At"])),
                (new_row_number, "N", str(candidate["Updated_At"])),
            ]
            try:
                self._transport.batch_update_prompt_cells(cells)
            except SheetsTransactionError:
                pass
            _new_headers, new_rows = self._read_table()
            old_readback = self._locate(new_rows, str(previous["Scene_Prompt_ID"]))
            new_readback = self._locate(new_rows, str(candidate["Scene_Prompt_ID"]))
            if (
                len(old_readback) != 1
                or len(new_readback) != 1
                or prompt_row_sha256(old_readback[0][1]) != prompt_row_sha256(previous)
                or prompt_row_sha256(new_readback[0][1]) != prompt_row_sha256(candidate)
            ):
                raise SheetsTransactionError(
                    "prompt approval batch readback is missing or different"
                )
            try:
                require_single_current_approval(
                    new_rows,
                    scene_id=str(candidate["Scene_ID"]),
                    generation_profile_id=str(candidate["Generation_Profile_ID"]),
                )
            except PromptApprovalIntegrityError as exc:
                raise SheetsTransactionError("prompt approval readback is ambiguous") from exc
            return old_readback[0][1], new_readback[0][1]

    def _read_table(self) -> tuple[tuple[str, ...], list[dict[str, object]]]:
        values = self._transport.read_prompt_values()
        if not values or tuple(str(value) for value in values[0]) != self._HEADERS:
            raise SheetsTransactionError("prompt-version headers differ from the local schema")
        rows: list[dict[str, object]] = []
        encountered_blank = False
        for values_row in values[1:]:
            if len(values_row) > len(self._HEADERS) and any(
                value not in (None, "") for value in values_row[len(self._HEADERS) :]
            ):
                raise SheetsTransactionError("prompt-version row exceeds the local schema")
            cells = list(values_row[: len(self._HEADERS)]) + [""] * max(
                0, len(self._HEADERS) - len(values_row)
            )
            if all(value in (None, "") for value in cells):
                encountered_blank = True
                continue
            if encountered_blank:
                raise SheetsTransactionError("prompt-version tab contains a blank row gap")
            rows.append(
                dict(zip(self._HEADERS, ("" if v is None else v for v in cells), strict=True))
            )
        return self._HEADERS, rows

    @staticmethod
    def _validate_row(row: dict[str, object]) -> None:
        from .prompt_version_lifecycle import PROMPT_VERSION_FIELDS

        if tuple(row.keys()) != PROMPT_VERSION_FIELDS:
            raise SheetsTransactionError("prompt row fields must exactly match the ordered schema")

    @staticmethod
    def _locate(
        rows: Sequence[dict[str, object]], prompt_id: str
    ) -> list[tuple[int, dict[str, object]]]:
        return [
            (i, row)
            for i, row in enumerate(rows, start=2)
            if row.get("Scene_Prompt_ID") == prompt_id
        ]

    @staticmethod
    def _as_version(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise SheetsTransactionError("persisted prompt version is invalid")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise SheetsTransactionError("persisted prompt version is invalid") from exc

    @staticmethod
    def _require_exact_phase_e(row: Mapping[str, object], result: object) -> None:
        from .phase_e import PreflightResult

        if not isinstance(result, PreflightResult):
            raise SheetsTransactionError("fresh Phase E evidence is invalid")
        expected = {
            "Scene_ID": result.scene_id,
            "Generation_Profile_ID": result.generation_profile_id,
            "Compiled_Prompt_Text": result.compiled_prompt,
            "Compiled_Negative_Prompt": result.compiled_negative_prompt or "",
            "Source_Context_SHA256": result.source_context_sha256,
            "Prompt_SHA256": result.prompt_sha256,
        }
        if (
            result.status != "PREFLIGHT_PASS"
            or not result.ready_for_provider_adapter
            or result.errors
            or any(row.get(key) != val for key, val in expected.items())
        ):
            raise SheetsTransactionError("prompt candidate differs from fresh Phase E")

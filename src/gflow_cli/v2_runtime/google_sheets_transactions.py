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
from typing import Protocol, cast
from urllib.parse import quote

import httpx

from .google_sheets import (
    APPROVED_SPREADSHEET_ID,
    AccessTokenProvider,
    GoogleSheetsReadError,
    load_schema_headers,
)

_ALLOWED_KEYS = {
    "PRODUCTION_JOBS": "Production_Job_ID",
    "SCENE_JOBS": "Scene_Job_ID",
}
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
        if tab not in _ALLOWED_KEYS or not bounded:
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
        if not set(_ALLOWED_KEYS).issubset(self._headers):
            raise SheetsTransactionError("V2 Production/Scene Job schemas are missing")
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


class _SheetWriterLock:
    def __init__(self, store: OptimisticGoogleSheetsJobStore) -> None:
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

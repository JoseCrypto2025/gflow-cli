from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from gflow_cli.v2_runtime.google_sheets import (
    APPROVED_SPREADSHEET_ID,
    GOOGLE_DRIVE_FILE_SCOPE,
    SOURCE_LIVE_GOOGLE_SHEETS,
    GoogleSheetsApiTransport,
    GoogleSheetsReadError,
    GoogleSheetsSnapshot,
    GoogleSheetsSnapshotSource,
    snapshot_document,
    validate_v2_snapshot,
    write_snapshot_atomic,
)


def _schema_dir(path: Path, *, name: str = "DEMO") -> Path:
    (path / "master").mkdir(parents=True)
    (path / "transaction").mkdir()
    (path / "relation").mkdir()
    schema = {
        "table_name": name,
        "table_type": "master",
        "primary_key": "Record_ID",
        "fields": [
            {"field_name": "Record_ID", "required": True, "data_type": "STRING"},
            {"field_name": "Value", "required": False, "data_type": "STRING"},
        ],
    }
    (path / "master" / f"{name}.schema.json").write_text(json.dumps(schema), encoding="utf-8")
    return path


class FakeTransport:
    def __init__(self, values: Sequence[Sequence[object]] | None = None) -> None:
        self.values = values if values is not None else [["Record_ID", "Value"], ["ID_0001", "ok"]]
        self.calls: list[tuple[str, str, str]] = []
        self.batch_calls: list[tuple[str, tuple[tuple[str, str], ...]]] = []
        self.failure = False

    def read_values(
        self, spreadsheet_id: str, sheet_name: str, value_range: str
    ) -> Sequence[Sequence[object]]:
        self.calls.append((spreadsheet_id, sheet_name, value_range))
        if self.failure:
            raise RuntimeError("contains fake token=never-print")
        return self.values

    def read_values_batch(
        self, spreadsheet_id: str, ranges: Mapping[str, str]
    ) -> Mapping[str, Sequence[Sequence[object]]]:
        self.batch_calls.append((spreadsheet_id, tuple(ranges.items())))
        if self.failure:
            raise RuntimeError("contains fake token=never-print")
        return {name: self.values for name in ranges}


def test_source_reads_only_approved_spreadsheet_and_sets_producer_timestamp(tmp_path: Path) -> None:
    transport = FakeTransport()
    read_at = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    source = GoogleSheetsSnapshotSource(
        transport,
        _schema_dir(tmp_path / "schemas"),
        validator=lambda _schema, _tables: [],
        clock=lambda: read_at,
    )
    snapshot = source.read()
    assert snapshot.spreadsheet_id == APPROVED_SPREADSHEET_ID
    assert snapshot.read_at == "2026-10-18T12:00:00Z"
    assert snapshot.source == SOURCE_LIVE_GOOGLE_SHEETS
    assert snapshot.tables["DEMO"] == [{"Record_ID": "ID_0001", "Value": "ok"}]
    assert transport.calls == []
    assert transport.batch_calls == [(APPROVED_SPREADSHEET_ID, (("DEMO", "A1:AZ1001"),))]
    assert not hasattr(transport, "write_values")


def test_source_rejects_an_unapproved_spreadsheet_before_read(tmp_path: Path) -> None:
    transport = FakeTransport()
    with pytest.raises(GoogleSheetsReadError, match="unapproved"):
        GoogleSheetsSnapshotSource(
            transport, _schema_dir(tmp_path / "schemas"), spreadsheet_id="wrong"
        )
    assert transport.calls == []


@pytest.mark.parametrize(
    "values",
    [
        [],
        [["Value", "Record_ID"], ["x", "ID_0001"]],
        [["Record_ID", "Value"], ["ID_0001", "x", "unexpected"]],
    ],
)
def test_source_rejects_missing_or_changed_headers_and_extra_columns(
    tmp_path: Path, values: Sequence[Sequence[object]]
) -> None:
    source = GoogleSheetsSnapshotSource(
        FakeTransport(values), _schema_dir(tmp_path / "schemas"), validator=lambda *_: []
    )
    with pytest.raises(GoogleSheetsReadError):
        source.read()


def test_source_rejects_validator_schema_defects_without_snapshot(tmp_path: Path) -> None:
    source = GoogleSheetsSnapshotSource(
        FakeTransport(),
        _schema_dir(tmp_path / "schemas"),
        validator=lambda *_: ["duplicate primary key", "orphan FK"],
    )
    with pytest.raises(GoogleSheetsReadError, match="failed V2 validation"):
        source.read()


def test_full_schema_validator_rejects_duplicate_ids_and_orphan_account_fk() -> None:
    schema_dir = Path(__file__).resolve().parents[2] / "schemas"
    from scripts.validation.validate_v2_data_architecture import load_schemas

    tables = {name: [] for name in load_schemas(schema_dir)}
    provider = {
        "Provider_ID": "PRV_0001",
        "Provider_Name": "Flow",
        "Provider_Type": "VIDEO",
        "Execution_Mode": "WEB_UI",
        "Billing_Mode": "CREDIT",
        "Status": "ACTIVE",
    }
    tables["PROVIDER_MASTER"] = [dict(provider), dict(provider)]
    account = {
        "Provider_Account_ID": "PVA_9999",
        "Provider_ID": "PRV_9999",
        "Account_Name": "Bad FK",
        "Environment": "TEST",
        "Execution_Target": "WEB_UI",
        "Auth_Profile_Key": "GFLOW_TEST_AUTH",
        "Base_URL": "https://flow.google.com/",
        "Is_Default": "FALSE",
        "Status": "ACTIVE",
    }
    tables["PROVIDER_ACCOUNT_MASTER"] = [account]
    errors = validate_v2_snapshot(schema_dir, tables)
    assert any("duplicate primary key PRV_0001" in error for error in errors)
    assert any("orphan FK Provider_ID=PRV_9999" in error for error in errors)


def test_credential_file_must_be_outside_checkout_and_private(tmp_path: Path) -> None:
    from gflow_cli.v2_runtime.google_sheets import GoogleWorkspaceCredentialProvider

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    credential = checkout / "credential.json"
    credential.write_text("{}", encoding="utf-8")
    with pytest.raises(GoogleSheetsReadError, match="outside the checkout"):
        GoogleWorkspaceCredentialProvider(credential, repository_root=checkout)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file mode enforcement is VPS-specific")
def test_credential_file_rejects_broad_permissions(tmp_path: Path) -> None:
    from gflow_cli.v2_runtime.google_sheets import GoogleWorkspaceCredentialProvider

    credential = tmp_path / "credential.json"
    credential.write_text("{}", encoding="utf-8")
    credential.chmod(0o644)
    with pytest.raises(GoogleSheetsReadError, match="permissions are too broad"):
        GoogleWorkspaceCredentialProvider(credential, repository_root=tmp_path / "checkout")


def test_source_fails_closed_when_transport_fails_and_hides_exception_text(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.failure = True
    source = GoogleSheetsSnapshotSource(
        transport, _schema_dir(tmp_path / "schemas"), validator=lambda *_: []
    )
    with pytest.raises(GoogleSheetsReadError) as error:
        source.read()
    assert "never-print" not in str(error.value)


def test_snapshot_write_is_atomic_private_and_under_runtime_root(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    folder = runtime / "snapshots"
    folder.mkdir(parents=True)
    target = folder / "current.json"
    target.write_text("old", encoding="utf-8")
    snapshot = GoogleSheetsSnapshot(
        APPROVED_SPREADSHEET_ID,
        "2026-10-18T12:00:00Z",
        SOURCE_LIVE_GOOGLE_SHEETS,
        {"DEMO": [{"Record_ID": "ID_0001"}]},
    )
    write_snapshot_atomic(snapshot, target, runtime_root=runtime)
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document == snapshot_document(snapshot)
    assert not list(folder.glob(".gflow-snapshot-*"))
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_snapshot_write_rejects_path_outside_runtime_root(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    outside = tmp_path / "outside.json"
    snapshot = GoogleSheetsSnapshot(
        APPROVED_SPREADSHEET_ID, "2026-10-18T12:00:00Z", SOURCE_LIVE_GOOGLE_SHEETS, {}
    )
    with pytest.raises(GoogleSheetsReadError, match="runtime root"):
        write_snapshot_atomic(snapshot, outside, runtime_root=runtime)
    assert not outside.exists()


def test_rest_transport_uses_scoped_token_and_only_gets_approved_values() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"values": [["ID", "Value"]]})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    class Tokens:
        def access_token(self) -> str:
            return "secret-token"

    adapter = GoogleSheetsApiTransport(Tokens(), client=client)
    assert adapter.read_values(APPROVED_SPREADSHEET_ID, "DEMO", "A1:AZ1001") == [["ID", "Value"]]
    assert seen["method"] == "GET"
    assert seen["auth"] == "Bearer secret-token"
    assert "sheets.googleapis.com" in str(seen["url"])
    assert not hasattr(adapter, "write_values")
    assert GOOGLE_DRIVE_FILE_SCOPE == "https://www.googleapis.com/auth/drive.file"
    adapter.close()
    client.close()


def test_batch_get_uses_one_ordered_request_and_maps_every_range() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "valueRanges": [
                    {"range": "FIRST!A1:AZ1001", "values": [["ID", "Value"]]},
                    {"range": "SECOND!A1:AZ1001", "values": [["ID", "Value"]]},
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    class Tokens:
        def access_token(self) -> str:
            return "secret-token"

    adapter = GoogleSheetsApiTransport(Tokens(), client=client)
    values = adapter.read_values_batch(
        APPROVED_SPREADSHEET_ID,
        {"FIRST": "A1:AZ1001", "SECOND": "A1:AZ1001"},
    )
    assert values == {
        "FIRST": [["ID", "Value"]],
        "SECOND": [["ID", "Value"]],
    }
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path.endswith("/values:batchGet")
    assert seen[0].url.params.get_list("ranges") == [
        "FIRST!A1:AZ1001",
        "SECOND!A1:AZ1001",
    ]
    assert seen[0].headers["Authorization"] == "Bearer secret-token"
    adapter.close()
    client.close()


def test_batch_get_accepts_canonical_quoted_absolute_ranges_and_empty_values() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "valueRanges": [
                    {"range": "'FIRST'!$A$1:$AZ$1001", "values": [["ID", "Value"]]},
                    {"range": "'SECOND'!$A$1:$AZ$1001"},
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    class Tokens:
        def access_token(self) -> str:
            return "secret-token"

    adapter = GoogleSheetsApiTransport(Tokens(), client=client)
    assert adapter.read_values_batch(
        APPROVED_SPREADSHEET_ID,
        {"FIRST": "A1:AZ1001", "SECOND": "A1:AZ1001"},
    ) == {"FIRST": [["ID", "Value"]], "SECOND": []}
    adapter.close()
    client.close()


@pytest.mark.parametrize(
    ("returned_range", "expected_title"),
    [
        ("Sheet1!A1:B2", "Sheet1"),
        ("'My Custom Sheet'!$A$1:$B$2", "My Custom Sheet"),
        ("'Jon''s_Data'!A1:B2", "Jon's_Data"),
    ],
)
def test_a1_range_parser_handles_quoted_titles_and_absolute_cells(
    returned_range: str, expected_title: str
) -> None:
    from gflow_cli.v2_runtime.google_sheets import _parse_a1_range

    parsed = _parse_a1_range(returned_range)
    assert parsed is not None
    assert parsed.sheet_name == expected_title
    assert parsed.bounds == (1, 1, 2, 2)


def test_batch_get_reports_safe_diagnostic_for_range_identity_mismatch() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "valueRanges": [
                    {"range": "SECOND!A1:AZ1001", "values": []},
                    {"range": "FIRST!A1:AZ1001", "values": []},
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    class Tokens:
        def access_token(self) -> str:
            return "secret-token"

    adapter = GoogleSheetsApiTransport(Tokens(), client=client)
    with pytest.raises(GoogleSheetsReadError) as error:
        adapter.read_values_batch(
            APPROVED_SPREADSHEET_ID,
            {"FIRST": "A1:AZ1001", "SECOND": "A1:AZ1001"},
        )
    message = str(error.value)
    assert "BATCH_RESPONSE_COUNT=2" in message
    assert "EXPECTED_RANGE_COUNT=2" in message
    assert "FAILED_INDEX=0" in message
    assert "REQUESTED_RANGE=FIRST!A1:AZ1001" in message
    assert "RETURNED_RANGE=SECOND!A1:AZ1001" in message
    assert "FAILURE_REASON=sheet_mismatch" in message
    assert "secret-token" not in message
    adapter.close()
    client.close()


def test_full_34_schema_snapshot_uses_one_batch_and_keeps_validation(tmp_path: Path) -> None:
    schema_dir = Path(__file__).resolve().parents[2] / "schemas"
    from gflow_cli.v2_runtime.google_sheets import load_schema_headers

    headers = load_schema_headers(schema_dir)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        ranges = request.url.params.get_list("ranges")
        value_ranges = []
        for a1_range in ranges:
            sheet_name = a1_range.split("!", 1)[0].strip("'")
            value_ranges.append(
                {
                    "range": f"'{sheet_name}'!$A$1:$AZ$1001",
                    "values": [list(headers[sheet_name]), [], []],
                }
            )
        return httpx.Response(200, json={"valueRanges": value_ranges})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    class Tokens:
        def access_token(self) -> str:
            return "secret-token"

    adapter = GoogleSheetsApiTransport(Tokens(), client=client)
    snapshot = GoogleSheetsSnapshotSource(adapter, schema_dir).read()
    assert len(snapshot.tables) == 34
    assert all(rows == [] for rows in snapshot.tables.values())
    assert len(seen) == 1
    assert len(seen[0].url.params.get_list("ranges")) == 34

    sequential_values = {
        table_name: [list(table_headers), [], []] for table_name, table_headers in headers.items()
    }

    class SequentialTransport:
        def read_values(
            self, spreadsheet_id: str, sheet_name: str, value_range: str
        ) -> Sequence[Sequence[object]]:
            del spreadsheet_id, value_range
            return sequential_values[sheet_name]

    sequential_snapshot = GoogleSheetsSnapshotSource(SequentialTransport(), schema_dir).read()
    assert snapshot.tables == sequential_snapshot.tables
    adapter.close()
    client.close()


@pytest.mark.parametrize("response_count", [0, 2])
def test_batch_get_range_count_mismatch_has_bounded_diagnostic(response_count: int) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "valueRanges": [
                    {"range": "FIRST!A1:AZ1001", "values": []} for _ in range(response_count)
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    class Tokens:
        def access_token(self) -> str:
            return "secret-token"

    adapter = GoogleSheetsApiTransport(Tokens(), client=client)
    with pytest.raises(GoogleSheetsReadError) as error:
        adapter.read_values_batch(APPROVED_SPREADSHEET_ID, {"FIRST": "A1:AZ1001"})
    message = str(error.value)
    assert f"BATCH_RESPONSE_COUNT={response_count}" in message
    assert "EXPECTED_RANGE_COUNT=1" in message
    assert "FAILED_INDEX=<none>" in message
    assert "FAILURE_REASON=range_count_mismatch" in message
    assert "secret-token" not in message
    adapter.close()
    client.close()


@pytest.mark.parametrize("status", [401, 403])
def test_unauthorized_and_forbidden_reads_are_not_retried(status: int) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, text="secret-token")

    sleeps: list[float] = []
    client = httpx.Client(transport=httpx.MockTransport(handler))

    class Tokens:
        def access_token(self) -> str:
            return "secret-token"

    adapter = GoogleSheetsApiTransport(Tokens(), client=client, sleeper=sleeps.append)
    with pytest.raises(GoogleSheetsReadError):
        adapter.read_values(APPROVED_SPREADSHEET_ID, "DEMO", "A1:AZ1001")
    assert calls == 1
    assert sleeps == []
    adapter.close()
    client.close()


def test_429_retries_with_retry_after_and_succeeds() -> None:
    statuses = [429, 429, 200]
    calls = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        status = statuses[calls]
        calls += 1
        if status == 429:
            return httpx.Response(status, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"values": [["ID", "Value"]]})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    class Tokens:
        def access_token(self) -> str:
            return "secret-token"

    adapter = GoogleSheetsApiTransport(
        Tokens(), client=client, sleeper=sleeps.append, random_uniform=lambda low, _high: low
    )
    assert adapter.read_values(APPROVED_SPREADSHEET_ID, "DEMO", "A1:AZ1001") == [["ID", "Value"]]
    assert calls == 3
    assert sleeps == [3.0, 3.0]
    adapter.close()
    client.close()


def test_retry_after_http_date_is_honored() -> None:
    now = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    calls = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "Sun, 18 Oct 2026 12:00:04 GMT"})
        return httpx.Response(200, json={"values": [["ID", "Value"]]})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    class Tokens:
        def access_token(self) -> str:
            return "secret-token"

    adapter = GoogleSheetsApiTransport(
        Tokens(), client=client, sleeper=sleeps.append, now=lambda: now
    )
    assert adapter.read_values(APPROVED_SPREADSHEET_ID, "DEMO", "A1:AZ1001")
    assert calls == 2
    assert sleeps == [4.0]
    adapter.close()
    client.close()


def test_transient_5xx_retries_but_stops_after_success() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"values": [["ID", "Value"]]})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    class Tokens:
        def access_token(self) -> str:
            return "secret-token"

    adapter = GoogleSheetsApiTransport(
        Tokens(), client=client, sleeper=sleeps.append, random_uniform=lambda low, _high: low
    )
    assert adapter.read_values(APPROVED_SPREADSHEET_ID, "DEMO", "A1:AZ1001")
    assert calls == 2
    assert sleeps == [0.8]
    adapter.close()
    client.close()


def test_429_fallback_backoff_is_bounded_and_exhaustion_is_typed() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    class Tokens:
        def access_token(self) -> str:
            return "secret-token"

    adapter = GoogleSheetsApiTransport(
        Tokens(), client=client, sleeper=sleeps.append, random_uniform=lambda low, _high: low
    )
    with pytest.raises(GoogleSheetsReadError) as exc:
        adapter.read_values(APPROVED_SPREADSHEET_ID, "DEMO", "A1:AZ1001")
    assert getattr(exc.value, "status_code", None) == 429
    assert calls == 7
    assert sleeps == [0.8, 1.6, 3.2, 6.4, 12.8, 25.6]
    assert sum(sleeps) <= 90
    client.close()


def test_rest_transport_rejects_other_sheet_and_redacts_api_errors() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(403, text="token leaked"))
    )

    class Tokens:
        def access_token(self) -> str:
            return "secret-token"

    adapter = GoogleSheetsApiTransport(Tokens(), client=client)
    with pytest.raises(GoogleSheetsReadError, match="unapproved"):
        adapter.read_values("other-sheet", "DEMO", "A1:AZ1001")
    with pytest.raises(GoogleSheetsReadError) as error:
        adapter.read_values(APPROVED_SPREADSHEET_ID, "DEMO", "A1:AZ1001")
    assert "token leaked" not in str(error.value)
    adapter.close()
    client.close()

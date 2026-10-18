from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from gflow_cli.v2_runtime.google_sheets_transactions import (
    OptimisticGoogleSheetsJobStore,
    SheetsTransactionError,
)


class FakeSheets:
    def __init__(self, schema_dir: Path) -> None:
        import json

        self.tables: dict[str, list[list[object]]] = {}
        for tab in ("PRODUCTION_JOBS", "SCENE_JOBS"):
            schema = json.loads((schema_dir / "transaction" / f"{tab}.schema.json").read_text())
            self.tables[tab] = [[field["field_name"] for field in schema["fields"]]]
        self.fail_after_append = False
        self.fail_after_update = False

    def read_values(self, tab: str, _range: str) -> Sequence[Sequence[object]]:
        return [list(row) for row in self.tables[tab]]

    def append_values(self, tab: str, values: Sequence[Sequence[object]]) -> None:
        self.tables[tab].extend([list(row) for row in values])
        if self.fail_after_append:
            self.fail_after_append = False
            raise SheetsTransactionError("acknowledgement uncertain")

    def update_values(self, tab: str, a1_range: str, values: Sequence[Sequence[object]]) -> None:
        start = int(a1_range.split(":")[0][1:])
        self.tables[tab][start - 1] = list(values[0])
        if self.fail_after_update:
            self.fail_after_update = False
            raise SheetsTransactionError("acknowledgement uncertain")


def _schema_dir() -> Path:
    return Path(__file__).parents[2] / "schemas"


def _row(tab: str, **changes: object) -> dict[str, object]:
    import json

    schema = json.loads(
        (_schema_dir() / "transaction" / f"{tab}.schema.json").read_text(encoding="utf-8")
    )
    values = {field["field_name"]: "" for field in schema["fields"]}
    values.update(changes)
    return values


def _store(tmp_path: Path, transport: FakeSheets) -> OptimisticGoogleSheetsJobStore:
    return OptimisticGoogleSheetsJobStore(
        transport, _schema_dir(), writer_lock_root=tmp_path / "runtime"
    )


def test_append_claim_is_idempotent_and_lost_ack_is_read_back(tmp_path: Path) -> None:
    transport = FakeSheets(_schema_dir())
    transport.fail_after_append = True
    store = _store(tmp_path, transport)
    row = _row(
        "PRODUCTION_JOBS",
        Production_Job_ID="PRD_000001",
        Request_Idempotency_Key="request-1",
        Requested_At="2026-10-18T00:00:00Z",
    )

    assert store.append_if_absent("PRODUCTION_JOBS", row) == row
    assert store.append_if_absent("PRODUCTION_JOBS", row) == row
    assert len(transport.tables["PRODUCTION_JOBS"]) == 2


def test_append_conflict_never_overwrites_existing_key(tmp_path: Path) -> None:
    transport = FakeSheets(_schema_dir())
    store = _store(tmp_path, transport)
    first = _row(
        "PRODUCTION_JOBS",
        Production_Job_ID="PRD_000001",
        Request_Idempotency_Key="request-1",
        Requested_At="2026-10-18T00:00:00Z",
    )
    store.append_if_absent("PRODUCTION_JOBS", first)
    changed = {**first, "Job_Status": "RUNNING"}

    with pytest.raises(SheetsTransactionError, match="bound to different"):
        store.append_if_absent("PRODUCTION_JOBS", changed)
    assert transport.tables["PRODUCTION_JOBS"][1][7] == ""


def test_update_requires_expected_digest_and_verifies_lost_ack(tmp_path: Path) -> None:
    transport = FakeSheets(_schema_dir())
    store = _store(tmp_path, transport)
    row = _row(
        "SCENE_JOBS",
        Scene_Job_ID="SJB_000001",
        Production_Job_ID="PRD_000001",
        Scene_Prompt_ID="SPV_000006",
        Attempt_No=1,
        Job_Status="PENDING",
    )
    store.append_if_absent("SCENE_JOBS", row)
    replacement = {**row, "Job_Status": "VALIDATING"}
    expected = store.row_digest("SCENE_JOBS", row)
    transport.fail_after_update = True

    assert (
        store.update_expected(
            "SCENE_JOBS",
            key_value="SJB_000001",
            expected_digest=expected,
            replacement=replacement,
        )
        == replacement
    )
    with pytest.raises(SheetsTransactionError, match="changed since"):
        store.update_expected(
            "SCENE_JOBS",
            key_value="SJB_000001",
            expected_digest=expected,
            replacement=row,
        )


def test_store_rejects_other_sheets_and_wrong_ordered_fields(tmp_path: Path) -> None:
    transport = FakeSheets(_schema_dir())
    store = _store(tmp_path, transport)
    with pytest.raises(SheetsTransactionError, match="only existing"):
        store.append_if_absent("SCENE_PROMPT_VERSION", {})
    row = _row(
        "PRODUCTION_JOBS",
        Production_Job_ID="PRD_000001",
        Request_Idempotency_Key="request-1",
        Requested_At="2026-10-18T00:00:00Z",
    )
    reversed_row = dict(reversed(tuple(row.items())))
    with pytest.raises(SheetsTransactionError, match="ordered schema"):
        store.append_if_absent("PRODUCTION_JOBS", reversed_row)

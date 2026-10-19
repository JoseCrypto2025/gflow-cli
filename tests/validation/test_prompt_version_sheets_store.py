from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest

from gflow_cli.v2_runtime.google_sheets_transactions import (
    GoogleSheetsPromptVersionApiTransport,
    OptimisticGoogleSheetsPromptVersionStore,
    SheetsTransactionError,
)
from gflow_cli.v2_runtime.phase_e import PreflightResult
from gflow_cli.v2_runtime.prompt_version_lifecycle import prompt_row_sha256, prompt_snapshot_sha256

_FIELDS = (
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


def _phase_e() -> PreflightResult:
    return PreflightResult(
        status="PREFLIGHT_PASS",
        ready_for_provider_adapter=True,
        scene_id="SCN_000001",
        generation_profile_id="GPR_0002",
        compiled_prompt="fresh",
        compiled_negative_prompt="",
        source_context_sha256="a" * 64,
        prompt_sha256="b" * 64,
        errors=(),
    )


def _row(
    prompt_id: str, status: str, *, version: int, text: str, source: str, prompt: str
) -> dict[str, object]:
    values = {
        "Scene_Prompt_ID": prompt_id,
        "Scene_ID": "SCN_000001",
        "Prompt_Version": version,
        "Generation_Profile_ID": "GPR_0002",
        "Compiled_Prompt_Text": text,
        "Compiled_Negative_Prompt": "",
        "Compiler_Version": "phase-e/1",
        "Source_Context_SHA256": source,
        "Prompt_SHA256": prompt,
        "Prompt_Status": status,
        "Validation_Message": "PREFLIGHT_PASS",
        "Approved_At": "2026-10-18T00:00:00Z" if status == "APPROVED" else "",
        "Created_At": "2026-10-18T00:00:00Z",
        "Updated_At": "2026-10-18T00:00:00Z",
    }
    return {name: values[name] for name in _FIELDS}


class FakePromptTransport:
    def __init__(self) -> None:
        self.rows: list[list[object]] = [list(_FIELDS)]
        self.append_count = 0
        self.update_count = 0
        self.batch_count = 0
        self.fail_append = False
        self.fail_append_after_write = False
        self.corrupt_batch_readback = False
        self.fail_batch_after_write = False

    def read_prompt_values(self) -> Sequence[Sequence[object]]:
        return [list(row) for row in self.rows]

    def append_prompt_values(self, values: Sequence[Sequence[object]]) -> None:
        self.append_count += 1
        if self.fail_append:
            raise SheetsTransactionError("write acknowledgement is uncertain")
        self.rows.extend([list(row) for row in values])
        if self.fail_append_after_write:
            self.fail_append_after_write = False
            raise SheetsTransactionError("write acknowledgement was lost")

    def update_prompt_values(self, a1_range: str, values: Sequence[Sequence[object]]) -> None:
        self.update_count += 1
        row_number = int(a1_range.split("A")[1].split(":")[0])
        self.rows[row_number - 1] = list(values[0])

    def batch_update_prompt_cells(self, cells) -> None:
        self.batch_count += 1
        for row_number, column, value in cells:
            column_index = {"J": 9, "K": 10, "L": 11, "N": 13}[column]
            self.rows[row_number - 1][column_index] = value
        if self.corrupt_batch_readback:
            self.rows[-1][9] = "VALIDATED"
        if self.fail_batch_after_write:
            self.fail_batch_after_write = False
            raise SheetsTransactionError("batch acknowledgement was lost")


def _store(
    tmp_path: Path, transport: FakePromptTransport
) -> OptimisticGoogleSheetsPromptVersionStore:
    return OptimisticGoogleSheetsPromptVersionStore(
        transport, Path(__file__).parents[2] / "schemas", writer_lock_root=tmp_path / "runtime"
    )


def test_prompt_store_append_uses_snapshot_digest_and_exact_readback(tmp_path: Path) -> None:
    transport = FakePromptTransport()
    store = _store(tmp_path, transport)
    old = _row("SPV_000006", "APPROVED", version=6, text="old", source="c" * 64, prompt="d" * 64)
    transport.rows.append([old[name] for name in _FIELDS])
    rows = store.read_prompt_rows()
    candidate = _row(
        "SPV_000007", "DRAFT", version=7, text="fresh", source="a" * 64, prompt="b" * 64
    )
    saved = store.append_candidate(
        candidate,
        expected_snapshot_sha256=prompt_snapshot_sha256(rows),
        fresh_phase_e=_phase_e(),
    )
    assert saved == candidate
    assert transport.append_count == 1


def test_prompt_store_rejects_snapshot_drift_before_append(tmp_path: Path) -> None:
    transport = FakePromptTransport()
    store = _store(tmp_path, transport)
    old = _row("SPV_000006", "APPROVED", version=6, text="old", source="c" * 64, prompt="d" * 64)
    transport.rows.append([old[name] for name in _FIELDS])
    wrong_snapshot = prompt_snapshot_sha256([])
    with pytest.raises(SheetsTransactionError, match="changed during"):
        store.append_candidate(
            _row("SPV_000007", "DRAFT", version=7, text="fresh", source="a" * 64, prompt="b" * 64),
            expected_snapshot_sha256=wrong_snapshot,
            fresh_phase_e=_phase_e(),
        )
    assert transport.append_count == 0


def test_lost_append_acknowledgement_is_resolved_by_exact_readback(tmp_path: Path) -> None:
    transport = FakePromptTransport()
    store = _store(tmp_path, transport)
    old = _row("SPV_000006", "APPROVED", version=6, text="old", source="c" * 64, prompt="d" * 64)
    transport.rows.append([old[name] for name in _FIELDS])
    rows = store.read_prompt_rows()
    transport.fail_append_after_write = True
    candidate = _row(
        "SPV_000007", "DRAFT", version=7, text="fresh", source="a" * 64, prompt="b" * 64
    )
    assert (
        store.append_candidate(
            candidate,
            expected_snapshot_sha256=prompt_snapshot_sha256(rows),
            fresh_phase_e=_phase_e(),
        )
        == candidate
    )
    assert transport.append_count == 1


def test_prompt_store_validation_updates_only_lifecycle_cells(tmp_path: Path) -> None:
    transport = FakePromptTransport()
    candidate = _row(
        "SPV_000007", "DRAFT", version=7, text="fresh", source="a" * 64, prompt="b" * 64
    )
    transport.rows.append([candidate[name] for name in _FIELDS])
    store = _store(tmp_path, transport)
    validated = {
        **candidate,
        "Prompt_Status": "VALIDATED",
        "Validation_Message": "FRESH_PHASE_E_EXACT_MATCH",
        "Updated_At": "2026-10-18T01:00:00Z",
    }
    result = store.update_candidate(
        validated,
        expected_row_sha256=prompt_row_sha256(candidate),
        fresh_phase_e=_phase_e(),
    )
    assert result == validated
    assert transport.batch_count == 1
    assert transport.rows[1][4] == "fresh"
    assert transport.rows[1][7] == "a" * 64


def test_prompt_store_approval_batches_both_rows_and_verifies(tmp_path: Path) -> None:
    transport = FakePromptTransport()
    old = _row("SPV_000006", "APPROVED", version=6, text="old", source="c" * 64, prompt="d" * 64)
    candidate = _row(
        "SPV_000007", "VALIDATED", version=7, text="fresh", source="a" * 64, prompt="b" * 64
    )
    transport.rows.extend([[old[name] for name in _FIELDS], [candidate[name] for name in _FIELDS]])
    store = _store(tmp_path, transport)
    superseded = {**old, "Prompt_Status": "SUPERSEDED", "Updated_At": "2026-10-18T01:00:00Z"}
    approved = {
        **candidate,
        "Prompt_Status": "APPROVED",
        "Approved_At": "2026-10-18T01:00:00Z",
        "Updated_At": "2026-10-18T01:00:00Z",
    }
    result_old, result_new = store.approve_pair(
        superseded,
        approved,
        expected_previous_sha256=prompt_row_sha256(old),
        expected_candidate_sha256=prompt_row_sha256(candidate),
        fresh_phase_e=_phase_e(),
    )
    assert result_old == superseded
    assert result_new == approved
    assert transport.batch_count == 1
    assert sum(row[9] == "APPROVED" for row in transport.rows[1:]) == 1


def test_lost_approval_acknowledgement_is_resolved_without_retry(tmp_path: Path) -> None:
    transport = FakePromptTransport()
    old = _row("SPV_000006", "APPROVED", version=6, text="old", source="c" * 64, prompt="d" * 64)
    candidate = _row(
        "SPV_000007", "VALIDATED", version=7, text="fresh", source="a" * 64, prompt="b" * 64
    )
    transport.rows.extend([[old[name] for name in _FIELDS], [candidate[name] for name in _FIELDS]])
    transport.fail_batch_after_write = True
    store = _store(tmp_path, transport)
    superseded = {**old, "Prompt_Status": "SUPERSEDED", "Updated_At": "2026-10-18T01:00:00Z"}
    approved = {
        **candidate,
        "Prompt_Status": "APPROVED",
        "Approved_At": "2026-10-18T01:00:00Z",
        "Updated_At": "2026-10-18T01:00:00Z",
    }
    result = store.approve_pair(
        superseded,
        approved,
        expected_previous_sha256=prompt_row_sha256(old),
        expected_candidate_sha256=prompt_row_sha256(candidate),
        fresh_phase_e=_phase_e(),
    )
    assert result == (superseded, approved)
    assert transport.batch_count == 1


def test_prompt_store_does_not_retry_or_claim_success_when_batch_readback_is_wrong(
    tmp_path: Path,
) -> None:
    transport = FakePromptTransport()
    old = _row("SPV_000006", "APPROVED", version=6, text="old", source="c" * 64, prompt="d" * 64)
    candidate = _row(
        "SPV_000007", "VALIDATED", version=7, text="fresh", source="a" * 64, prompt="b" * 64
    )
    transport.rows.extend([[old[name] for name in _FIELDS], [candidate[name] for name in _FIELDS]])
    transport.corrupt_batch_readback = True
    store = _store(tmp_path, transport)
    superseded = {**old, "Prompt_Status": "SUPERSEDED", "Updated_At": "2026-10-18T01:00:00Z"}
    approved = {
        **candidate,
        "Prompt_Status": "APPROVED",
        "Approved_At": "2026-10-18T01:00:00Z",
        "Updated_At": "2026-10-18T01:00:00Z",
    }
    with pytest.raises(SheetsTransactionError, match="readback"):
        store.approve_pair(
            superseded,
            approved,
            expected_previous_sha256=prompt_row_sha256(old),
            expected_candidate_sha256=prompt_row_sha256(candidate),
            fresh_phase_e=_phase_e(),
        )
    assert transport.batch_count == 1


def test_google_prompt_transport_uses_one_atomic_spreadsheets_batch_update() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "sheets": [
                        {"properties": {"sheetId": 2106968153, "title": "SCENE_PROMPT_VERSION"}}
                    ]
                },
            )
        return httpx.Response(200, json={})

    class TokenProvider:
        def access_token(self) -> str:
            return "redacted-test-token"

    client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = GoogleSheetsPromptVersionApiTransport(TokenProvider(), client=client)
    transport.batch_update_prompt_cells(
        [
            (2, "J", "SUPERSEDED"),
            (2, "N", "2026-10-18T01:00:00Z"),
            (3, "J", "APPROVED"),
            (3, "K", "FRESH_PHASE_E_EXACT_MATCH"),
            (3, "L", "2026-10-18T01:00:00Z"),
            (3, "N", "2026-10-18T01:00:00Z"),
        ]
    )

    assert [request.method for request in requests] == ["GET", "POST"]
    assert requests[1].url.path.endswith(":batchUpdate")
    body = json.loads(requests[1].content)
    assert len(body["requests"]) == 6
    assert all("updateCells" in item for item in body["requests"])

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from gflow_cli.v2_runtime.phase_g import PhaseGError
from scripts.operations.collect_provider_silent_d2_evidence_v3 import (
    _HISTORICAL_MEDIA_ID,
    _HISTORICAL_WORKFLOW_ID,
    _PROJECT_ID,
    _is_project_listing_response,
    _merge_project_records,
    _ProjectListingObserver,
    _reload_project_listing,
    _require_historical_records,
    _SanitizedGenerationRecord,
)

_OTHER_WORKFLOW_ID = "a1111111-1111-4111-8111-111111111111"
_OTHER_MEDIA_ID = "b2222222-2222-4222-8222-222222222222"


def _record(
    workflow_id: str = _HISTORICAL_WORKFLOW_ID,
    media_id: str = _HISTORICAL_MEDIA_ID,
    project_id: str = _PROJECT_ID,
    status: int = 3,
) -> list[object]:
    details: list[object] = [None] * 14
    details[8] = [status]
    details[10] = "https://signed.example/poster?token=secret"
    details[13] = 1234
    media_info: list[object] = [None] * 9
    media_info[8] = "https://signed.example/video?token=secret"
    return [workflow_id, project_id, media_id, None, None, details, None, media_info]


def _body(payload: object) -> str:
    item = ["wrb.fr", "Zzl0ze", json.dumps(payload), None, None, None, "generic"]
    return ")]}'\n" + json.dumps([item, ["di", 1]])


class _Response:
    url = "https://flow.google.com/data/batchexecute?rpcids=Zzl0ze&f.sid=secret"

    def __init__(self, body: str) -> None:
        self._body = body

    async def text(self) -> str:
        return self._body


class _Page:
    url = f"https://flow.google.com/project/{_PROJECT_ID}"

    def __init__(self, bodies: list[str] | None) -> None:
        self._listeners: list[object] = []
        self._bodies = bodies
        self.reload_count = 0

    def on(self, event: str, callback: object) -> None:
        assert event == "response"
        self._listeners.append(callback)

    def remove_listener(self, event: str, callback: object) -> None:
        assert event == "response"
        self._listeners.remove(callback)

    async def reload(self, *, wait_until: str, timeout: int) -> None:
        assert wait_until == "domcontentloaded" and 0 < timeout <= 30_000
        self.reload_count += 1
        if self._bodies is None:
            return
        body = self._bodies[min(self.reload_count - 1, len(self._bodies) - 1)]
        for callback in tuple(self._listeners):
            callback(_Response(body))  # type: ignore[operator]

    async def wait_for_load_state(self, state: str, *, timeout: int) -> None:
        assert state == "domcontentloaded" and 0 < timeout <= 30_000


def test_project_listing_url_filter_requires_exact_host_endpoint_and_rpc() -> None:
    assert _is_project_listing_response("https://flow.google.com/data/batchexecute?rpcids=Zzl0ze")
    assert not _is_project_listing_response(
        "https://flow.google.com/data/batchexecute?rpcids=as29s"
    )
    assert not _is_project_listing_response("https://example.com/data/batchexecute?rpcids=Zzl0ze")
    assert not _is_project_listing_response("https://flow.google.com/project/123")


def test_three_project_reloads_require_three_independent_listing_responses() -> None:
    body = _body([[_record()]])
    page = _Page([body, body, body])

    async def collect() -> list[list[_SanitizedGenerationRecord]]:
        observer = _ProjectListingObserver(page)
        try:
            return [await _reload_project_listing(page, observer) for _ in range(3)]
        finally:
            await observer.close()

    snapshots = asyncio.run(collect())

    assert page.reload_count == 3
    assert all(len(snapshot) == 1 for snapshot in snapshots)
    assert all(snapshot == snapshots[0] for snapshot in snapshots)


def test_project_listing_enumerates_several_historical_records() -> None:
    second = _record(_OTHER_WORKFLOW_ID, _OTHER_MEDIA_ID, status=2)
    page = _Page([_body([[_record(), second]])])

    async def collect() -> list[_SanitizedGenerationRecord]:
        observer = _ProjectListingObserver(page)
        try:
            return await _reload_project_listing(page, observer)
        finally:
            await observer.close()

    records = asyncio.run(collect())

    assert {(item.workflow_id, item.media_id) for item in records} == {
        (_HISTORICAL_WORKFLOW_ID, _HISTORICAL_MEDIA_ID),
        (_OTHER_WORKFLOW_ID, _OTHER_MEDIA_ID),
    }
    _require_historical_records(
        records,
        [_HISTORICAL_WORKFLOW_ID, _OTHER_WORKFLOW_ID],
        [_HISTORICAL_MEDIA_ID, _OTHER_MEDIA_ID],
    )


def test_missing_project_listing_response_fails_closed() -> None:
    page = _Page(None)

    async def collect() -> None:
        observer = _ProjectListingObserver(page)
        try:
            await _reload_project_listing(page, observer, timeout_s=0.001)
        finally:
            await observer.close()

    with pytest.raises(PhaseGError, match="was not observed"):
        asyncio.run(collect())


def test_observed_listing_with_unrecognized_shape_fails_closed() -> None:
    page = _Page([_body([["unrelated", "UUID-bearing", _HISTORICAL_MEDIA_ID]])])

    async def collect() -> None:
        observer = _ProjectListingObserver(page)
        try:
            await _reload_project_listing(page, observer)
        finally:
            await observer.close()

    with pytest.raises(PhaseGError, match="no video record shape decoded"):
        asyncio.run(collect())


def test_project_listing_records_are_sanitized_before_retention() -> None:
    page = _Page([_body([[_record()]])])

    async def collect() -> list[_SanitizedGenerationRecord]:
        observer = _ProjectListingObserver(page)
        try:
            return await _reload_project_listing(page, observer)
        finally:
            await observer.close()

    records = asyncio.run(collect())
    serialized = json.dumps([asdict(item) for item in records])

    assert "workflow_id" in serialized
    assert "status" in serialized
    assert "signed.example" not in serialized
    assert "token=secret" not in serialized
    assert "f.sid" not in serialized


def test_listing_and_status_records_union_and_deduplicate() -> None:
    listing = [
        _SanitizedGenerationRecord(_HISTORICAL_WORKFLOW_ID, _PROJECT_ID, _HISTORICAL_MEDIA_ID, 3)
    ]
    live = [
        SimpleNamespace(
            workflow_id=_HISTORICAL_WORKFLOW_ID,
            project_id=_PROJECT_ID,
            media_id=_HISTORICAL_MEDIA_ID,
            status=3,
        ),
        SimpleNamespace(
            workflow_id=_OTHER_WORKFLOW_ID,
            project_id="c3333333-3333-4333-8333-333333333333",
            media_id=_OTHER_MEDIA_ID,
            status=3,
        ),
    ]

    records = _merge_project_records(listing, live)

    assert records == listing


@pytest.mark.parametrize(
    "record",
    [
        _SanitizedGenerationRecord(_OTHER_WORKFLOW_ID, _PROJECT_ID, _HISTORICAL_MEDIA_ID, 3),
        _SanitizedGenerationRecord(_HISTORICAL_WORKFLOW_ID, _PROJECT_ID, _OTHER_MEDIA_ID, 3),
    ],
)
def test_new_execution_or_media_identity_fails_historical_gate(
    record: _SanitizedGenerationRecord,
) -> None:
    with pytest.raises(PhaseGError, match="new structured provider output"):
        _require_historical_records([record], [_HISTORICAL_WORKFLOW_ID], [_HISTORICAL_MEDIA_ID])


def test_conflicting_status_for_same_identity_fails_closed() -> None:
    historical = _SanitizedGenerationRecord(
        _HISTORICAL_WORKFLOW_ID, _PROJECT_ID, _HISTORICAL_MEDIA_ID, 2
    )
    live = SimpleNamespace(
        workflow_id=_HISTORICAL_WORKFLOW_ID,
        project_id=_PROJECT_ID,
        media_id=_HISTORICAL_MEDIA_ID,
        status=3,
    )

    with pytest.raises(PhaseGError, match="records disagree"):
        _merge_project_records([historical], [live])


def test_new_live_status_record_is_checked_against_listing_history() -> None:
    listing = [
        _SanitizedGenerationRecord(_HISTORICAL_WORKFLOW_ID, _PROJECT_ID, _HISTORICAL_MEDIA_ID, 3)
    ]
    new_status = SimpleNamespace(
        workflow_id=_OTHER_WORKFLOW_ID,
        project_id=_PROJECT_ID,
        media_id=_OTHER_MEDIA_ID,
        status=2,
    )
    merged = _merge_project_records(listing, [new_status])

    with pytest.raises(PhaseGError, match="new structured provider output"):
        _require_historical_records(merged, [_HISTORICAL_WORKFLOW_ID], [_HISTORICAL_MEDIA_ID])

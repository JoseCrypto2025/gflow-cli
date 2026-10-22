"""Regression coverage for the canonical D2 Master snapshot contract."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from gflow_cli.v2_runtime import phase_g_run
from gflow_cli.v2_runtime.google_sheets import (
    APPROVED_SPREADSHEET_ID,
    SOURCE_LIVE_GOOGLE_SHEETS,
    GoogleSheetsSnapshot,
    snapshot_document,
)
from scripts.operations import export_d2_master_snapshot

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
PROMPT_ROWS = [{"Scene_Prompt_ID": "SPV_TEST", "Prompt_Status": "APPROVED"}]


def _document(read_at: datetime | None = None) -> dict[str, object]:
    tables: dict[str, object] = {
        "SCENE_PROMPT_VERSION": PROMPT_ROWS,
        "SCENE_MASTER": [{"Scene_ID": "SCN_TEST"}],
    }
    return dict(
        snapshot_document(
            GoogleSheetsSnapshot(
                spreadsheet_id=APPROVED_SPREADSHEET_ID,
                read_at=(read_at or NOW).isoformat(timespec="seconds").replace("+00:00", "Z"),
                source=SOURCE_LIVE_GOOGLE_SHEETS,
                tables=cast(dict[str, list], tables),
            )
        )
    )


def _load(document: dict[str, object], tmp_path: Path) -> tuple[dict[str, list], list]:
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return phase_g_run.load_phase_g_master_snapshot(path, now=NOW)


def test_canonical_snapshot_is_accepted_and_prompts_come_from_table(tmp_path: Path) -> None:
    document = _document(NOW - timedelta(minutes=15))
    document["prompt_versions"] = [{"Scene_Prompt_ID": "DECOY"}]

    tables, prompt_versions = _load(document, tmp_path)

    assert prompt_versions is tables["SCENE_PROMPT_VERSION"]
    assert prompt_versions == PROMPT_ROWS
    assert prompt_versions != document["prompt_versions"]


def test_fresh_read_at_within_fifteen_minutes_passes(tmp_path: Path) -> None:
    tables, _ = _load(_document(NOW - timedelta(minutes=14, seconds=59)), tmp_path)

    assert tables["SCENE_MASTER"] == [{"Scene_ID": "SCN_TEST"}]


def test_stale_read_at_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="freshness"):
        _load(_document(NOW - timedelta(minutes=15, seconds=1)), tmp_path)


def test_future_read_at_beyond_allowed_skew_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="freshness"):
        _load(_document(NOW + timedelta(seconds=61)), tmp_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("spreadsheet_id", "wrong", "spreadsheet"),
        ("source", "LOCAL_FIXTURE", "source"),
    ],
)
def test_wrong_snapshot_identity_fails(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    document = _document()
    document[field] = value

    with pytest.raises(ValueError, match=message):
        _load(document, tmp_path)


def test_missing_tables_fails(tmp_path: Path) -> None:
    document = _document()
    document.pop("tables")

    with pytest.raises(ValueError, match="tables"):
        _load(document, tmp_path)


def test_missing_scene_prompt_version_fails(tmp_path: Path) -> None:
    document = _document()
    tables = cast(dict[str, object], document["tables"])
    tables.pop("SCENE_PROMPT_VERSION")

    with pytest.raises(ValueError, match="SCENE_PROMPT_VERSION"):
        _load(document, tmp_path)


def test_non_list_scene_prompt_version_fails(tmp_path: Path) -> None:
    document = _document()
    tables = cast(dict[str, object], document["tables"])
    tables["SCENE_PROMPT_VERSION"] = {"Scene_Prompt_ID": "SPV_TEST"}

    with pytest.raises(ValueError, match="SCENE_PROMPT_VERSION"):
        _load(document, tmp_path)


def test_request_builder_receives_prompt_rows_from_canonical_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = _document(datetime.now(UTC))
    document["prompt_versions"] = [{"Scene_Prompt_ID": "DECOY"}]
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    captured: dict[str, object] = {}
    expected_request = object()

    def fake_build(tables: dict, prompt_versions: list, **kwargs: object) -> object:
        captured["tables"] = tables
        captured["prompt_versions"] = prompt_versions
        captured["kwargs"] = kwargs
        return expected_request

    monkeypatch.setattr(phase_g_run, "build_phase_g_request", fake_build)
    result = phase_g_run.build_phase_g_request_from_snapshot(
        path,
        d2_run_id="D2_TEST",
        runtime_state_root=tmp_path / "state",
        temp_root=tmp_path / "temp",
    )

    tables = cast(dict[str, list], captured["tables"])
    assert result is expected_request
    assert captured["prompt_versions"] is tables["SCENE_PROMPT_VERSION"]
    assert captured["prompt_versions"] != document["prompt_versions"]


def test_exporter_uses_validated_source_and_canonical_atomic_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = GoogleSheetsSnapshot(
        spreadsheet_id=APPROVED_SPREADSHEET_ID,
        read_at="2026-10-22T12:00:00Z",
        source=SOURCE_LIVE_GOOGLE_SHEETS,
        tables={"SCENE_PROMPT_VERSION": PROMPT_ROWS},
    )
    calls: dict[str, object] = {}

    class FakeCredentialProvider:
        def __init__(self, credential_path: Path, *, repository_root: Path) -> None:
            calls["credential_path"] = credential_path
            calls["repository_root"] = repository_root

    class FakeTransport:
        def __init__(self, provider: FakeCredentialProvider) -> None:
            calls["provider"] = provider

        def close(self) -> None:
            calls["transport_closed"] = True

    class FakeSnapshotSource:
        def __init__(self, transport: FakeTransport, schema_dir: Path) -> None:
            calls["transport"] = transport
            calls["schema_dir"] = schema_dir

        def read(self) -> GoogleSheetsSnapshot:
            calls["source_read"] = True
            return snapshot

    actual_writer = export_d2_master_snapshot.write_snapshot_atomic

    def writer_spy(value: GoogleSheetsSnapshot, path: Path, *, runtime_root: Path) -> None:
        calls["writer_snapshot"] = value
        calls["writer_path"] = path
        calls["writer_runtime_root"] = runtime_root
        actual_writer(value, path, runtime_root=runtime_root)

    monkeypatch.setattr(
        export_d2_master_snapshot, "GoogleWorkspaceCredentialProvider", FakeCredentialProvider
    )
    monkeypatch.setattr(export_d2_master_snapshot, "GoogleSheetsApiTransport", FakeTransport)
    monkeypatch.setattr(export_d2_master_snapshot, "GoogleSheetsSnapshotSource", FakeSnapshotSource)
    monkeypatch.setattr(export_d2_master_snapshot, "write_snapshot_atomic", writer_spy)

    result = export_d2_master_snapshot.export_d2_master_snapshot(
        tmp_path / "external-credential.json", tmp_path
    )

    output_path = Path(cast(str, result["output_path"]))
    persisted = json.loads(output_path.read_text(encoding="utf-8"))
    assert calls["source_read"] is True
    assert calls["transport_closed"] is True
    assert calls["writer_snapshot"] is snapshot
    assert calls["writer_runtime_root"] == tmp_path.resolve()
    assert output_path.is_relative_to((tmp_path / "phase-g").resolve())
    assert persisted == snapshot_document(snapshot)
    assert "prompt_versions" not in persisted
    assert result["spreadsheet_id"] == APPROVED_SPREADSHEET_ID
    assert result["source"] == SOURCE_LIVE_GOOGLE_SHEETS
    assert result["table_count"] == 1
    assert result["write_performed"] is True

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gflow_cli.v2_runtime.google_sheets_production_jobs import GoogleSheetsProductionJobStore
from gflow_cli.v2_runtime.google_sheets_transactions import (
    OptimisticGoogleSheetsJobStore,
    SheetsTransactionError,
)
from gflow_cli.v2_runtime.google_sheets_tts import GoogleSheetsTTSJobStore
from gflow_cli.v2_runtime.production_jobs import (
    JobConflictError,
    ProductionJobCoordinator,
    ProductionRequest,
)
from gflow_cli.v2_runtime.tts import TTSJobRequest


class FakeSheets:
    def __init__(self, schema_dir: Path) -> None:
        import json

        self.tables: dict[str, list[list[object]]] = {}
        for tab in ("PRODUCTION_JOBS", "SCENE_JOBS", "TTS_JOBS", "FINAL_OUTPUT"):
            schema = json.loads((schema_dir / "transaction" / f"{tab}.schema.json").read_text())
            self.tables[tab] = [[field["field_name"] for field in schema["fields"]]]
        dialogue_schema = json.loads(
            (schema_dir / "relation" / "SCENE_DIALOGUE_LINES.schema.json").read_text()
        )
        self.tables["SCENE_DIALOGUE_LINES"] = [
            [field["field_name"] for field in dialogue_schema["fields"]]
        ]
        self.fail_after_append = False
        self.fail_after_update = False
        self.skip_update_before_failure = False

    def read_values(self, tab: str, a1_range: str) -> Sequence[Sequence[object]]:
        del a1_range
        return [list(row) for row in self.tables[tab]]

    def append_values(self, tab: str, values: Sequence[Sequence[object]]) -> None:
        self.tables[tab].extend([list(row) for row in values])
        if self.fail_after_append:
            self.fail_after_append = False
            raise SheetsTransactionError("acknowledgement uncertain")

    def update_values(self, tab: str, a1_range: str, values: Sequence[Sequence[object]]) -> None:
        if self.fail_after_update and self.skip_update_before_failure:
            self.fail_after_update = False
            self.skip_update_before_failure = False
            raise SheetsTransactionError("acknowledgement uncertain")
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
    values: dict[str, object] = {field["field_name"]: "" for field in schema["fields"]}
    values.update(changes)
    return values


def _store(tmp_path: Path, transport: FakeSheets) -> OptimisticGoogleSheetsJobStore:
    return OptimisticGoogleSheetsJobStore(
        transport, _schema_dir(), writer_lock_root=tmp_path / "runtime"
    )


def _add_dialogue_line(transport: FakeSheets, text: str) -> None:
    headers = transport.tables["SCENE_DIALOGUE_LINES"][0]
    row = [""] * len(headers)
    for key, value in {
        "Dialogue_Line_ID": "SDL_000001",
        "Scene_ID": "SCN_000001",
        "Line_No": 1,
        "Speaker_Type": "NARRATOR",
        "Dialogue_Text": text,
        "Status": "ACTIVE",
    }.items():
        row[headers.index(key)] = value
    transport.tables["SCENE_DIALOGUE_LINES"].append(row)


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


def test_quarantine_preserves_other_job_fields_and_resolves_lost_ack(tmp_path: Path) -> None:
    transport = FakeSheets(_schema_dir())
    rows = _store(tmp_path, transport)
    jobs = GoogleSheetsProductionJobStore(rows)
    claim = ProductionJobCoordinator(jobs).submit_request(
        ProductionRequest("SCR_000001", "PROD", "quarantine-test", ("SPV_000006",)),
        datetime(2026, 10, 18, tzinfo=UTC),
    )
    production_id = claim.production_job.production_job_id
    scene_id = claim.scene_jobs[0].scene_job_id
    for before, after in (
        ("REQUESTED", "VALIDATING"),
        ("VALIDATING", "READY"),
        ("READY", "QUEUED"),
        ("QUEUED", "RUNNING"),
    ):
        jobs.transition_production(production_id, before, after, "2026-10-18T00:01:00Z")
    jobs.transition_scene(scene_id, "PENDING", "VALIDATING", "2026-10-18T00:01:00Z")
    jobs.transition_scene(scene_id, "VALIDATING", "RUNNING", "2026-10-18T00:02:00Z")
    production_before = jobs.read_production_job_row(production_id)
    scene_before = jobs.read_scene_job_row(scene_id)
    transport.fail_after_update = True

    production_after = jobs.quarantine_production_job(
        production_id, occurred_at="2026-10-18T00:03:00Z"
    )
    scene_after = jobs.quarantine_scene_job(scene_id, occurred_at="2026-10-18T00:03:00Z")

    assert production_after["Job_Status"] == scene_after["Job_Status"] == "QUARANTINED"
    assert {
        key: value
        for key, value in production_after.items()
        if key not in {"Job_Status", "Updated_At"}
    } == {
        key: value
        for key, value in production_before.items()
        if key not in {"Job_Status", "Updated_At"}
    }
    assert {
        key: value for key, value in scene_after.items() if key not in {"Job_Status", "Updated_At"}
    } == {
        key: value for key, value in scene_before.items() if key not in {"Job_Status", "Updated_At"}
    }


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


def test_production_store_claim_is_idempotent_and_uses_exact_schema_rows(tmp_path: Path) -> None:
    transport = FakeSheets(_schema_dir())
    rows = _store(tmp_path, transport)
    store = GoogleSheetsProductionJobStore(rows)
    request = ProductionRequest("SCR_000001", "TEST", "request-1", ("SPV_000001", "SPV_000002"))

    first = ProductionJobCoordinator(store).submit_request(request)
    replay = ProductionJobCoordinator(store).submit_request(request)

    assert first.production_job.production_job_id == "PRD_000001"
    assert [job.scene_job_id for job in first.scene_jobs] == ["SJB_000001", "SJB_000002"]
    assert replay.replayed
    assert replay.production_job == first.production_job
    assert replay.scene_jobs == first.scene_jobs
    assert len(transport.tables["PRODUCTION_JOBS"]) == 2
    assert len(transport.tables["SCENE_JOBS"]) == 3


def test_production_store_reconciles_lost_claim_ack_by_stable_key(tmp_path: Path) -> None:
    transport = FakeSheets(_schema_dir())
    rows = _store(tmp_path, transport)
    store = GoogleSheetsProductionJobStore(rows)
    transport.fail_after_append = True
    request = ProductionRequest("SCR_000001", "TEST", "request-1", ("SPV_000001",))

    claim = store.claim(request, "2026-10-18T00:00:00Z")

    assert claim.production_job.production_job_id == "PRD_000001"
    assert len(transport.tables["PRODUCTION_JOBS"]) == 2


def test_production_store_rejects_changed_request_and_compare_transition(tmp_path: Path) -> None:
    transport = FakeSheets(_schema_dir())
    store = GoogleSheetsProductionJobStore(_store(tmp_path, transport))
    claim = store.claim(
        ProductionRequest("SCR_000001", "TEST", "request-1", ("SPV_000001",)),
        "2026-10-18T00:00:00Z",
    )
    with pytest.raises(JobConflictError, match="another request"):
        store.claim(
            ProductionRequest("SCR_000001", "TEST", "request-1", ("SPV_000002",)),
            "2026-10-18T00:00:01Z",
        )

    pending = claim.scene_jobs[0]
    transport.fail_after_update = True
    validating = store.transition_scene(
        pending.scene_job_id, "PENDING", "VALIDATING", "2026-10-18T00:00:02Z"
    )
    assert validating.job_status == "VALIDATING"
    with pytest.raises(JobConflictError, match="changed before transition"):
        store.transition_scene(
            pending.scene_job_id, "PENDING", "VALIDATING", "2026-10-18T00:00:03Z"
        )
    assert len(transport.tables["SCENE_JOBS"]) == 2


def test_production_terminal_transitions_persist_required_timestamps_and_reasons(
    tmp_path: Path,
) -> None:
    transport = FakeSheets(_schema_dir())
    store = GoogleSheetsProductionJobStore(_store(tmp_path, transport))
    failed_claim = store.claim(
        ProductionRequest("SCR_000001", "TEST", "request-failed", ("SPV_000001",)),
        "2026-10-24T00:00:00Z",
    )
    failed = store.transition_production(
        failed_claim.production_job.production_job_id,
        "REQUESTED",
        "FAILED",
        "2026-10-24T00:00:01Z",
        reason_code="VALIDATION_FAILED",
    )
    assert failed.job_status == "FAILED"
    failed_row = _store(tmp_path, transport).read_rows("PRODUCTION_JOBS")[0]
    assert failed_row["Error_Code"] == "VALIDATION_FAILED"
    assert failed_row["Completed_At"] == "2026-10-24T00:00:01Z"

    cancelled_claim = store.claim(
        ProductionRequest("SCR_000002", "TEST", "request-cancelled", ("SPV_000002",)),
        "2026-10-24T00:00:02Z",
    )
    cancelled = store.transition_production(
        cancelled_claim.production_job.production_job_id,
        "REQUESTED",
        "CANCELLED",
        "2026-10-24T00:00:03Z",
    )
    assert cancelled.job_status == "CANCELLED"
    cancelled_row = _store(tmp_path, transport).read_rows("PRODUCTION_JOBS")[1]
    assert cancelled_row["Completed_At"] == "2026-10-24T00:00:03Z"


def test_production_failed_transition_requires_an_error_code(tmp_path: Path) -> None:
    store = GoogleSheetsProductionJobStore(_store(tmp_path, FakeSheets(_schema_dir())))
    claim = store.claim(
        ProductionRequest("SCR_000001", "TEST", "request-failed-no-code", ("SPV_000001",)),
        "2026-10-24T00:00:00Z",
    )

    with pytest.raises(JobConflictError, match="FAILED requires an error code"):
        store.transition_production(
            claim.production_job.production_job_id,
            "REQUESTED",
            "FAILED",
            "2026-10-24T00:00:01Z",
        )


def test_production_completion_requires_completed_scene_and_persisted_final_output(
    tmp_path: Path,
) -> None:
    transport = FakeSheets(_schema_dir())
    rows = _store(tmp_path, transport)
    store = GoogleSheetsProductionJobStore(rows)
    claim = store.claim(
        ProductionRequest("SCR_000001", "TEST", "request-complete", ("SPV_000001",)),
        "2026-10-24T00:00:00Z",
    )
    production_id = claim.production_job.production_job_id
    for expected, following in (
        ("REQUESTED", "VALIDATING"),
        ("VALIDATING", "READY"),
        ("READY", "QUEUED"),
        ("QUEUED", "RUNNING"),
    ):
        store.transition_production(production_id, expected, following, "2026-10-24T00:00:01Z")

    with pytest.raises(JobConflictError, match="persisted FINAL_OUTPUT"):
        store.transition_production(production_id, "RUNNING", "COMPLETED", "2026-10-24T00:00:02Z")

    scene = claim.scene_jobs[0]
    for expected, following in (
        ("PENDING", "VALIDATING"),
        ("VALIDATING", "QUEUED"),
        ("QUEUED", "RUNNING"),
        ("RUNNING", "UPLOADING"),
        ("UPLOADING", "REMOTE_VERIFYING"),
    ):
        store.transition_scene(scene.scene_job_id, expected, following, "2026-10-24T00:00:03Z")
    digest = "a" * 64
    store.record_remote_evidence(
        scene.scene_job_id,
        provider_execution_id="execution-1",
        provider_media_id="media-1",
        output_sha256=digest,
        output_size_bytes=1024,
        output_duration_sec=8.0,
        remote_output_uri="https://drive.google.com/file/d/file-1/view",
        remote_file_id="file-1",
        remote_sha256=digest,
        local_cleanup_status="RETAINED",
        occurred_at="2026-10-24T00:00:04Z",
    )
    store.mark_local_cleaned(scene.scene_job_id, "2026-10-24T00:00:05Z")
    store.complete_scene(scene.scene_job_id, "2026-10-24T00:00:06Z")

    final_output = {header: "" for header in rows.schema_headers("FINAL_OUTPUT")}
    final_output.update(
        {
            "Final_Output_ID": "FOUT_000001",
            "Production_Job_ID": production_id,
            "Output_Version": 1,
            "Output_Type": "FINAL_VIDEO",
            "Is_Primary": True,
            "Remote_Output_URI": "https://drive.google.com/file/d/file-1/view",
            "Remote_File_ID": "file-1",
            "Output_SHA256": digest,
            "Output_Size_Bytes": 1024,
            "Duration_Sec": 8.0,
            "Validation_Status": "PASS",
            "Output_Status": "READY",
            "Remote_Verified": True,
        }
    )
    saved_output = rows.append_if_unique(
        "FINAL_OUTPUT",
        final_output,
        unique_constraints=(("Production_Job_ID", "Output_Type", "Output_Version"),),
        identity_fields=("Output_SHA256", "Remote_File_ID", "Remote_Verified"),
    )

    completed = store.complete_production(production_id, saved_output, "2026-10-24T00:00:07Z")

    assert completed.job_status == "COMPLETED"
    assert rows.read_rows("PRODUCTION_JOBS")[0]["Completed_At"] == "2026-10-24T00:00:07Z"


def _tts_request(text: str = "Approved line.") -> TTSJobRequest:
    return TTSJobRequest(
        "PRD_000001",
        "SDL_000001",
        1,
        text,
        "VOI_000001",
        "th-TH-NiwatNeural",
        "th-TH",
        "PVA_0001",
        "MDL_TTS_0001",
        3.0,
    )


def test_tts_job_store_claim_is_idempotent_and_uses_exact_schema_row(tmp_path: Path) -> None:
    transport = FakeSheets(_schema_dir())
    _add_dialogue_line(transport, "Approved line.")
    store = GoogleSheetsTTSJobStore(_store(tmp_path, transport))

    first = store.claim(_tts_request(), "2026-10-24T00:00:00Z")
    replay = store.claim(_tts_request(), "2026-10-24T00:01:00Z")

    assert first.tts_job_id == "TTS_000001"
    assert replay == first
    assert len(transport.tables["TTS_JOBS"]) == 2
    assert len(transport.tables["TTS_JOBS"][1]) == len(transport.tables["TTS_JOBS"][0])
    saved = dict(zip(transport.tables["TTS_JOBS"][0], transport.tables["TTS_JOBS"][1], strict=True))
    assert saved["Synthesis_Config_SHA256"] == _tts_request().synthesis_config_sha256


def test_dialogue_source_table_is_read_only_in_job_transaction_adapter(tmp_path: Path) -> None:
    transport = FakeSheets(_schema_dir())
    store = _store(tmp_path, transport)
    with pytest.raises(SheetsTransactionError, match="only existing V2 job tabs"):
        store.append_if_absent(
            "SCENE_DIALOGUE_LINES",
            {header: "" for header in store.schema_headers("SCENE_DIALOGUE_LINES")},
        )


def test_tts_job_store_reconciles_lost_append_ack_by_composite_attempt_key(
    tmp_path: Path,
) -> None:
    transport = FakeSheets(_schema_dir())
    _add_dialogue_line(transport, "Approved line.")
    store = GoogleSheetsTTSJobStore(_store(tmp_path, transport))
    transport.fail_after_append = True

    claim = store.claim(_tts_request(), "2026-10-24T00:00:00Z")

    assert claim.tts_job_id == "TTS_000001"
    assert len(transport.tables["TTS_JOBS"]) == 2


def test_tts_job_store_rejects_same_attempt_with_changed_text(tmp_path: Path) -> None:
    transport = FakeSheets(_schema_dir())
    _add_dialogue_line(transport, "Approved line.")
    store = GoogleSheetsTTSJobStore(_store(tmp_path, transport))
    store.claim(_tts_request(), "2026-10-24T00:00:00Z")

    with pytest.raises(SheetsTransactionError, match="match its unique persisted dialogue line"):
        store.claim(_tts_request("Changed approved line."), "2026-10-24T00:01:00Z")


def test_tts_job_store_rejects_same_attempt_with_changed_synthesis_config(
    tmp_path: Path,
) -> None:
    transport = FakeSheets(_schema_dir())
    _add_dialogue_line(transport, "Approved line.")
    store = GoogleSheetsTTSJobStore(_store(tmp_path, transport))
    store.claim(_tts_request(), "2026-10-24T00:00:00Z")

    changed = replace(_tts_request(), voice_name="th-TH-OtherNeural")
    with pytest.raises(SheetsTransactionError, match="different input data"):
        store.claim(changed, "2026-10-24T00:01:00Z")


def test_tts_job_store_rejects_malformed_persisted_id_before_path_use(tmp_path: Path) -> None:
    transport = FakeSheets(_schema_dir())
    _add_dialogue_line(transport, "Approved line.")
    store = GoogleSheetsTTSJobStore(_store(tmp_path, transport))
    store.claim(_tts_request(), "2026-10-24T00:00:00Z")
    headers = transport.tables["TTS_JOBS"][0]
    transport.tables["TTS_JOBS"][1][headers.index("TTS_Job_ID")] = "../outside"

    with pytest.raises(SheetsTransactionError, match="TTS Job ID is invalid"):
        store.claim(_tts_request(), "2026-10-24T00:01:00Z")


def test_tts_completed_artifact_is_reused_without_a_new_provider_attempt(
    tmp_path: Path,
) -> None:
    transport = FakeSheets(_schema_dir())
    _add_dialogue_line(transport, "Approved line.")
    store = GoogleSheetsTTSJobStore(_store(tmp_path, transport))
    claim = store.claim(_tts_request(), "2026-10-24T00:00:00Z")
    for expected, next_status in (("PENDING", "VALIDATING"), ("VALIDATING", "RUNNING")):
        store.transition(
            claim.tts_job_id,
            expected_status=expected,
            next_status=next_status,
            occurred_at="2026-10-24T00:01:00Z",
        )
    store.record_provider_receipt(
        claim.tts_job_id,
        "provider-execution-1",
        "2026-10-24T00:01:30Z",
    )
    for expected, next_status in (
        ("RUNNING", "UPLOADING"),
        ("UPLOADING", "REMOTE_VERIFYING"),
    ):
        store.transition(
            claim.tts_job_id,
            expected_status=expected,
            next_status=next_status,
            occurred_at="2026-10-24T00:01:00Z",
        )
    with pytest.raises(SheetsTransactionError, match="remote evidence is incomplete"):
        store.record_remote_evidence(
            claim.tts_job_id,
            provider_execution_id="provider-execution-1",
            remote_audio_uri="https://attacker.example/audio",
            remote_file_id="drive-audio-1",
            audio_sha256="a" * 64,
            audio_size_bytes=512,
            audio_duration_ms=750,
            occurred_at="2026-10-24T00:01:45Z",
        )
    store.record_remote_evidence(
        claim.tts_job_id,
        provider_execution_id="provider-execution-1",
        remote_audio_uri="https://drive.google.com/file/d/drive-audio-1/view",
        remote_file_id="drive-audio-1",
        audio_sha256="a" * 64,
        audio_size_bytes=512,
        audio_duration_ms=750,
        occurred_at="2026-10-24T00:02:00Z",
    )
    store.mark_local_cleaned(claim.tts_job_id, "2026-10-24T00:03:00Z")
    store.complete(claim.tts_job_id, "2026-10-24T00:04:00Z")

    artifact = store.completed_artifact(claim.tts_job_id)

    assert artifact is not None
    assert artifact.provider_execution_id == "provider-execution-1"
    assert artifact.remote_file_id == "drive-audio-1"
    assert artifact.audio_size_bytes == 512
    assert artifact.audio_duration_ms == 750

    headers = transport.tables["TTS_JOBS"][0]
    transport.tables["TTS_JOBS"][1][headers.index("Remote_Audio_URI")] = (
        "https://drive.google.com/file/d/other-file/view"
    )
    with pytest.raises(SheetsTransactionError, match="failed artifact evidence"):
        store.completed_artifact(claim.tts_job_id)


def test_unresolved_scene_transition_is_persisted_as_hold(tmp_path: Path) -> None:
    transport = FakeSheets(_schema_dir())
    store = GoogleSheetsProductionJobStore(_store(tmp_path, transport))
    claim = store.claim(
        ProductionRequest("SCR_000001", "TEST", "request-1", ("SPV_000001",)),
        "2026-10-18T00:00:00Z",
    )
    transport.fail_after_update = True
    transport.skip_update_before_failure = True

    with pytest.raises(SheetsTransactionError, match="HOLD required"):
        store.transition_scene(
            claim.scene_jobs[0].scene_job_id,
            "PENDING",
            "VALIDATING",
            "2026-10-18T00:00:01Z",
        )

    held = _store(tmp_path, transport).read_rows("SCENE_JOBS")[0]
    assert held["Job_Status"] == "HOLD"
    assert held["Error_Code"] == "SHEETS_ACK_UNRESOLVED"


def test_cleanup_failure_lost_ack_is_reconciled_by_stable_scene_id(tmp_path: Path) -> None:
    transport = FakeSheets(_schema_dir())
    store = GoogleSheetsProductionJobStore(_store(tmp_path, transport))
    claim = store.claim(
        ProductionRequest("SCR_000001", "TEST", "cleanup-ack", ("SPV_000001",)),
        "2026-10-24T00:00:00Z",
    )
    scene = claim.scene_jobs[0]
    for expected, following in (
        ("PENDING", "VALIDATING"),
        ("VALIDATING", "QUEUED"),
        ("QUEUED", "RUNNING"),
        ("RUNNING", "UPLOADING"),
        ("UPLOADING", "REMOTE_VERIFYING"),
    ):
        store.transition_scene(scene.scene_job_id, expected, following, "2026-10-24T00:00:01Z")
    digest = "a" * 64
    store.record_remote_evidence(
        scene.scene_job_id,
        provider_execution_id="execution-1",
        provider_media_id="media-1",
        output_sha256=digest,
        output_size_bytes=1024,
        output_duration_sec=8.0,
        remote_output_uri="https://drive.google.com/file/d/file-1/view",
        remote_file_id="file-1",
        remote_sha256=digest,
        local_cleanup_status="RETAINED",
        occurred_at="2026-10-24T00:00:02Z",
    )

    transport.fail_after_update = True
    result = store.record_cleanup_failure(
        scene.scene_job_id, "LOCAL_DELETE_FAILED", "2026-10-24T00:00:03Z"
    )

    assert result.local_cleanup_status == "CLEANUP_FAILED"
    assert result.error_code == "LOCAL_DELETE_FAILED"

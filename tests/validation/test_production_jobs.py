from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from gflow_cli.v2_runtime.production_jobs import (
    InMemoryProductionJobStore,
    InvalidJobTransitionError,
    JobConflictError,
    ProductionJobCoordinator,
    ProductionRequest,
    validate_production_job_row,
    validate_scene_job_row,
)


def _request(key: str = "run-1", prompts: tuple[str, ...] = ("SPV_000004",)) -> ProductionRequest:
    return ProductionRequest("SCR_000001", "PROD", key, prompts)


def _move_to_remote_verifying(store: InMemoryProductionJobStore, scene_job_id: str) -> None:
    expected = "PENDING"
    for target in ("VALIDATING", "RUNNING", "UPLOADING", "REMOTE_VERIFYING"):
        store.transition_scene(scene_job_id, expected, target, "2026-10-18T00:00:00Z")
        expected = target


def test_idempotent_claim_returns_same_job_and_scene_attempt() -> None:
    store = InMemoryProductionJobStore()
    coordinator = ProductionJobCoordinator(store)
    first = coordinator.submit_request(_request())
    replay = ProductionJobCoordinator(store).submit_request(_request())

    assert first.production_job == replay.production_job
    assert first.scene_jobs == replay.scene_jobs
    assert replay.replayed
    assert len(store.production_jobs()) == 1
    assert len(store.scene_jobs()) == 1


def test_reused_key_with_changed_request_conflicts_without_writes() -> None:
    store = InMemoryProductionJobStore()
    store.claim(_request(), "2026-10-18T00:00:00Z")

    with pytest.raises(JobConflictError, match="another request"):
        store.claim(_request(prompts=("SPV_000005",)), "2026-10-18T00:00:00Z")

    assert len(store.production_jobs()) == len(store.scene_jobs()) == 1


def test_new_key_advances_per_script_run_number() -> None:
    store = InMemoryProductionJobStore()
    first = store.claim(_request("run-1"), "2026-10-18T00:00:00Z")
    second = store.claim(_request("run-2"), "2026-10-18T00:01:00Z")

    assert first.production_job.production_run_no == 1
    assert second.production_job.production_run_no == 2
    assert second.production_job.production_job_id == "PRD_000002"


def test_scene_transitions_reject_skips_and_hold_without_reason() -> None:
    store = InMemoryProductionJobStore()
    job = store.claim(_request(), "2026-10-18T00:00:00Z").scene_jobs[0]

    with pytest.raises(InvalidJobTransitionError):
        store.transition_scene(job.scene_job_id, "PENDING", "RUNNING", "2026-10-18T00:00:01Z")
    with pytest.raises(InvalidJobTransitionError, match="reason code"):
        store.transition_scene(job.scene_job_id, "PENDING", "HOLD", "2026-10-18T00:00:01Z")

    assert store.scene_jobs()[0].job_status == "PENDING"


def test_held_attempt_cannot_be_retried_automatically() -> None:
    store = InMemoryProductionJobStore()
    job = store.claim(_request(), "2026-10-18T00:00:00Z").scene_jobs[0]
    store.transition_scene(
        job.scene_job_id, "PENDING", "HOLD", "2026-10-18T00:00:01Z", "UNKNOWN_SUBMISSION_STATE"
    )

    with pytest.raises(JobConflictError, match="FAILED or CANCELLED"):
        store.append_attempt(job.production_job_id, job.scene_prompt_id, "2026-10-18T00:00:02Z")


def test_explicit_new_attempt_is_append_only_and_replay_resolves_latest() -> None:
    store = InMemoryProductionJobStore()
    claim = store.claim(_request(), "2026-10-18T00:00:00Z")
    attempt1 = claim.scene_jobs[0]
    store.transition_scene(attempt1.scene_job_id, "PENDING", "FAILED", "2026-10-18T00:00:01Z")
    attempt2 = store.append_attempt(
        claim.production_job.production_job_id, attempt1.scene_prompt_id, "2026-10-18T00:00:02Z"
    )

    replay = store.claim(_request(), "2026-10-18T00:00:03Z")

    assert attempt2.attempt_no == 2
    assert replay.scene_jobs == (attempt2,)
    assert len(store.scene_jobs()) == 2


def test_remote_verification_and_cleanup_are_required_before_completion() -> None:
    store = InMemoryProductionJobStore()
    job = store.claim(_request(), "2026-10-18T00:00:00Z").scene_jobs[0]
    _move_to_remote_verifying(store, job.scene_job_id)

    with pytest.raises(InvalidJobTransitionError, match="verified persistent output"):
        store.complete_scene(job.scene_job_id, "2026-10-18T00:00:02Z")
    with pytest.raises(ValueError, match="checksum"):
        store.record_remote_evidence(
            job.scene_job_id,
            provider_execution_id="execution-1",
            provider_media_id="media-1",
            output_sha256="a" * 64,
            output_size_bytes=10,
            output_duration_sec=8,
            remote_output_uri="https://drive.google.com/file/d/1",
            remote_file_id="file-1",
            remote_sha256="b" * 64,
            local_cleanup_status="RETAINED",
            occurred_at="2026-10-18T00:00:01Z",
        )

    store.record_remote_evidence(
        job.scene_job_id,
        provider_execution_id="execution-1",
        provider_media_id="media-1",
        output_sha256="a" * 64,
        output_size_bytes=10,
        output_duration_sec=8,
        remote_output_uri="https://drive.google.com/file/d/1",
        remote_file_id="file-1",
        remote_sha256="a" * 64,
        local_cleanup_status="RETAINED",
        occurred_at="2026-10-18T00:00:01Z",
    )
    with pytest.raises(InvalidJobTransitionError, match="verified persistent output"):
        store.complete_scene(job.scene_job_id, "2026-10-18T00:00:01Z")
    store.mark_local_cleaned(job.scene_job_id, "2026-10-18T00:00:02Z")
    complete = store.complete_scene(job.scene_job_id, "2026-10-18T00:00:03Z")

    assert complete.job_status == "COMPLETED"
    assert complete.remote_verified and complete.output_sha256 == complete.remote_sha256


def test_coordinator_requires_utc_aware_times() -> None:
    coordinator = ProductionJobCoordinator(InMemoryProductionJobStore())

    with pytest.raises(ValueError, match="timezone-aware"):
        coordinator.submit_request(_request(), datetime(2026, 10, 18))


def test_schema_row_validator_rejects_completed_without_evidence() -> None:
    with pytest.raises(ValueError, match="verified persistent output"):
        validate_scene_job_row({"Scene_Job_ID": "SJB_000001", "Job_Status": "COMPLETED"})


def test_job_serializers_match_transaction_schema_headers() -> None:
    root = Path(__file__).parents[2]
    claim = InMemoryProductionJobStore().claim(_request(), "2026-10-18T00:00:00Z")
    production_schema = json.loads(
        (root / "schemas/transaction/PRODUCTION_JOBS.schema.json").read_text(encoding="utf-8")
    )
    scene_schema = json.loads(
        (root / "schemas/transaction/SCENE_JOBS.schema.json").read_text(encoding="utf-8")
    )

    assert set(claim.production_job.as_row()) == {
        field["field_name"] for field in production_schema["fields"]
    }
    assert set(claim.scene_jobs[0].as_row()) == {
        field["field_name"] for field in scene_schema["fields"]
    }
    validate_production_job_row(claim.production_job.as_row())

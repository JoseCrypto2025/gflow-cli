"""Transaction contracts for idempotent Production and Scene Jobs.

The in-memory store is test-only. The Google Sheets implementation uses
bounded optimistic writes because Sheets values updates do not expose CAS.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlparse

from .timestamps import format_utc_timestamp


class JobConflictError(ValueError):
    """A request key, attempt key, or expected state conflicts with stored data."""


class InvalidJobTransitionError(ValueError):
    """A requested state transition is not permitted by the transaction contract."""


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_JOB_STATUSES = frozenset(
    {
        "PENDING",
        "VALIDATING",
        "QUEUED",
        "RUNNING",
        "UPLOADING",
        "REMOTE_VERIFYING",
        "COMPLETED",
        "HOLD",
        "FAILED",
        "CANCELLED",
    }
)
_PRODUCTION_STATUSES = frozenset(
    {
        "REQUESTED",
        "VALIDATING",
        "READY",
        "QUEUED",
        "RUNNING",
        "HOLD",
        "FAILED",
        "COMPLETED",
        "CANCELLED",
    }
)
_PRODUCTION_TRANSITIONS: dict[str, frozenset[str]] = {
    "REQUESTED": frozenset({"VALIDATING", "HOLD", "FAILED", "CANCELLED"}),
    "VALIDATING": frozenset({"READY", "HOLD", "FAILED", "CANCELLED"}),
    "READY": frozenset({"QUEUED", "HOLD", "FAILED", "CANCELLED"}),
    "QUEUED": frozenset({"RUNNING", "HOLD", "FAILED", "CANCELLED"}),
    "RUNNING": frozenset({"HOLD", "FAILED", "COMPLETED", "CANCELLED"}),
    "HOLD": frozenset(),
    "FAILED": frozenset(),
    "COMPLETED": frozenset(),
    "CANCELLED": frozenset(),
}
_TRANSITIONS: dict[str, frozenset[str]] = {
    "PENDING": frozenset({"VALIDATING", "HOLD", "FAILED", "CANCELLED"}),
    "VALIDATING": frozenset({"QUEUED", "RUNNING", "HOLD", "FAILED", "CANCELLED"}),
    "QUEUED": frozenset({"RUNNING", "HOLD", "FAILED", "CANCELLED"}),
    "RUNNING": frozenset({"UPLOADING", "HOLD", "FAILED", "CANCELLED"}),
    "UPLOADING": frozenset({"REMOTE_VERIFYING", "HOLD", "FAILED"}),
    "REMOTE_VERIFYING": frozenset({"COMPLETED", "HOLD", "FAILED"}),
    "COMPLETED": frozenset(),
    "HOLD": frozenset(),
    "FAILED": frozenset(),
    "CANCELLED": frozenset(),
}


def _utc(value: datetime | None) -> str:
    moment = value or datetime.now(UTC)
    return format_utc_timestamp(moment, timespec="auto")


def _canonical_digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def is_valid_scene_job_status(status: str) -> bool:
    return status in _JOB_STATUSES


def is_valid_scene_job_transition(current: str, following: str) -> bool:
    return current in _TRANSITIONS and following in _TRANSITIONS[current]


def is_valid_production_job_status(status: str) -> bool:
    return status in _PRODUCTION_STATUSES


def is_valid_production_job_transition(current: str, following: str) -> bool:
    return following in _PRODUCTION_TRANSITIONS.get(current, frozenset())


def validate_remote_evidence(
    *,
    provider_execution_id: str,
    provider_media_id: str,
    output_sha256: str,
    output_size_bytes: int,
    output_duration_sec: float,
    remote_output_uri: str,
    remote_file_id: str,
    remote_sha256: str,
    local_cleanup_status: str,
) -> None:
    if not provider_execution_id or not provider_media_id:
        raise ValueError("exact provider execution and media IDs are required")
    parsed_uri = urlparse(remote_output_uri)
    if (
        parsed_uri.scheme != "https"
        or not parsed_uri.netloc
        or parsed_uri.hostname in {"flow.google.com", "labs.google", "aisandbox-pa.googleapis.com"}
        or not remote_file_id
    ):
        raise ValueError("persistent remote URI and file ID are required")
    if not _SHA256_RE.fullmatch(output_sha256) or output_sha256.lower() != remote_sha256.lower():
        raise ValueError("remote checksum must match the validated local checksum")
    if (
        isinstance(output_size_bytes, bool)
        or output_size_bytes <= 0
        or isinstance(output_duration_sec, bool)
        or not math.isfinite(output_duration_sec)
        or output_duration_sec <= 0
    ):
        raise ValueError("validated output size and duration must be positive")
    if local_cleanup_status not in {"NOT_STARTED", "RETAINED"}:
        raise ValueError("local media must remain until remote verification is recorded")


@dataclass(frozen=True, slots=True)
class ProductionRequest:
    script_id: str
    execution_environment: str
    request_idempotency_key: str
    scene_prompt_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.script_id or not self.request_idempotency_key:
            raise ValueError("script ID and idempotency key are required")
        if self.execution_environment not in {"DEV", "TEST", "PROD"}:
            raise ValueError("execution environment is invalid")
        if (
            not self.scene_prompt_ids
            or any(not prompt_id.strip() for prompt_id in self.scene_prompt_ids)
            or len(set(self.scene_prompt_ids)) != len(self.scene_prompt_ids)
        ):
            raise ValueError("scene prompt set must be nonempty and unique")

    @property
    def request_digest(self) -> str:
        return _canonical_digest(
            {
                "script_id": self.script_id,
                "execution_environment": self.execution_environment,
                "scene_prompt_ids": list(self.scene_prompt_ids),
            }
        )

    @property
    def scene_set_sha256(self) -> str:
        return _canonical_digest({"scene_prompt_ids": list(self.scene_prompt_ids)})


@dataclass(frozen=True, slots=True)
class ProductionJob:
    production_job_id: str
    script_id: str
    production_run_no: int
    execution_environment: str
    request_idempotency_key: str
    request_digest: str
    scene_set_sha256: str
    job_status: str
    requested_at: str

    def as_row(self) -> dict[str, object]:
        """Serialize exactly the schema columns used by PRODUCTION_JOBS."""
        return {
            "Production_Job_ID": self.production_job_id,
            "Script_ID": self.script_id,
            "Production_Run_No": self.production_run_no,
            "Execution_Environment": self.execution_environment,
            "Request_Idempotency_Key": self.request_idempotency_key,
            "Queue_Priority": "",
            "Scene_Set_SHA256": self.scene_set_sha256,
            "Job_Status": self.job_status,
            "Hold_Reason_Code": "",
            "Error_Code": "",
            "Error_Message": "",
            "Requested_At": self.requested_at,
            "Started_At": "",
            "Completed_At": "",
            "Created_At": self.requested_at,
            "Updated_At": self.requested_at,
        }


@dataclass(frozen=True, slots=True)
class SceneJob:
    scene_job_id: str
    production_job_id: str
    scene_prompt_id: str
    attempt_no: int
    job_status: str
    provider_execution_id: str | None = None
    provider_media_id: str | None = None
    output_sha256: str | None = None
    output_size_bytes: int | None = None
    output_duration_sec: float | None = None
    remote_output_uri: str | None = None
    remote_file_id: str | None = None
    remote_verified: bool = False
    remote_sha256: str | None = None
    local_cleanup_status: str = "NOT_STARTED"
    updated_at: str = ""
    local_deleted_at: str | None = None
    created_at: str = ""
    remote_verified_at: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def as_row(self) -> dict[str, object]:
        """Serialize exactly the checked-in SCENE_JOBS columns."""
        return {
            "Scene_Job_ID": self.scene_job_id,
            "Production_Job_ID": self.production_job_id,
            "Scene_Prompt_ID": self.scene_prompt_id,
            "Attempt_No": self.attempt_no,
            "Job_Status": self.job_status,
            "Provider_Execution_ID": self.provider_execution_id or "",
            "Temporary_Workspace": "",
            "Remote_Output_URI": self.remote_output_uri or "",
            "Remote_File_ID": self.remote_file_id or "",
            "Output_SHA256": self.output_sha256 or "",
            "Output_Size_Bytes": self.output_size_bytes
            if self.output_size_bytes is not None
            else "",
            "Output_Duration_Sec": self.output_duration_sec
            if self.output_duration_sec is not None
            else "",
            "Remote_Verified": self.remote_verified,
            "Local_Cleanup_Status": self.local_cleanup_status,
            "Error_Code": self.error_code or "",
            "Error_Message": self.error_message or "",
            "Queued_At": "",
            "Started_At": "",
            "Generation_Completed_At": "",
            "Remote_Verified_At": self.remote_verified_at or "",
            "Local_Deleted_At": self.local_deleted_at or "",
            "Completed_At": self.updated_at if self.job_status == "COMPLETED" else "",
            "Created_At": self.created_at,
            "Updated_At": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class JobEvent:
    entity_id: str
    previous_status: str
    next_status: str
    occurred_at: str
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class JobClaim:
    production_job: ProductionJob
    scene_jobs: tuple[SceneJob, ...]
    replayed: bool


class ProductionJobStore(Protocol):
    """Atomic transaction operations required by the coordinator."""

    def claim(self, request: ProductionRequest, requested_at: str) -> JobClaim: ...

    def transition_production(
        self,
        production_job_id: str,
        expected_status: str,
        next_status: str,
        occurred_at: str,
        reason_code: str | None = None,
    ) -> ProductionJob: ...

    def complete_production(
        self,
        production_job_id: str,
        final_output: Mapping[str, object],
        occurred_at: str,
    ) -> ProductionJob: ...

    def mark_local_cleaned(self, scene_job_id: str, occurred_at: str) -> SceneJob: ...

    def record_cleanup_failure(
        self, scene_job_id: str, reason_code: str, occurred_at: str
    ) -> SceneJob: ...

    def complete_scene(self, scene_job_id: str, occurred_at: str) -> SceneJob: ...

    def transition_scene(
        self,
        scene_job_id: str,
        expected_status: str,
        next_status: str,
        occurred_at: str,
        reason_code: str | None = None,
    ) -> SceneJob: ...

    def record_remote_evidence(
        self,
        scene_job_id: str,
        *,
        provider_execution_id: str,
        provider_media_id: str,
        output_sha256: str,
        output_size_bytes: int,
        output_duration_sec: float,
        remote_output_uri: str,
        remote_file_id: str,
        remote_sha256: str,
        local_cleanup_status: str,
        occurred_at: str,
    ) -> SceneJob: ...

    def events(self) -> tuple[JobEvent, ...]: ...


class InMemoryProductionJobStore:
    """Thread-safe test fixture; not a durable or production transaction store."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._production: dict[str, ProductionJob] = {}
        self._by_key: dict[str, str] = {}
        self._scene_jobs: dict[str, SceneJob] = {}
        self._attempt_keys: dict[tuple[str, str, int], str] = {}
        self._events: list[JobEvent] = []

    def transition_production(
        self,
        production_job_id: str,
        expected_status: str,
        next_status: str,
        occurred_at: str,
        reason_code: str | None = None,
    ) -> ProductionJob:
        with self._lock:
            current = self._production.get(production_job_id)
            if current is None or current.job_status != expected_status:
                raise JobConflictError("Production Job state changed before transition")
            if next_status == "COMPLETED":
                raise InvalidJobTransitionError(
                    "Production Job completion requires persisted FINAL_OUTPUT evidence"
                )
            allowed = _PRODUCTION_TRANSITIONS[current.job_status]
            if next_status not in _PRODUCTION_STATUSES or next_status not in allowed:
                raise InvalidJobTransitionError("Production Job transition is not allowed")
            if next_status == "HOLD" and not reason_code:
                raise InvalidJobTransitionError("HOLD requires a reason code")
            if next_status == "FAILED" and not reason_code:
                raise InvalidJobTransitionError("FAILED requires an error code")
            updated = replace(current, job_status=next_status)
            self._production[production_job_id] = updated
            self._events.append(
                JobEvent(
                    production_job_id,
                    current.job_status,
                    next_status,
                    occurred_at,
                    reason_code,
                )
            )
            return updated

    def complete_production(
        self,
        production_job_id: str,
        final_output: Mapping[str, object],
        occurred_at: str,
    ) -> ProductionJob:
        with self._lock:
            current = self._production.get(production_job_id)
            if current is None or current.job_status != "RUNNING":
                raise JobConflictError("Production Job must be RUNNING before completion")
            jobs = [
                job
                for job in self._scene_jobs.values()
                if job.production_job_id == production_job_id
            ]
            if not jobs or any(job.job_status != "COMPLETED" for job in jobs):
                raise InvalidJobTransitionError("all Scene Jobs must be complete first")
            validate_final_output_evidence(production_job_id, final_output)
            updated = replace(current, job_status="COMPLETED")
            self._production[production_job_id] = updated
            self._events.append(
                JobEvent(production_job_id, current.job_status, "COMPLETED", occurred_at)
            )
            return updated

    def claim(self, request: ProductionRequest, requested_at: str) -> JobClaim:
        with self._lock:
            existing_id = self._by_key.get(request.request_idempotency_key)
            if existing_id:
                existing = self._production[existing_id]
                if existing.request_digest != request.request_digest:
                    raise JobConflictError("idempotency key is already bound to another request")
                latest_jobs: list[SceneJob] = []
                for prompt_id in request.scene_prompt_ids:
                    attempts = [
                        (attempt, job_id)
                        for (
                            production_id,
                            saved_prompt_id,
                            attempt,
                        ), job_id in self._attempt_keys.items()
                        if production_id == existing_id and saved_prompt_id == prompt_id
                    ]
                    if attempts:
                        latest_jobs.append(self._scene_jobs[max(attempts)[1]])
                jobs = tuple(latest_jobs)
                return JobClaim(existing, jobs, True)

            run_no = 1 + max(
                (
                    job.production_run_no
                    for job in self._production.values()
                    if job.script_id == request.script_id
                ),
                default=0,
            )
            production_id = f"PRD_{len(self._production) + 1:06d}"
            production = ProductionJob(
                production_id,
                request.script_id,
                run_no,
                request.execution_environment,
                request.request_idempotency_key,
                request.request_digest,
                request.scene_set_sha256,
                "REQUESTED",
                requested_at,
            )
            self._production[production_id] = production
            self._by_key[request.request_idempotency_key] = production_id
            jobs = tuple(
                self._create_scene_job(production_id, prompt_id, 1, requested_at)
                for prompt_id in request.scene_prompt_ids
            )
            return JobClaim(production, jobs, False)

    def append_attempt(
        self, production_job_id: str, scene_prompt_id: str, occurred_at: str
    ) -> SceneJob:
        """Append an explicit new attempt; never retry a nonterminal or held one."""
        with self._lock:
            attempts = [
                job
                for job in self._scene_jobs.values()
                if job.production_job_id == production_job_id
                and job.scene_prompt_id == scene_prompt_id
            ]
            if not attempts:
                raise JobConflictError("scene job attempt does not exist")
            latest = max(attempts, key=lambda job: job.attempt_no)
            if latest.job_status not in {"FAILED", "CANCELLED"}:
                raise JobConflictError("new attempt requires a prior FAILED or CANCELLED attempt")
            return self._create_scene_job(
                production_job_id, scene_prompt_id, latest.attempt_no + 1, occurred_at
            )

    def _create_scene_job(
        self, production_job_id: str, scene_prompt_id: str, attempt_no: int, occurred_at: str
    ) -> SceneJob:
        key = (production_job_id, scene_prompt_id, attempt_no)
        if key in self._attempt_keys:
            raise JobConflictError("scene attempt key already exists")
        scene_id = f"SJB_{len(self._scene_jobs) + 1:06d}"
        job = SceneJob(
            scene_id,
            production_job_id,
            scene_prompt_id,
            attempt_no,
            "PENDING",
            updated_at=occurred_at,
            created_at=occurred_at,
        )
        self._scene_jobs[scene_id] = job
        self._attempt_keys[key] = scene_id
        self._events.append(JobEvent(scene_id, "", "PENDING", occurred_at))
        return job

    def transition_scene(
        self,
        scene_job_id: str,
        expected_status: str,
        next_status: str,
        occurred_at: str,
        reason_code: str | None = None,
    ) -> SceneJob:
        with self._lock:
            job = self._scene_jobs.get(scene_job_id)
            if job is None:
                raise JobConflictError("scene job does not exist")
            if job.job_status != expected_status:
                raise JobConflictError("scene job state changed before transition")
            if next_status not in _JOB_STATUSES or next_status not in _TRANSITIONS[job.job_status]:
                raise InvalidJobTransitionError("scene job transition is not allowed")
            if next_status == "HOLD" and not reason_code:
                raise InvalidJobTransitionError("HOLD requires a reason code")
            updated = replace(job, job_status=next_status, updated_at=occurred_at)
            self._scene_jobs[scene_job_id] = updated
            self._events.append(
                JobEvent(scene_job_id, job.job_status, next_status, occurred_at, reason_code)
            )
            return updated

    def record_remote_evidence(
        self,
        scene_job_id: str,
        *,
        provider_execution_id: str,
        provider_media_id: str,
        output_sha256: str,
        output_size_bytes: int,
        output_duration_sec: float,
        remote_output_uri: str,
        remote_file_id: str,
        remote_sha256: str,
        local_cleanup_status: str,
        occurred_at: str,
    ) -> SceneJob:
        with self._lock:
            job = self._scene_jobs.get(scene_job_id)
            if job is None or job.job_status != "REMOTE_VERIFYING":
                raise InvalidJobTransitionError("remote evidence requires REMOTE_VERIFYING state")
            validate_remote_evidence(
                provider_execution_id=provider_execution_id,
                provider_media_id=provider_media_id,
                output_sha256=output_sha256,
                output_size_bytes=output_size_bytes,
                output_duration_sec=output_duration_sec,
                remote_output_uri=remote_output_uri,
                remote_file_id=remote_file_id,
                remote_sha256=remote_sha256,
                local_cleanup_status=local_cleanup_status,
            )
            updated = SceneJob(
                scene_job_id=job.scene_job_id,
                production_job_id=job.production_job_id,
                scene_prompt_id=job.scene_prompt_id,
                attempt_no=job.attempt_no,
                job_status=job.job_status,
                provider_execution_id=provider_execution_id,
                provider_media_id=provider_media_id,
                output_sha256=output_sha256.lower(),
                output_size_bytes=output_size_bytes,
                output_duration_sec=output_duration_sec,
                remote_output_uri=remote_output_uri,
                remote_file_id=remote_file_id,
                remote_verified=True,
                remote_sha256=remote_sha256.lower(),
                local_cleanup_status=local_cleanup_status,
                updated_at=occurred_at,
                created_at=job.created_at,
                remote_verified_at=occurred_at,
            )
            self._scene_jobs[scene_job_id] = updated
            self._events.append(
                JobEvent(
                    scene_job_id,
                    job.job_status,
                    job.job_status,
                    occurred_at,
                    "REMOTE_EVIDENCE_VERIFIED",
                )
            )
            return updated

    def mark_local_cleaned(self, scene_job_id: str, occurred_at: str) -> SceneJob:
        """Record cleanup only after the remote object and checksum are verified."""
        with self._lock:
            job = self._scene_jobs.get(scene_job_id)
            if (
                job is None
                or job.job_status != "REMOTE_VERIFYING"
                or not job.remote_verified
                or not job.remote_sha256
                or job.remote_sha256 != job.output_sha256
            ):
                raise InvalidJobTransitionError("local cleanup requires verified remote output")
            updated = replace(
                job,
                local_cleanup_status="CLEANED",
                local_deleted_at=occurred_at,
                error_code=None,
                error_message=None,
                updated_at=occurred_at,
            )
            self._scene_jobs[scene_job_id] = updated
            self._events.append(
                JobEvent(
                    scene_job_id,
                    job.job_status,
                    job.job_status,
                    occurred_at,
                    "LOCAL_CLEANUP_COMPLETE",
                )
            )
            return updated

    def record_cleanup_failure(
        self, scene_job_id: str, reason_code: str, occurred_at: str
    ) -> SceneJob:
        if not reason_code.strip():
            raise ValueError("cleanup failure requires a stable reason code")
        with self._lock:
            job = self._scene_jobs.get(scene_job_id)
            if (
                job is None
                or job.job_status != "REMOTE_VERIFYING"
                or not job.remote_verified
                or not job.remote_sha256
                or job.remote_sha256 != job.output_sha256
            ):
                raise InvalidJobTransitionError(
                    "cleanup failure can be recorded only after remote verification"
                )
            updated = replace(
                job,
                local_cleanup_status="CLEANUP_FAILED",
                error_code=reason_code,
                error_message=reason_code,
                updated_at=occurred_at,
            )
            self._scene_jobs[scene_job_id] = updated
            self._events.append(
                JobEvent(
                    scene_job_id,
                    job.job_status,
                    job.job_status,
                    occurred_at,
                    reason_code,
                )
            )
            return updated

    def complete_scene(self, scene_job_id: str, occurred_at: str) -> SceneJob:
        with self._lock:
            job = self._scene_jobs.get(scene_job_id)
            if job is None or job.job_status != "REMOTE_VERIFYING":
                raise InvalidJobTransitionError("completion requires REMOTE_VERIFYING state")
            if not (
                job.provider_execution_id
                and job.remote_output_uri
                and job.remote_file_id
                and job.output_sha256
                and job.remote_verified
                and job.remote_sha256 == job.output_sha256
                and job.local_cleanup_status == "CLEANED"
                and job.local_deleted_at
                and job.output_size_bytes
                and job.output_duration_sec
            ):
                raise InvalidJobTransitionError(
                    "completion requires verified persistent output and cleanup"
                )
        return self.transition_scene(scene_job_id, "REMOTE_VERIFYING", "COMPLETED", occurred_at)

    def events(self) -> tuple[JobEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def production_jobs(self) -> tuple[ProductionJob, ...]:
        with self._lock:
            return tuple(self._production.values())

    def scene_jobs(self) -> tuple[SceneJob, ...]:
        with self._lock:
            return tuple(self._scene_jobs.values())


@dataclass(slots=True)
class ProductionJobCoordinator:
    """Coordinator façade over a store that provides atomic operations."""

    store: ProductionJobStore
    _started: bool = field(default=False, init=False)

    def submit_request(self, request: ProductionRequest, now: datetime | None = None) -> JobClaim:
        """Claim once; repeated calls return the same job and attempts."""
        if self._started:
            raise JobConflictError("coordinator instances are single-request")
        self._started = True
        return self.store.claim(request, _utc(now))

    def transition_scene(
        self,
        job: SceneJob,
        next_status: str,
        *,
        reason_code: str | None = None,
        now: datetime | None = None,
    ) -> SceneJob:
        return self.store.transition_scene(
            job.scene_job_id, job.job_status, next_status, _utc(now), reason_code
        )


def validate_scene_job_row(row: Mapping[str, object]) -> None:
    """Validate the schema-level invariants required by the D7 coordinator."""
    if not re.fullmatch(r"SJB_[0-9]{6}", str(row.get("Scene_Job_ID", ""))):
        raise ValueError("Scene_Job_ID is invalid")
    status = row.get("Job_Status")
    if status not in _JOB_STATUSES:
        raise ValueError("Job_Status is invalid")
    if status == "HOLD" and not row.get("Error_Code"):
        raise ValueError("HOLD requires an error code")
    if status == "COMPLETED":
        required = (
            row.get("Provider_Execution_ID"),
            row.get("Remote_Output_URI"),
            row.get("Remote_File_ID"),
            row.get("Output_SHA256"),
            row.get("Output_Size_Bytes"),
            row.get("Output_Duration_Sec"),
            row.get("Remote_Verified") is True,
            row.get("Local_Cleanup_Status") == "CLEANED",
            row.get("Local_Deleted_At"),
        )
        if not all(required):
            raise ValueError("COMPLETED requires verified persistent output and cleanup")
        if not _SHA256_RE.fullmatch(str(row.get("Output_SHA256", ""))):
            raise ValueError("Output_SHA256 is invalid")
        if str(row.get("Remote_Output_URI", "")).startswith(("http://", "https://")) is False:
            raise ValueError("Remote_Output_URI must be a persistent URL")


def validate_production_job_row(row: Mapping[str, object]) -> None:
    """Validate schema-level Production Job identity and status requirements."""
    if not re.fullmatch(r"PRD_[0-9]{6}", str(row.get("Production_Job_ID", ""))):
        raise ValueError("Production_Job_ID is invalid")
    if row.get("Execution_Environment") not in {"DEV", "TEST", "PROD"}:
        raise ValueError("Execution_Environment is invalid")
    if row.get("Job_Status") not in {
        "REQUESTED",
        "VALIDATING",
        "READY",
        "QUEUED",
        "RUNNING",
        "HOLD",
        "FAILED",
        "COMPLETED",
        "CANCELLED",
    }:
        raise ValueError("Job_Status is invalid")
    if not row.get("Request_Idempotency_Key") or not row.get("Requested_At"):
        raise ValueError("idempotency key and Requested_At are required")
    status = row.get("Job_Status")
    if status == "HOLD" and not row.get("Hold_Reason_Code"):
        raise ValueError("HOLD requires Hold_Reason_Code")
    if status == "FAILED" and not row.get("Error_Code"):
        raise ValueError("FAILED requires Error_Code")
    if status in {"RUNNING", "COMPLETED"} and not row.get("Started_At"):
        raise ValueError("RUNNING and COMPLETED require Started_At")
    if status in {"COMPLETED", "FAILED", "CANCELLED"} and not row.get("Completed_At"):
        raise ValueError("COMPLETED, FAILED, and CANCELLED require Completed_At")


def validate_final_output_evidence(production_job_id: str, row: Mapping[str, object]) -> None:
    """Require the persisted, fully validated primary video before job completion."""
    if (
        row.get("Production_Job_ID") != production_job_id
        or row.get("Output_Type") != "FINAL_VIDEO"
        or row.get("Output_Status") != "READY"
        or row.get("Validation_Status") != "PASS"
        or row.get("Remote_Verified") is not True
        or not row.get("Final_Output_ID")
        or not row.get("Remote_File_ID")
        or not _SHA256_RE.fullmatch(str(row.get("Output_SHA256", "")))
    ):
        raise InvalidJobTransitionError(
            "Production Job completion requires a persisted, validated FINAL_VIDEO"
        )
    size = row.get("Output_Size_Bytes")
    duration = row.get("Duration_Sec")
    if (
        isinstance(size, bool)
        or not isinstance(size, (int, float, str))
        or int(size) <= 0
        or isinstance(duration, bool)
        or not isinstance(duration, (int, float, str))
        or not math.isfinite(float(duration))
        or float(duration) <= 0
    ):
        raise InvalidJobTransitionError("FINAL_VIDEO size and duration evidence are invalid")
    remote_uri = row.get("Remote_Output_URI")
    parsed_uri = urlparse(str(remote_uri or ""))
    if parsed_uri.scheme != "https" or not parsed_uri.netloc:
        raise InvalidJobTransitionError("FINAL_VIDEO must have a persistent remote URI")

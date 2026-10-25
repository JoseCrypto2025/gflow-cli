"""ProductionJobStore implementation over the existing V2 job tabs."""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import cast

from .google_sheets_transactions import OptimisticGoogleSheetsJobStore, SheetsTransactionError
from .production_jobs import (
    JobClaim,
    JobConflictError,
    JobEvent,
    ProductionJob,
    ProductionRequest,
    SceneJob,
    is_valid_production_job_status,
    is_valid_production_job_transition,
    is_valid_scene_job_status,
    is_valid_scene_job_transition,
    validate_final_output_evidence,
    validate_production_job_row,
    validate_remote_evidence,
    validate_scene_job_row,
)


def _integer_cell(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{label} must be an integer")
    return int(value)


def _float_cell(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{label} must be numeric")
    return float(value)


class GoogleSheetsProductionJobStore:
    """Bridge the coordinator contract to bounded, readback-verified Sheet rows.

    Sheet values updates are not server-side compare-and-swap. The underlying
    transaction adapter serializes this process's writers and checks readback;
    any conflicting or unresolved state therefore raises and must be held by
    the caller instead of being retried blindly.
    """

    def __init__(self, rows: OptimisticGoogleSheetsJobStore) -> None:
        self._rows = rows
        self._lock = threading.RLock()
        self._events: list[JobEvent] = []

    def read_production_job_row(self, production_job_id: str) -> dict[str, object]:
        """Read one exact row for the internal quarantine operator."""
        with self._lock:
            return dict(self._get_production_row(production_job_id))

    def read_scene_job_row(self, scene_job_id: str) -> dict[str, object]:
        """Read one exact row for the internal quarantine operator."""
        with self._lock:
            return dict(self._get_scene_row(scene_job_id))

    def quarantine_production_job(
        self, production_job_id: str, *, occurred_at: str
    ) -> dict[str, object]:
        """Mark a running row QUARANTINED without the generic HOLD fallback."""
        with self._lock:
            current = self._get_production_row(production_job_id)
            if current.get("Job_Status") == "QUARANTINED":
                return current
            if current.get("Job_Status") != "RUNNING":
                raise JobConflictError("Production Job is not in the exact expected RUNNING state")
            replacement = {**current, "Job_Status": "QUARANTINED", "Updated_At": occurred_at}
            validate_production_job_row(replacement)
            try:
                saved = self._rows.update_expected(
                    "PRODUCTION_JOBS",
                    key_value=production_job_id,
                    expected_digest=self._rows.row_digest("PRODUCTION_JOBS", current),
                    replacement=replacement,
                )
            except SheetsTransactionError as exc:
                observed = self._get_production_row(production_job_id)
                if self._rows.row_digest("PRODUCTION_JOBS", observed) == self._rows.row_digest(
                    "PRODUCTION_JOBS", replacement
                ):
                    saved = observed
                else:
                    raise SheetsTransactionError(
                        "Production Job quarantine write is unresolved; replay after fresh read"
                    ) from exc
            self._events.append(JobEvent(production_job_id, "RUNNING", "QUARANTINED", occurred_at))
            return saved

    def quarantine_scene_job(self, scene_job_id: str, *, occurred_at: str) -> dict[str, object]:
        """Mark a running row QUARANTINED without the generic HOLD fallback."""
        with self._lock:
            current = self._get_scene_row(scene_job_id)
            if current.get("Job_Status") == "QUARANTINED":
                return current
            if current.get("Job_Status") != "RUNNING":
                raise JobConflictError("Scene Job is not in the exact expected RUNNING state")
            replacement = {**current, "Job_Status": "QUARANTINED", "Updated_At": occurred_at}
            validate_scene_job_row(replacement)
            try:
                saved = self._rows.update_expected(
                    "SCENE_JOBS",
                    key_value=scene_job_id,
                    expected_digest=self._rows.row_digest("SCENE_JOBS", current),
                    replacement=replacement,
                )
            except SheetsTransactionError as exc:
                observed = self._get_scene_row(scene_job_id)
                if self._rows.row_digest("SCENE_JOBS", observed) == self._rows.row_digest(
                    "SCENE_JOBS", replacement
                ):
                    saved = observed
                else:
                    raise SheetsTransactionError(
                        "Scene Job quarantine write is unresolved; replay after fresh read"
                    ) from exc
            self._events.append(JobEvent(scene_job_id, "RUNNING", "QUARANTINED", occurred_at))
            return saved

    def claim(self, request: ProductionRequest, requested_at: str) -> JobClaim:
        with self._lock:
            productions = self._rows.read_rows("PRODUCTION_JOBS")
            matches = [
                row
                for row in productions
                if row.get("Request_Idempotency_Key") == request.request_idempotency_key
            ]
            if len(matches) > 1:
                raise SheetsTransactionError("request idempotency key is duplicated; hold required")
            if matches:
                production_row = matches[0]
                production = self._production_from_row(production_row, request)
                self._assert_request_match(production_row, request)
                replayed = True
            else:
                production_rows = [self._production_from_row(row) for row in productions]
                run_no = 1 + max(
                    (
                        job.production_run_no
                        for job in production_rows
                        if job.script_id == request.script_id
                    ),
                    default=0,
                )
                next_production_number = self._next_numeric_suffix(
                    list(productions), "Production_Job_ID", "PRD_"
                )
                production_id = f"PRD_{next_production_number:06d}"
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
                production_row = production.as_row()
                validate_production_job_row(production_row)
                self._rows.append_if_absent("PRODUCTION_JOBS", production_row)
                replayed = False

            scene_rows = self._rows.read_rows("SCENE_JOBS")
            saved = [
                self._scene_from_row(row)
                for row in scene_rows
                if row.get("Production_Job_ID") == production.production_job_id
            ]
            requested_ids = set(request.scene_prompt_ids)
            if any(job.scene_prompt_id not in requested_ids for job in saved):
                raise JobConflictError("stored Scene Jobs differ from the idempotent request")

            by_prompt: dict[str, list[SceneJob]] = {
                prompt_id: [] for prompt_id in request.scene_prompt_ids
            }
            for job in saved:
                by_prompt[job.scene_prompt_id].append(job)
            for attempts in by_prompt.values():
                attempt_numbers = [job.attempt_no for job in attempts]
                if len(attempt_numbers) != len(set(attempt_numbers)):
                    raise SheetsTransactionError(
                        "duplicate Scene Job attempt exists; hold required"
                    )

            all_scene_rows = list(scene_rows)
            next_scene_number = self._next_numeric_suffix(all_scene_rows, "Scene_Job_ID", "SJB_")
            claimed: list[SceneJob] = []
            for prompt_id in request.scene_prompt_ids:
                attempts = by_prompt[prompt_id]
                if attempts:
                    claimed.append(max(attempts, key=lambda job: job.attempt_no))
                    continue
                scene = SceneJob(
                    scene_job_id=f"SJB_{next_scene_number:06d}",
                    production_job_id=production.production_job_id,
                    scene_prompt_id=prompt_id,
                    attempt_no=1,
                    job_status="PENDING",
                    updated_at=requested_at,
                    created_at=requested_at,
                )
                row = scene.as_row()
                validate_scene_job_row(row)
                self._rows.append_if_absent("SCENE_JOBS", row)
                claimed.append(scene)
                self._events.append(JobEvent(scene.scene_job_id, "", "PENDING", requested_at))
                all_scene_rows.append(row)
                next_scene_number += 1

            return JobClaim(production, tuple(claimed), replayed)

    def transition_production(
        self,
        production_job_id: str,
        expected_status: str,
        next_status: str,
        occurred_at: str,
        reason_code: str | None = None,
    ) -> ProductionJob:
        if next_status == "COMPLETED":
            raise JobConflictError(
                "Production Job completion requires persisted FINAL_OUTPUT evidence"
            )
        if not is_valid_production_job_status(next_status):
            raise JobConflictError("Production Job status is invalid")
        if next_status == "HOLD" and not reason_code:
            raise JobConflictError("HOLD requires a reason code")
        if next_status == "FAILED" and not reason_code:
            raise JobConflictError("FAILED requires an error code")
        with self._lock:
            current_row = self._get_production_row(production_job_id)
            current = self._production_from_row(current_row)
            if current.job_status != expected_status:
                raise JobConflictError("Production Job state changed before transition")
            if not is_valid_production_job_transition(current.job_status, next_status):
                raise JobConflictError("Production Job transition is not allowed")
            replacement = {
                **current_row,
                "Job_Status": next_status,
                "Updated_At": occurred_at,
            }
            if next_status in {"VALIDATING", "RUNNING"} and not current_row.get("Started_At"):
                replacement["Started_At"] = occurred_at
            if next_status in {"FAILED", "CANCELLED"}:
                replacement["Completed_At"] = occurred_at
            replacement["Hold_Reason_Code"] = reason_code if next_status == "HOLD" else ""
            replacement["Error_Code"] = reason_code if next_status == "FAILED" else ""
            replacement["Error_Message"] = reason_code if next_status == "FAILED" else ""
            validate_production_job_row(replacement)
            saved = self._replace_production(
                current_row, replacement, production_job_id, expected_status, next_status
            )
            self._events.append(
                JobEvent(production_job_id, expected_status, next_status, occurred_at, reason_code)
            )
            return self._production_from_row(saved)

    def complete_production(
        self,
        production_job_id: str,
        final_output: Mapping[str, object],
        occurred_at: str,
    ) -> ProductionJob:
        with self._lock:
            current_row = self._get_production_row(production_job_id)
            current = self._production_from_row(current_row)
            if current.job_status != "RUNNING":
                raise JobConflictError("Production Job must be RUNNING before completion")
            jobs = [
                self._scene_from_row(row)
                for row in self._rows.read_rows("SCENE_JOBS")
                if row.get("Production_Job_ID") == production_job_id
            ]
            if not jobs or any(job.job_status != "COMPLETED" for job in jobs):
                raise JobConflictError("all Scene Jobs must be complete first")
            validate_final_output_evidence(production_job_id, final_output)
            saved_output = [
                row
                for row in self._rows.read_rows("FINAL_OUTPUT")
                if row.get("Final_Output_ID") == final_output.get("Final_Output_ID")
                and row.get("Production_Job_ID") == production_job_id
            ]
            if len(saved_output) != 1 or any(
                saved_output[0].get(field) != final_output.get(field)
                for field in (
                    "Final_Output_ID",
                    "Output_Type",
                    "Output_Status",
                    "Validation_Status",
                    "Remote_Verified",
                    "Remote_File_ID",
                    "Remote_Output_URI",
                    "Output_SHA256",
                    "Output_Size_Bytes",
                    "Duration_Sec",
                )
            ):
                raise JobConflictError("FINAL_OUTPUT readback does not match completion evidence")
            replacement = {
                **current_row,
                "Job_Status": "COMPLETED",
                "Completed_At": occurred_at,
                "Updated_At": occurred_at,
            }
            validate_production_job_row(replacement)
            saved = self._replace_production(
                current_row, replacement, production_job_id, "RUNNING", "COMPLETED"
            )
            self._events.append(JobEvent(production_job_id, "RUNNING", "COMPLETED", occurred_at))
            return self._production_from_row(saved)

    def transition_scene(
        self,
        scene_job_id: str,
        expected_status: str,
        next_status: str,
        occurred_at: str,
        reason_code: str | None = None,
    ) -> SceneJob:
        if not is_valid_scene_job_status(next_status):
            raise ValueError("scene job status is invalid")
        with self._lock:
            current_row = self._get_scene_row(scene_job_id)
            current = self._scene_from_row(current_row)
            if current.job_status != expected_status:
                raise JobConflictError("scene job state changed before transition")
            if not is_valid_scene_job_transition(current.job_status, next_status):
                raise JobConflictError("scene job transition is not allowed")
            if next_status == "HOLD" and not reason_code:
                raise JobConflictError("HOLD requires a reason code")
            updated = replace(
                current,
                job_status=next_status,
                updated_at=occurred_at,
                local_cleanup_status=(
                    "CLEANUP_FAILED"
                    if next_status == "HOLD" and current.local_cleanup_status == "CLEANUP_FAILED"
                    else current.local_cleanup_status
                ),
            )
            row = {**current_row, **updated.as_row()}
            row["Error_Code"] = reason_code or ""
            row["Error_Message"] = reason_code or ""
            if next_status == "QUEUED":
                row["Queued_At"] = occurred_at
            elif next_status == "RUNNING":
                row["Started_At"] = occurred_at
            elif next_status == "COMPLETED":
                row["Completed_At"] = occurred_at
            validate_scene_job_row(row)
            saved = self._replace_scene(
                current_row, row, scene_job_id, expected_status, next_status
            )
            self._events.append(
                JobEvent(scene_job_id, current.job_status, next_status, occurred_at, reason_code)
            )
            return self._scene_from_row(saved)

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
            current_row = self._get_scene_row(scene_job_id)
            current = self._scene_from_row(current_row)
            if current.job_status != "REMOTE_VERIFYING":
                raise JobConflictError("remote evidence requires REMOTE_VERIFYING state")
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
            updated = replace(
                current,
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
                remote_verified_at=occurred_at,
                updated_at=occurred_at,
            )
            row = {**current_row, **updated.as_row()}
            saved = self._replace_scene(
                current_row, row, scene_job_id, "REMOTE_VERIFYING", "REMOTE_VERIFYING"
            )
            self._events.append(
                JobEvent(
                    scene_job_id,
                    current.job_status,
                    current.job_status,
                    occurred_at,
                    "REMOTE_EVIDENCE_VERIFIED",
                )
            )
            return self._scene_from_row(saved)

    def mark_local_cleaned(self, scene_job_id: str, occurred_at: str) -> SceneJob:
        with self._lock:
            row = self._get_scene_row(scene_job_id)
            job = self._scene_from_row(row)
            if (
                job.job_status != "REMOTE_VERIFYING"
                or not job.remote_verified
                or not job.output_sha256
                or job.remote_sha256 != job.output_sha256
            ):
                raise JobConflictError("local cleanup requires verified remote output")
            replacement = {
                **row,
                "Local_Cleanup_Status": "CLEANED",
                "Local_Deleted_At": occurred_at,
                "Error_Code": "",
                "Error_Message": "",
                "Updated_At": occurred_at,
            }
            saved = self._replace_scene(
                row, replacement, scene_job_id, job.job_status, job.job_status
            )
            self._events.append(
                JobEvent(
                    scene_job_id,
                    job.job_status,
                    job.job_status,
                    occurred_at,
                    "LOCAL_CLEANUP_COMPLETE",
                )
            )
            return self._scene_from_row(saved)

    def record_cleanup_failure(
        self, scene_job_id: str, reason_code: str, occurred_at: str
    ) -> SceneJob:
        if not reason_code.strip():
            raise ValueError("cleanup failure requires a stable reason code")
        with self._lock:
            current = self._get_scene_row(scene_job_id)
            job = self._scene_from_row(current)
            if (
                job.job_status != "REMOTE_VERIFYING"
                or not job.remote_verified
                or not job.remote_sha256
                or job.remote_sha256 != job.output_sha256
            ):
                raise JobConflictError(
                    "cleanup failure can be recorded only after remote verification"
                )
            replacement = {
                **current,
                "Local_Cleanup_Status": "CLEANUP_FAILED",
                "Error_Code": reason_code,
                "Error_Message": reason_code,
                "Updated_At": occurred_at,
            }
            try:
                saved = self._rows.update_expected(
                    "SCENE_JOBS",
                    key_value=scene_job_id,
                    expected_digest=self._rows.row_digest("SCENE_JOBS", current),
                    replacement=replacement,
                )
            except SheetsTransactionError as exc:
                observed = self._get_scene_row(scene_job_id)
                if self._rows.row_digest("SCENE_JOBS", observed) == self._rows.row_digest(
                    "SCENE_JOBS", replacement
                ):
                    saved = observed
                else:
                    raise SheetsTransactionError(
                        "cleanup failure write acknowledgement is unresolved; retry is blocked"
                    ) from exc
            self._events.append(
                JobEvent(
                    scene_job_id,
                    job.job_status,
                    job.job_status,
                    occurred_at,
                    reason_code,
                )
            )
            return self._scene_from_row(saved)

    def complete_scene(self, scene_job_id: str, occurred_at: str) -> SceneJob:
        job = self._scene_from_row(self._get_scene_row(scene_job_id))
        if not (
            job.job_status == "REMOTE_VERIFYING"
            and job.provider_execution_id
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
            raise JobConflictError("completion requires verified persistent output and cleanup")
        return self.transition_scene(scene_job_id, "REMOTE_VERIFYING", "COMPLETED", occurred_at)

    def append_attempt(
        self, production_job_id: str, scene_prompt_id: str, occurred_at: str
    ) -> SceneJob:
        with self._lock:
            rows = self._rows.read_rows("SCENE_JOBS")
            attempts = [
                self._scene_from_row(row)
                for row in rows
                if row.get("Production_Job_ID") == production_job_id
                and row.get("Scene_Prompt_ID") == scene_prompt_id
            ]
            if not attempts:
                raise JobConflictError("scene job attempt does not exist")
            latest = max(attempts, key=lambda item: item.attempt_no)
            if latest.job_status not in {"FAILED", "CANCELLED"}:
                raise JobConflictError("new attempt requires a prior FAILED or CANCELLED attempt")
            if sum(item.attempt_no == latest.attempt_no for item in attempts) != 1:
                raise SheetsTransactionError("duplicate Scene Job attempt exists; hold required")
            scene_rows = [dict(row) for row in rows]
            number = self._next_numeric_suffix(scene_rows, "Scene_Job_ID", "SJB_")
            job = SceneJob(
                f"SJB_{number:06d}",
                production_job_id,
                scene_prompt_id,
                latest.attempt_no + 1,
                "PENDING",
                updated_at=occurred_at,
                created_at=occurred_at,
            )
            self._rows.append_if_absent("SCENE_JOBS", job.as_row())
            self._events.append(JobEvent(job.scene_job_id, "", "PENDING", occurred_at))
            return job

    def events(self) -> tuple[JobEvent, ...]:
        """Return transitions observed by this process; schemas have no durable event table."""
        return tuple(self._events)

    def _replace_scene(
        self,
        current: dict[str, object],
        replacement: dict[str, object],
        scene_job_id: str,
        expected_status: str,
        next_status: str,
    ) -> dict[str, object]:
        try:
            return self._rows.update_expected(
                "SCENE_JOBS",
                key_value=scene_job_id,
                expected_digest=self._rows.row_digest("SCENE_JOBS", current),
                replacement=replacement,
            )
        except SheetsTransactionError as exc:
            # Resolve an uncertain acknowledgement by stable key and exact row.
            try:
                observed = self._get_scene_row(scene_job_id)
            except SheetsTransactionError:
                raise SheetsTransactionError(
                    "Scene Job write outcome is unresolved; state must remain on HOLD"
                ) from exc
            if self._rows.row_digest("SCENE_JOBS", observed) == self._rows.row_digest(
                "SCENE_JOBS", replacement
            ):
                return observed
            if observed.get("Job_Status") != expected_status:
                observed_status = str(observed.get("Job_Status", ""))
                if is_valid_scene_job_transition(observed_status, "HOLD"):
                    self._write_hold(scene_job_id, observed, replacement)
                raise SheetsTransactionError(
                    "Scene Job write outcome conflicts with expected state; HOLD required"
                ) from exc
            if next_status != "HOLD":
                try:
                    self._write_hold(scene_job_id, observed, replacement)
                except SheetsTransactionError:
                    pass
            raise SheetsTransactionError(
                "Scene Job update acknowledgement is unresolved; HOLD required"
            ) from exc

    def _replace_production(
        self,
        current: dict[str, object],
        replacement: dict[str, object],
        production_job_id: str,
        expected_status: str,
        next_status: str,
    ) -> dict[str, object]:
        try:
            return self._rows.update_expected(
                "PRODUCTION_JOBS",
                key_value=production_job_id,
                expected_digest=self._rows.row_digest("PRODUCTION_JOBS", current),
                replacement=replacement,
            )
        except SheetsTransactionError as exc:
            observed = self._get_production_row(production_job_id)
            if self._rows.row_digest("PRODUCTION_JOBS", observed) == self._rows.row_digest(
                "PRODUCTION_JOBS", replacement
            ):
                return observed
            if observed.get("Job_Status") != expected_status:
                raise SheetsTransactionError(
                    "Production Job write outcome conflicts with expected state; HOLD required"
                ) from exc
            if next_status != "HOLD":
                held = {
                    **observed,
                    "Job_Status": "HOLD",
                    "Hold_Reason_Code": "SHEETS_ACK_UNRESOLVED",
                    "Updated_At": str(replacement.get("Updated_At", "")),
                }
                try:
                    self._rows.update_expected(
                        "PRODUCTION_JOBS",
                        key_value=production_job_id,
                        expected_digest=self._rows.row_digest("PRODUCTION_JOBS", observed),
                        replacement=held,
                    )
                except SheetsTransactionError:
                    pass
            raise SheetsTransactionError(
                "Production Job update acknowledgement is unresolved; HOLD required"
            ) from exc

    def _get_production_row(self, production_job_id: str) -> dict[str, object]:
        matches = [
            dict(row)
            for row in self._rows.read_rows("PRODUCTION_JOBS")
            if row.get("Production_Job_ID") == production_job_id
        ]
        if len(matches) != 1:
            raise SheetsTransactionError("Production Job is missing or duplicated; HOLD required")
        return matches[0]

    def _write_hold(
        self,
        scene_job_id: str,
        observed: dict[str, object],
        attempted: dict[str, object],
    ) -> dict[str, object]:
        held = {
            **observed,
            "Job_Status": "HOLD",
            "Error_Code": "SHEETS_ACK_UNRESOLVED",
            "Error_Message": "Sheet write acknowledgement could not be reconciled",
            "Updated_At": str(attempted.get("Updated_At", "")),
        }
        return self._rows.update_expected(
            "SCENE_JOBS",
            key_value=scene_job_id,
            expected_digest=self._rows.row_digest("SCENE_JOBS", observed),
            replacement=held,
        )

    def _get_scene_row(self, scene_job_id: str) -> dict[str, object]:
        matches = [
            row
            for row in self._rows.read_rows("SCENE_JOBS")
            if row.get("Scene_Job_ID") == scene_job_id
        ]
        if len(matches) != 1:
            raise SheetsTransactionError("Scene Job ID is missing or duplicated; HOLD required")
        return matches[0]

    @staticmethod
    def _assert_request_match(row: dict[str, object], request: ProductionRequest) -> None:
        if (
            row.get("Script_ID") != request.script_id
            or row.get("Execution_Environment") != request.execution_environment
            or row.get("Scene_Set_SHA256") != request.scene_set_sha256
        ):
            raise JobConflictError("idempotency key is already bound to another request")

    @staticmethod
    def _production_from_row(
        row: dict[str, object], request: ProductionRequest | None = None
    ) -> ProductionJob:
        job_id = str(row.get("Production_Job_ID", ""))
        if not re.fullmatch(r"PRD_[0-9]{6}", job_id):
            raise SheetsTransactionError("Production Job ID is invalid; HOLD required")
        try:
            job = ProductionJob(
                job_id,
                str(row.get("Script_ID", "")),
                _integer_cell(row.get("Production_Run_No", 0), "Production_Run_No"),
                str(row.get("Execution_Environment", "")),
                str(row.get("Request_Idempotency_Key", "")),
                request.request_digest if request else "",
                str(row.get("Scene_Set_SHA256", "")),
                str(row.get("Job_Status", "")),
                str(row.get("Requested_At", "")),
            )
            if request:
                validate_production_job_row(row)
            return job
        except (TypeError, ValueError) as exc:
            raise SheetsTransactionError("Production Job row is invalid; HOLD required") from exc

    @staticmethod
    def _scene_from_row(row: dict[str, object]) -> SceneJob:
        try:
            size = row.get("Output_Size_Bytes")
            duration = row.get("Output_Duration_Sec")
            status = str(row.get("Job_Status", ""))
            job = SceneJob(
                scene_job_id=str(row.get("Scene_Job_ID", "")),
                production_job_id=str(row.get("Production_Job_ID", "")),
                scene_prompt_id=str(row.get("Scene_Prompt_ID", "")),
                attempt_no=_integer_cell(row.get("Attempt_No", 0), "Attempt_No"),
                job_status=status,
                provider_execution_id=str(row.get("Provider_Execution_ID") or "") or None,
                provider_media_id=None,
                output_sha256=str(row.get("Output_SHA256") or "") or None,
                output_size_bytes=_integer_cell(size, "Output_Size_Bytes")
                if size not in (None, "")
                else None,
                output_duration_sec=_float_cell(duration, "Output_Duration_Sec")
                if duration not in (None, "")
                else None,
                remote_output_uri=str(row.get("Remote_Output_URI") or "") or None,
                remote_file_id=str(row.get("Remote_File_ID") or "") or None,
                remote_verified=row.get("Remote_Verified") is True,
                remote_sha256=(
                    str(row.get("Output_SHA256")) if row.get("Remote_Verified") is True else None
                ),
                local_cleanup_status=str(row.get("Local_Cleanup_Status") or "NOT_STARTED"),
                updated_at=str(row.get("Updated_At") or ""),
                local_deleted_at=str(row.get("Local_Deleted_At") or "") or None,
                created_at=str(row.get("Created_At") or ""),
                remote_verified_at=str(row.get("Remote_Verified_At") or "") or None,
                error_code=str(row.get("Error_Code") or "") or None,
                error_message=str(row.get("Error_Message") or "") or None,
            )
            validate_scene_job_row(row)
            return job
        except (TypeError, ValueError) as exc:
            raise SheetsTransactionError("Scene Job row is invalid; HOLD required") from exc

    @staticmethod
    def _next_numeric_suffix(rows: Sequence[object], key: str | None, prefix: str) -> int:
        values: list[int] = []
        for row in rows:
            raw = (
                cast(dict[str, object], row).get(key)
                if key is not None and isinstance(row, dict)
                else row
            )
            if isinstance(raw, str) and re.fullmatch(rf"{re.escape(prefix)}[0-9]{{6}}", raw):
                values.append(int(raw[len(prefix) :]))
        return max(values, default=0) + 1

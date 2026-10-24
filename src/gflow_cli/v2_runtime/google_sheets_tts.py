"""Exact-schema TTS_JOBS claims and compare-and-transition operations."""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote

from .google_sheets_transactions import OptimisticGoogleSheetsJobStore, SheetsTransactionError
from .tts import TTSArtifact, TTSJobClaim, TTSJobRequest

_TTS_TRANSITIONS: dict[str, frozenset[str]] = {
    "PENDING": frozenset({"VALIDATING", "HOLD", "FAILED", "CANCELLED"}),
    "VALIDATING": frozenset({"QUEUED", "RUNNING", "HOLD", "FAILED", "CANCELLED"}),
    "QUEUED": frozenset({"RUNNING", "HOLD", "FAILED", "CANCELLED"}),
    "RUNNING": frozenset({"UPLOADING", "REMOTE_VERIFYING", "HOLD", "FAILED"}),
    "UPLOADING": frozenset({"REMOTE_VERIFYING", "HOLD", "FAILED"}),
    "REMOTE_VERIFYING": frozenset({"COMPLETED", "HOLD", "FAILED"}),
    "COMPLETED": frozenset(),
    "HOLD": frozenset(),
    "FAILED": frozenset(),
    "CANCELLED": frozenset(),
}


@dataclass(frozen=True, slots=True)
class TTSJobRow:
    values: dict[str, object]

    @property
    def tts_job_id(self) -> str:
        return str(self.values["TTS_Job_ID"])

    @property
    def job_status(self) -> str:
        return str(self.values["Job_Status"])


class GoogleSheetsTTSJobStore:
    """Idempotent TTS transaction access using the current TTS_JOBS schema."""

    def __init__(self, rows: OptimisticGoogleSheetsJobStore) -> None:
        self._rows = rows
        self._lock = threading.RLock()

    def claim(self, request: TTSJobRequest, occurred_at: str) -> TTSJobClaim:
        with self._lock:
            dialogue_matches = [
                row
                for row in self._rows.read_rows("SCENE_DIALOGUE_LINES")
                if row.get("Dialogue_Line_ID") == request.dialogue_line_id
            ]
            if (
                len(dialogue_matches) != 1
                or dialogue_matches[0].get("Dialogue_Text") != request.text
            ):
                raise SheetsTransactionError(
                    "TTS request text must match its unique persisted dialogue line"
                )
            existing = [
                row
                for row in self._rows.read_rows("TTS_JOBS")
                if row.get("Production_Job_ID") == request.production_job_id
                and row.get("Dialogue_Line_ID") == request.dialogue_line_id
                and self._integer(row.get("Attempt_No")) == request.attempt_no
            ]
            if len(existing) > 1:
                raise SheetsTransactionError("TTS attempt key is duplicated; HOLD required")
            if existing:
                self._assert_request(existing[0], request)
                return self._claim_from_row(existing[0], request)

            all_rows = list(self._rows.read_rows("TTS_JOBS"))
            number = 1 + max((self._suffix(row.get("TTS_Job_ID")) for row in all_rows), default=0)
            values: dict[str, object] = {name: "" for name in self._rows.schema_headers("TTS_JOBS")}
            values.update(
                {
                    "TTS_Job_ID": f"TTS_{number:06d}",
                    "Production_Job_ID": request.production_job_id,
                    "Dialogue_Line_ID": request.dialogue_line_id,
                    "Attempt_No": request.attempt_no,
                    "Resolved_Voice_ID": request.voice_id,
                    "Provider_Account_ID": request.provider_account_id,
                    "Model_ID": request.model_id,
                    "Input_Text_SHA256": request.input_text_sha256,
                    "Synthesis_Config_SHA256": request.synthesis_config_sha256,
                    "Job_Status": "PENDING",
                    "Local_Cleanup_Status": "NOT_STARTED",
                    "Created_At": occurred_at,
                    "Updated_At": occurred_at,
                }
            )
            saved = self._rows.append_if_unique(
                "TTS_JOBS",
                values,
                unique_constraints=(("Production_Job_ID", "Dialogue_Line_ID", "Attempt_No"),),
                identity_fields=(
                    "Production_Job_ID",
                    "Dialogue_Line_ID",
                    "Attempt_No",
                    "Resolved_Voice_ID",
                    "Provider_Account_ID",
                    "Model_ID",
                    "Input_Text_SHA256",
                    "Synthesis_Config_SHA256",
                ),
            )
            self._assert_request(saved, request)
            return self._claim_from_row(saved, request)

    def completed_artifact(self, tts_job_id: str) -> TTSArtifact | None:
        row = self._get(tts_job_id)
        if row.get("Job_Status") != "COMPLETED":
            return None
        try:
            audio_size = self._integer(row["Audio_Size_Bytes"])
            audio_duration = self._integer(row["Audio_Duration_Ms"])
            artifact = TTSArtifact(
                self._text(row["Provider_Execution_ID"]),
                self._text(row["Remote_Audio_URI"]),
                self._text(row["Remote_File_ID"]),
                self._text(row["Audio_SHA256"]),
                audio_size,
                audio_duration,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SheetsTransactionError(
                "completed TTS row has incomplete artifact evidence"
            ) from exc
        if (
            not artifact.provider_execution_id
            or not artifact.remote_file_id
            or artifact.remote_audio_uri
            != f"https://drive.google.com/file/d/{quote(artifact.remote_file_id, safe='')}/view"
            or not re.fullmatch(r"[0-9a-f]{64}", artifact.audio_sha256)
            or artifact.audio_size_bytes <= 0
            or artifact.audio_duration_ms <= 0
            or not self._truth(row.get("Remote_Verified"))
            or row.get("Local_Cleanup_Status") != "CLEANED"
        ):
            raise SheetsTransactionError("completed TTS row failed artifact evidence validation")
        return artifact

    def validate_completed_claim(self, claim: TTSJobClaim) -> TTSArtifact:
        """Re-read the exact persisted attempt and its current dialogue source."""
        row = self._get(claim.tts_job_id)
        persisted = self._claim_from_row(row, claim.request)
        if persisted.job_status != "COMPLETED" or claim.job_status != "COMPLETED":
            raise SheetsTransactionError("TTS merge requires a persisted COMPLETED job")
        dialogue_matches = [
            item
            for item in self._rows.read_rows("SCENE_DIALOGUE_LINES")
            if item.get("Dialogue_Line_ID") == claim.request.dialogue_line_id
        ]
        if (
            len(dialogue_matches) != 1
            or dialogue_matches[0].get("Dialogue_Text") != claim.request.text
        ):
            raise SheetsTransactionError(
                "TTS merge dialogue source is missing, duplicated, or changed"
            )
        artifact = self.completed_artifact(claim.tts_job_id)
        if artifact is None:
            raise SheetsTransactionError("TTS merge job has no durable completed artifact")
        return artifact

    def transition(
        self,
        tts_job_id: str,
        *,
        expected_status: str,
        next_status: str,
        occurred_at: str,
        error_code: str | None = None,
    ) -> TTSJobRow:
        if next_status not in _TTS_TRANSITIONS.get(expected_status, frozenset()):
            raise SheetsTransactionError("TTS Job transition is not allowed")
        if next_status == "HOLD" and not error_code:
            raise SheetsTransactionError("TTS Job HOLD requires a reason code")
        current = self._get(tts_job_id)
        if current.get("Job_Status") != expected_status:
            raise SheetsTransactionError("TTS Job state changed before transition")
        replacement = {
            **current,
            "Job_Status": next_status,
            "Updated_At": occurred_at,
            "Error_Code": error_code or "",
            "Error_Message": error_code or "",
        }
        if next_status == "RUNNING":
            replacement["Started_At"] = occurred_at
        if next_status == "COMPLETED":
            replacement["Completed_At"] = occurred_at
        saved = self._rows.update_expected(
            "TTS_JOBS",
            key_value=tts_job_id,
            expected_digest=self._rows.row_digest("TTS_JOBS", current),
            replacement=replacement,
        )
        return TTSJobRow(saved)

    def record_remote_evidence(
        self,
        tts_job_id: str,
        *,
        provider_execution_id: str,
        remote_audio_uri: str,
        remote_file_id: str,
        audio_sha256: str,
        audio_size_bytes: int,
        audio_duration_ms: int,
        occurred_at: str,
    ) -> TTSJobRow:
        current = self._get(tts_job_id)
        if current.get("Job_Status") != "REMOTE_VERIFYING":
            raise SheetsTransactionError("TTS remote evidence requires REMOTE_VERIFYING")
        if (
            not provider_execution_id
            or not remote_file_id
            or remote_audio_uri
            != f"https://drive.google.com/file/d/{quote(remote_file_id, safe='')}/view"
            or not re.fullmatch(r"[0-9a-f]{64}", audio_sha256)
            or audio_size_bytes <= 0
            or audio_duration_ms <= 0
        ):
            raise SheetsTransactionError("TTS remote evidence is incomplete")
        replacement = {
            **current,
            "Provider_Execution_ID": provider_execution_id,
            "Remote_Audio_URI": remote_audio_uri,
            "Remote_File_ID": remote_file_id,
            "Audio_SHA256": audio_sha256,
            "Audio_Size_Bytes": audio_size_bytes,
            "Audio_Duration_Ms": audio_duration_ms,
            "Remote_Verified": True,
            "Remote_Verified_At": occurred_at,
            "Local_Cleanup_Status": "RETAINED",
            "Updated_At": occurred_at,
        }
        saved = self._rows.update_expected(
            "TTS_JOBS",
            key_value=tts_job_id,
            expected_digest=self._rows.row_digest("TTS_JOBS", current),
            replacement=replacement,
        )
        return TTSJobRow(saved)

    def record_provider_receipt(
        self,
        tts_job_id: str,
        provider_execution_id: str,
        occurred_at: str,
    ) -> TTSJobRow:
        current = self._get(tts_job_id)
        if current.get("Job_Status") != "RUNNING" or not provider_execution_id.strip():
            raise SheetsTransactionError("provider receipt requires a running TTS attempt")
        existing_id = current.get("Provider_Execution_ID")
        if existing_id and existing_id != provider_execution_id:
            raise SheetsTransactionError("provider receipt conflicts with the recorded execution")
        replacement = {
            **current,
            "Provider_Execution_ID": provider_execution_id,
            "Updated_At": occurred_at,
        }
        saved = self._rows.update_expected(
            "TTS_JOBS",
            key_value=tts_job_id,
            expected_digest=self._rows.row_digest("TTS_JOBS", current),
            replacement=replacement,
        )
        return TTSJobRow(saved)

    def mark_local_cleaned(self, tts_job_id: str, occurred_at: str) -> TTSJobRow:
        current = self._get(tts_job_id)
        if (
            current.get("Job_Status") != "REMOTE_VERIFYING"
            or current.get("Remote_Verified") is not True
        ):
            raise SheetsTransactionError("TTS cleanup requires verified remote audio")
        replacement = {
            **current,
            "Local_Cleanup_Status": "CLEANED",
            "Local_Deleted_At": occurred_at,
            "Error_Code": "",
            "Error_Message": "",
            "Updated_At": occurred_at,
        }
        saved = self._rows.update_expected(
            "TTS_JOBS",
            key_value=tts_job_id,
            expected_digest=self._rows.row_digest("TTS_JOBS", current),
            replacement=replacement,
        )
        return TTSJobRow(saved)

    def cleanup_state(self, tts_job_id: str) -> dict[str, object]:
        """Read the exact persisted cleanup state for a recovery decision."""
        return dict(self._get(tts_job_id))

    def record_cleanup_failure(
        self, tts_job_id: str, reason_code: str, occurred_at: str
    ) -> TTSJobRow:
        if not reason_code.strip():
            raise ValueError("cleanup failure requires a stable reason code")
        current = self._get(tts_job_id)
        if (
            current.get("Job_Status") != "REMOTE_VERIFYING"
            or self._truth(current.get("Remote_Verified")) is not True
            or not current.get("Remote_File_ID")
            or not re.fullmatch(r"[0-9a-f]{64}", str(current.get("Audio_SHA256", "")))
        ):
            raise SheetsTransactionError("cleanup failure requires verified persisted TTS audio")
        if current.get("Local_Cleanup_Status") == "CLEANED":
            return TTSJobRow(dict(current))
        replacement = {
            **current,
            "Local_Cleanup_Status": "CLEANUP_FAILED",
            "Error_Code": reason_code,
            "Error_Message": reason_code,
            "Updated_At": occurred_at,
        }
        try:
            saved = self._rows.update_expected(
                "TTS_JOBS",
                key_value=tts_job_id,
                expected_digest=self._rows.row_digest("TTS_JOBS", current),
                replacement=replacement,
            )
        except SheetsTransactionError as exc:
            observed = self._get(tts_job_id)
            if self._rows.row_digest("TTS_JOBS", observed) == self._rows.row_digest(
                "TTS_JOBS", replacement
            ):
                saved = observed
            else:
                raise SheetsTransactionError(
                    "TTS cleanup failure acknowledgement is unresolved"
                ) from exc
        return TTSJobRow(saved)

    def complete(self, tts_job_id: str, occurred_at: str) -> TTSJobRow:
        current = self._get(tts_job_id)
        required = (
            current.get("Provider_Execution_ID"),
            current.get("Remote_Audio_URI"),
            current.get("Remote_File_ID"),
            current.get("Audio_SHA256"),
            current.get("Audio_Size_Bytes"),
            current.get("Audio_Duration_Ms"),
            current.get("Remote_Verified") is True,
            current.get("Local_Cleanup_Status") == "CLEANED",
            current.get("Local_Deleted_At"),
        )
        if current.get("Job_Status") != "REMOTE_VERIFYING" or not all(required):
            raise SheetsTransactionError(
                "TTS completion requires verified remote audio and cleanup"
            )
        return self.transition(
            tts_job_id,
            expected_status="REMOTE_VERIFYING",
            next_status="COMPLETED",
            occurred_at=occurred_at,
        )

    def _get(self, tts_job_id: str) -> dict[str, object]:
        matches = [
            row for row in self._rows.read_rows("TTS_JOBS") if row.get("TTS_Job_ID") == tts_job_id
        ]
        if len(matches) != 1:
            raise SheetsTransactionError("TTS Job ID is missing or duplicated; HOLD required")
        return matches[0]

    @staticmethod
    def _assert_request(row: Mapping[str, object], request: TTSJobRequest) -> None:
        expected = {
            "Production_Job_ID": request.production_job_id,
            "Dialogue_Line_ID": request.dialogue_line_id,
            "Attempt_No": request.attempt_no,
            "Resolved_Voice_ID": request.voice_id,
            "Provider_Account_ID": request.provider_account_id,
            "Model_ID": request.model_id,
            "Input_Text_SHA256": request.input_text_sha256,
            "Synthesis_Config_SHA256": request.synthesis_config_sha256,
        }
        if any(row.get(name) != value for name, value in expected.items()):
            raise SheetsTransactionError("TTS attempt key is bound to different input data")

    @classmethod
    def _claim_from_row(cls, row: dict[str, object], request: TTSJobRequest) -> TTSJobClaim:
        cls._assert_request(row, request)
        tts_job_id = row.get("TTS_Job_ID")
        status = row.get("Job_Status")
        if not isinstance(tts_job_id, str) or not re.fullmatch(r"TTS_[0-9]{6}", tts_job_id):
            raise SheetsTransactionError("persisted TTS Job ID is invalid; HOLD required")
        if not isinstance(status, str) or status not in _TTS_TRANSITIONS:
            raise SheetsTransactionError("persisted TTS Job status is invalid; HOLD required")
        return TTSJobClaim(tts_job_id, request, status)

    @staticmethod
    def _integer(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            return 0
        try:
            return int(value)
        except ValueError:
            return 0

    @staticmethod
    def _text(value: object) -> str:
        return value if isinstance(value, str) else ""

    @staticmethod
    def _truth(value: object) -> bool:
        return value is True or (isinstance(value, str) and value.strip().upper() == "TRUE")

    @staticmethod
    def _suffix(value: object) -> int:
        match = re.fullmatch(r"TTS_([0-9]{6})", str(value or ""))
        return int(match.group(1)) if match else 0

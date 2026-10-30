"""Redacted, append-only Phase G execution-stage diagnostics."""

from __future__ import annotations

import hashlib
import json
import os
import re
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from gflow_cli.redaction import redact_sensitive_text

PhaseGStage = Literal[
    "REQUEST_BINDING",
    "CONSUMED_AUTHORIZATION_VALIDATION",
    "PROFILE_LEASE",
    "FLOW_OPEN",
    "ACCOUNT_VERIFY",
    "PROJECT_VERIFY",
    "SETTINGS_SNAPSHOT",
    "SETTINGS_APPLY",
    "MODEL_APPLY",
    "MODEL_READBACK",
    "ASPECT_RATIO_APPLY",
    "ASPECT_RATIO_READBACK",
    "OUTPUT_COUNT_APPLY",
    "OUTPUT_COUNT_READBACK",
    "PROMPT_PREPARE",
    "JOURNAL_PREFLIGHT",
    "JOURNAL_BEGIN",
    "CREDIT_GUARD_PREPARE",
    "RESERVATION_CREATE",
    "SUBMISSION_BASELINE",
    "GENERATE_CHECKPOINT",
    "GENERATE_CLICK",
    "POST_CLICK_EXECUTION",
    "SAME_ATTEMPT_RECONCILIATION",
]
PhaseGFailureReason = Literal[
    "PHASE_G_EXECUTION_FAILED_BEFORE_CLICK",
    "PHASE_G_EXECUTION_INTERRUPTED_AFTER_CLICK",
    "PHASE_G_POST_CLICK_RECONCILIATION_FAILED",
]

_ATTEMPT_RE = re.compile(r"^D2_[A-Za-z0-9_-]{8,64}$")
_AUTHORIZATION_RE = re.compile(r"^LRA_[0-9A-F]{24}$")
_JOB_RE = re.compile(r"^(?:PRD|SJB)_[A-Za-z0-9_-]{6,64}$")
_EXCEPTION_CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|"
    r"password|authorization|cookie|set-cookie|x-goog-api-key)\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_TRACEBACK_LIMIT = 500


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _safe_message(message: str, sensitive_values: tuple[str, ...]) -> str:
    lowered = message.casefold()
    for value in sensitive_values:
        if not value:
            continue
        folded = value.casefold()
        if folded in lowered:
            return "<redacted:prompt-containing-exception>"
        if len(folded) >= 24 and any(
            folded[index : index + 24] in lowered for index in range(0, len(folded) - 23, 12)
        ):
            return "<redacted:prompt-containing-exception>"
    cleaned = _CREDENTIAL_ASSIGNMENT_RE.sub("<redacted:credential>", message)
    return redact_sensitive_text(cleaned)[:_TRACEBACK_LIMIT]


def _exception_location(exc: Exception, repository_root: Path) -> tuple[str | None, int | None]:
    try:
        root = repository_root.resolve(strict=True)
    except OSError:
        return None, None
    frames = traceback.extract_tb(exc.__traceback__)
    for frame in reversed(frames):
        path = Path(frame.filename)
        try:
            relative = path.resolve(strict=False).relative_to(root)
        except (OSError, ValueError):
            continue
        if relative.parts and relative.parts[0] in {"src", "scripts", "tests"}:
            return relative.as_posix(), frame.lineno
    return None, None


@dataclass(slots=True)
class PhaseGDiagnosticRecorder:
    """Append one redacted event per stage/failure outside attempt and credit ledgers."""

    runtime_root: Path
    attempt_id: str
    authorization_id: str
    production_job_id: str
    scene_job_id: str

    def __post_init__(self) -> None:
        if (
            not self.runtime_root.is_absolute()
            or self.runtime_root == Path(self.runtime_root.anchor)
            or not _ATTEMPT_RE.fullmatch(self.attempt_id)
            or not _AUTHORIZATION_RE.fullmatch(self.authorization_id)
            or not _JOB_RE.fullmatch(self.production_job_id)
            or not _JOB_RE.fullmatch(self.scene_job_id)
        ):
            raise ValueError("Phase G diagnostic identity or runtime root is invalid")

    @property
    def path(self) -> Path:
        return (
            self.runtime_root.resolve(strict=False)
            / "production"
            / "phase-g"
            / "execution-diagnostics"
            / f"{self.attempt_id}.jsonl"
        )

    def record_stage(
        self,
        event_type: Literal["STAGE_STARTED", "STAGE_COMPLETED"],
        stage: PhaseGStage,
        *,
        last_completed_stage: str | None,
    ) -> None:
        self._append(
            {
                "schema_version": 1,
                "event_type": event_type,
                "timestamp": _timestamp(),
                "attempt_id": self.attempt_id,
                "authorization_id": self.authorization_id,
                "production_job_id": self.production_job_id,
                "scene_job_id": self.scene_job_id,
                "stage": stage,
                "last_completed_stage": last_completed_stage,
            }
        )

    def record_failure(
        self,
        exc: Exception,
        progress: PhaseGExecutionProgress,
        *,
        repository_root: Path,
        sensitive_values: tuple[str, ...] = (),
        reason_code: PhaseGFailureReason = "PHASE_G_EXECUTION_FAILED_BEFORE_CLICK",
        failed_stage: PhaseGStage | None = None,
    ) -> dict[str, object]:
        raw_traceback = "".join(traceback.format_exception(exc))
        source_file, source_line = _exception_location(exc, repository_root)
        exception_type = type(exc).__name__
        if not _EXCEPTION_CLASS_RE.fullmatch(exception_type):
            exception_type = "Exception"
        record: dict[str, object] = {
            "schema_version": 1,
            "event_type": "FAILURE",
            "timestamp": _timestamp(),
            "attempt_id": self.attempt_id,
            "authorization_id": self.authorization_id,
            "production_job_id": self.production_job_id,
            "scene_job_id": self.scene_job_id,
            "reason_code": reason_code,
            "exception_type": exception_type,
            "exception_message": _safe_message(str(exc), sensitive_values),
            "failed_stage": failed_stage or progress.failed_stage,
            "last_completed_stage": progress.last_completed_stage,
            "source_file": source_file,
            "source_line": source_line,
            "traceback_digest": hashlib.sha256(raw_traceback.encode("utf-8")).hexdigest(),
        }
        self._append(record)
        return record

    def record_diagnostic_complete(self, progress: PhaseGExecutionProgress) -> None:
        self._append(
            {
                "schema_version": 1,
                "event_type": "DIAGNOSTIC_COMPLETE",
                "timestamp": _timestamp(),
                "attempt_id": self.attempt_id,
                "authorization_id": self.authorization_id,
                "production_job_id": self.production_job_id,
                "scene_job_id": self.scene_job_id,
                "last_completed_stage": progress.last_completed_stage,
                "stopped_before_stage": "JOURNAL_BEGIN",
                "generate_action_count": 0,
                "reservation_created": False,
                "provider_submit_request_count": 0,
            }
        )

    def _append(self, record: dict[str, object]) -> None:
        root = self.runtime_root.resolve(strict=False)
        if root == Path(root.anchor):
            raise OSError("Phase G diagnostic path escaped runtime state root")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink() or not root.is_dir():
            raise OSError("Phase G runtime state root is not a directory")
        directory = root
        for component in ("production", "phase-g", "execution-diagnostics"):
            directory = directory / component
            if not directory.exists():
                directory.mkdir(mode=0o700)
            if directory.is_symlink() or not directory.is_dir():
                raise OSError("Phase G diagnostic path cannot contain a symlink")
            if not directory.resolve(strict=True).is_relative_to(root):
                raise OSError("Phase G diagnostic path escaped runtime state root")
        if self.path.is_symlink():
            raise OSError("Phase G diagnostic path cannot be a symlink")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        flags |= cast(int, getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(self.path, flags, 0o600)
        try:
            payload = (
                json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
                    "utf-8"
                )
                + b"\n"
            )
            with os.fdopen(descriptor, "ab", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(descriptor)
        finally:
            os.close(descriptor)


@dataclass(slots=True)
class PhaseGExecutionProgress:
    """Track the current pre-click stage and optionally persist each transition."""

    recorder: PhaseGDiagnosticRecorder | None = None
    strict: bool = False
    current_stage: str | None = None
    last_completed_stage: str | None = None
    failed_stage: str | None = None
    persistence_error: str | None = None

    def start(self, stage: PhaseGStage) -> None:
        self.current_stage = stage
        self.failed_stage = stage
        self._record("STAGE_STARTED", stage)

    def complete(self, stage: PhaseGStage) -> None:
        self.last_completed_stage = stage
        self.current_stage = None
        self.failed_stage = None
        self._record("STAGE_COMPLETED", stage)

    def _record(
        self, event_type: Literal["STAGE_STARTED", "STAGE_COMPLETED"], stage: PhaseGStage
    ) -> None:
        if self.recorder is None:
            return
        try:
            self.recorder.record_stage(
                event_type, stage, last_completed_stage=self.last_completed_stage
            )
        except Exception as exc:
            self.persistence_error = type(exc).__name__
            if self.strict:
                raise

"""Offline-testable adapter for the pinned upstream ``gflow video t2v`` CLI.

This adapter deliberately does not implement ``SceneGenerationPort``. The
upstream CLI result does not carry the account, project, execution, or D2
identity evidence required by production orchestration.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import UUID

from gflow_cli.redaction import redact_sensitive_diagnostic

UPSTREAM_VERSION = "0.92.1"
UPSTREAM_COMMIT = "63dd0081e117dc081ed7fc774d3dd19ac91ec9aa"
RESULT_SCHEMA_VERSION = "UPSTREAM_GFLOW_ADAPTER_RESULT_V1"
DEFAULT_TIMEOUT_SECONDS = 900.0
_MAX_PROMPT_CHARS = 65_536
_MAX_CAPTURE_BYTES = 1_048_576
_MAX_DIAGNOSTIC_CHARS = 256
_MAX_IDEMPOTENCY_KEYS = 10_000
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_OPAQUE_SECRET_RE = re.compile(
    r"(?i)(?:\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token)\s*[=:]\s*"
    r"[\"']?[^\s,;\"']+|(?<![A-Za-z0-9])[A-Za-z0-9_~+/-]{40,}={0,2}|"
    r"\b[A-Z][A-Z0-9]+(?:_[A-Z0-9]+){2,}\b)"
)
_MODEL_MAP = {
    "omni-flash": "omni_flash",
    "veo-lite": "veo_3_1_lite",
    "veo-fast": "veo_3_1_fast",
    "veo-quality": "veo_3_1_quality",
    "veo-lite-lp": "veo_3_1_lite_lower_priority",
}
_ASPECT_MAP = {"9:16": "portrait", "16:9": "landscape"}
_AUTH_ERRORS = {
    "AuthExpiredError",
    "AuthMissingError",
    "FlowAccountChooserError",
    "FlowAccessUnavailableError",
}
_PROJECT_ERRORS = {"UiSelectorDriftError"}
_CREDIT_ERRORS = {"InsufficientCreditsError"}
_DOWNLOAD_ERRORS = {"MediaDownloadError"}
_UNSUPPORTED_ERRORS = {"FlowHostMigratedError", "FlowAccessUnavailableError"}

# The CLI's positional prompt is passed to this fixed launcher over stdin, not
# in the operating-system command line. The process args contain no prompt.
_CLI_LAUNCHER = (
    "import json,sys; "
    "payload=json.load(sys.stdin); "
    "from gflow_cli.cli import main; "
    "main(args=payload['args'],prog_name='gflow',standalone_mode=True)"
)


class AdapterStatus(StrEnum):
    UPSTREAM_GENERATION_COMPLETED = "UPSTREAM_GENERATION_COMPLETED"
    PRE_SUBMIT_FAILURE = "PRE_SUBMIT_FAILURE"
    AUTH_FAILURE = "AUTH_FAILURE"
    PROJECT_NAVIGATION_FAILURE = "PROJECT_NAVIGATION_FAILURE"
    UNSUPPORTED_FLOW_STATE = "UNSUPPORTED_FLOW_STATE"
    INSUFFICIENT_CREDITS = "INSUFFICIENT_CREDITS"
    POST_SUBMIT_UNKNOWN = "POST_SUBMIT_UNKNOWN"
    DOWNLOAD_FAILURE_AFTER_GENERATION = "DOWNLOAD_FAILURE_AFTER_GENERATION"


@dataclass(frozen=True, slots=True)
class AdapterRequest:
    """Validated production inputs; authorization and durable identity stay with the caller."""

    approved_prompt: str
    profile_id: str
    profile_name: str
    expected_account: str
    profile_home: Path
    project_id: str
    model: str
    aspect_ratio: str
    duration_seconds: int
    output_count: int
    staging_root: Path
    staging_output_path: Path
    production_job_id: str
    scene_job_id: str
    request_idempotency_key: str
    generation_authorized: bool
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Bounded process result supplied by the real runner or an offline fake."""

    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_truncated: bool = False
    process_started_at: str | None = None
    process_ended_at: str | None = None
    process_duration_ms: int | None = None
    process_signal: int | None = None
    process_killed_by_caller: bool | None = None
    stdout_byte_count: int | None = None
    stderr_byte_count: int | None = None
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None


EvidenceCallback = Callable[[str, Mapping[str, object]], None]


@dataclass(frozen=True, slots=True)
class AdapterResult:
    """Stable local result; success only means upstream produced a local MP4."""

    schema_version: str
    status: AdapterStatus
    exit_code: int | None
    upstream_operation_id: str | None
    requested_project_id: str
    project_binding_source: str
    profile_id: str
    production_job_id: str
    scene_job_id: str
    request_idempotency_key: str
    media_id: str | None
    local_output_path: Path | None
    stdout_digest: str
    stderr_digest: str
    elapsed_ms: int
    normalized_error_code: str | None
    upstream_error_type: str | None
    submit_may_have_occurred: bool
    auto_retry: bool
    safe_message: str
    remediation: str
    drive_file_id: str | None = None
    d2_run_id: str | None = None
    submission_state: str = "UNKNOWN"
    generate_action_observed: bool | str = "UNKNOWN"
    generate_action_count: int | str = "UNKNOWN"
    submit_action_observed: bool | str = "UNKNOWN"
    submit_transport_observed: bool | str = "UNKNOWN"
    submit_timestamp: str = "UNKNOWN"
    provider_request_id: str = "UNKNOWN"
    provider_operation_id: str = "UNKNOWN"
    provider_media_id: str = "UNKNOWN"
    process_started_at: str | None = None
    process_ended_at: str | None = None
    process_duration_ms: int | None = None
    process_return_code: int | None = None
    process_signal: int | None = None
    process_timed_out: bool | None = None
    process_killed_by_caller: bool | None = None
    process_stdout_byte_count: int | None = None
    process_stderr_byte_count: int | None = None
    process_stdout_sha256: str | None = None
    process_stderr_sha256: str | None = None
    process_output_truncated: bool | None = None
    parse_error_type: str | None = None
    parse_error_message: str | None = None
    output_top_level_type: str | None = None
    safe_stdout_prefix: str | None = None
    safe_stdout_suffix: str | None = None
    safe_stderr_prefix: str | None = None
    safe_stderr_suffix: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return the stable local schema without prompt, account, or raw streams."""
        return {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "exit_code": self.exit_code,
            "upstream_operation_id": self.upstream_operation_id,
            "requested_project_id": self.requested_project_id,
            "project_binding_source": self.project_binding_source,
            "profile_id": self.profile_id,
            "production_job_id": self.production_job_id,
            "scene_job_id": self.scene_job_id,
            "request_idempotency_key": self.request_idempotency_key,
            "media_id": self.media_id,
            "local_output_path": str(self.local_output_path)
            if self.local_output_path is not None
            else None,
            "stdout_digest": self.stdout_digest,
            "stderr_digest": self.stderr_digest,
            "elapsed_ms": self.elapsed_ms,
            "normalized_error_code": self.normalized_error_code,
            "upstream_error_type": self.upstream_error_type,
            "submit_may_have_occurred": self.submit_may_have_occurred,
            "auto_retry": self.auto_retry,
            "safe_message": self.safe_message,
            "remediation": self.remediation,
            "drive_file_id": self.drive_file_id,
            "d2_run_id": self.d2_run_id,
            "submission_state": self.submission_state,
            "generate_action_observed": self.generate_action_observed,
            "generate_action_count": self.generate_action_count,
            "submit_action_observed": self.submit_action_observed,
            "submit_transport_observed": self.submit_transport_observed,
            "submit_timestamp": self.submit_timestamp,
            "provider_request_id": self.provider_request_id,
            "provider_operation_id": self.provider_operation_id,
            "provider_media_id": self.provider_media_id,
            "process_started_at": self.process_started_at or "UNKNOWN",
            "process_ended_at": self.process_ended_at or "UNKNOWN",
            "process_duration_ms": self.process_duration_ms
            if self.process_duration_ms is not None
            else "UNKNOWN",
            "process_return_code": self.process_return_code
            if self.process_return_code is not None
            else "UNKNOWN",
            "process_signal": self.process_signal if self.process_signal is not None else "UNKNOWN",
            "process_timed_out": self.process_timed_out
            if self.process_timed_out is not None
            else "UNKNOWN",
            "process_killed_by_caller": self.process_killed_by_caller
            if self.process_killed_by_caller is not None
            else "UNKNOWN",
            "process_stdout_byte_count": self.process_stdout_byte_count
            if self.process_stdout_byte_count is not None
            else "UNKNOWN",
            "process_stderr_byte_count": self.process_stderr_byte_count
            if self.process_stderr_byte_count is not None
            else "UNKNOWN",
            "process_stdout_sha256": self.process_stdout_sha256 or "UNKNOWN",
            "process_stderr_sha256": self.process_stderr_sha256 or "UNKNOWN",
            "process_output_truncated": self.process_output_truncated
            if self.process_output_truncated is not None
            else "UNKNOWN",
            "parse_error_type": self.parse_error_type or "UNKNOWN",
            "parse_error_message": self.parse_error_message or "UNKNOWN",
            "output_top_level_type": self.output_top_level_type or "UNKNOWN",
            "safe_stdout_prefix": self.safe_stdout_prefix
            if self.safe_stdout_prefix is not None
            else "UNKNOWN",
            "safe_stdout_suffix": self.safe_stdout_suffix
            if self.safe_stdout_suffix is not None
            else "UNKNOWN",
            "safe_stderr_prefix": self.safe_stderr_prefix
            if self.safe_stderr_prefix is not None
            else "UNKNOWN",
            "safe_stderr_suffix": self.safe_stderr_suffix
            if self.safe_stderr_suffix is not None
            else "UNKNOWN",
        }


Runner = Callable[..., CommandResult]


class ProcessLaunchError(OSError):
    """The child never started, so the failure is provably pre-submit."""


class UpstreamGflowVideoAdapter:
    """Invoke one pinned upstream CLI request without owning production policy."""

    def __init__(
        self,
        *,
        python_executable: Path,
        pinned_version: str = UPSTREAM_VERSION,
        runner: Runner | None = None,
        runtime_pin_verifier: Callable[[], None] | None = None,
    ) -> None:
        if not python_executable.is_absolute():
            raise ValueError("the pinned upstream Python executable must be absolute")
        if pinned_version != UPSTREAM_VERSION:
            raise ValueError("adapter V1 only supports the audited upstream CLI version")
        self._python = python_executable
        self._runner = _run_bounded_process if runner is None else runner
        self._runtime_pin_verifier = runtime_pin_verifier
        self._seen_keys: set[str] = set()
        self._seen_lock = threading.Lock()

    def generate_once(
        self,
        request: AdapterRequest,
        *,
        evidence_callback: EvidenceCallback | None = None,
    ) -> AdapterResult:
        """Run one generation; any post-launch uncertainty is non-retryable."""
        started = time.monotonic()
        base = _result_base(request)
        if not request.generation_authorized:
            return _failure(
                base,
                status=AdapterStatus.PRE_SUBMIT_FAILURE,
                error_code="GENERATION_NOT_AUTHORIZED",
                message="Generation was not authorized by the caller.",
                remediation=(
                    "Obtain external production authorization before invoking the adapter."
                ),
                elapsed_ms=_elapsed_ms(started),
            )
        with self._seen_lock:
            if request.request_idempotency_key in self._seen_keys:
                return _failure(
                    base,
                    status=AdapterStatus.PRE_SUBMIT_FAILURE,
                    error_code="DUPLICATE_INVOCATION",
                    message="This adapter instance already consumed the invocation key.",
                    remediation=(
                        "Use the caller's durable Production Job state; "
                        "do not create another attempt."
                    ),
                    elapsed_ms=_elapsed_ms(started),
                )
        problem = _validate_request(request)
        if problem is not None:
            return _failure(
                base,
                status=AdapterStatus.PRE_SUBMIT_FAILURE,
                error_code="INVALID_REQUEST",
                message="The upstream video request failed local validation.",
                remediation=problem,
                elapsed_ms=_elapsed_ms(started),
            )
        with self._seen_lock:
            if request.request_idempotency_key in self._seen_keys:
                return _failure(
                    base,
                    status=AdapterStatus.PRE_SUBMIT_FAILURE,
                    error_code="DUPLICATE_INVOCATION",
                    message="This adapter instance already consumed the invocation key.",
                    remediation=(
                        "Use the caller's durable Production Job state; "
                        "do not create another attempt."
                    ),
                    elapsed_ms=_elapsed_ms(started),
                )
            self._seen_keys.add(request.request_idempotency_key)
            if len(self._seen_keys) > _MAX_IDEMPOTENCY_KEYS:
                # Fail closed rather than silently evicting a key that could be replayed.
                return _failure(
                    base,
                    status=AdapterStatus.PRE_SUBMIT_FAILURE,
                    error_code="IDEMPOTENCY_CAPACITY_EXHAUSTED",
                    message="The adapter invocation guard reached its configured capacity.",
                    remediation=(
                        "Review durable caller state before creating a fresh adapter instance."
                    ),
                    elapsed_ms=_elapsed_ms(started),
                )

        if self._runtime_pin_verifier is not None:
            try:
                self._runtime_pin_verifier()
            except Exception:
                return _failure(
                    base,
                    status=AdapterStatus.PRE_SUBMIT_FAILURE,
                    error_code="UPSTREAM_RUNTIME_PIN_MISMATCH",
                    message="The installed upstream runtime does not match its pinned source.",
                    remediation=(
                        "Verify the isolated runtime manifest, source commit, and artifact hashes."
                    ),
                    elapsed_ms=_elapsed_ms(started),
                )

        environment = upstream_version_probe_environment(request.profile_home)
        version_result: CommandResult
        try:
            version_result = self._runner(
                upstream_version_probe_command(self._python),
                b"",
                upstream_version_probe_cwd(request.profile_home),
                environment,
                min(15.0, request.timeout_seconds),
            )
        except (OSError, subprocess.SubprocessError):
            return _failure(
                base,
                status=AdapterStatus.PRE_SUBMIT_FAILURE,
                error_code="UPSTREAM_CLI_UNAVAILABLE",
                message="The pinned upstream CLI could not be checked before generation.",
                remediation=(
                    "Verify the absolute interpreter and installed gflow-cli v0.92.1 package."
                ),
                elapsed_ms=_elapsed_ms(started),
            )
        if (
            version_result.timed_out
            or version_result.returncode != 0
            or version_result.output_truncated
            or not upstream_version_matches(version_result.stdout)
        ):
            return _failure(
                base,
                status=AdapterStatus.PRE_SUBMIT_FAILURE,
                exit_code=version_result.returncode,
                error_code="UPSTREAM_VERSION_MISMATCH",
                stdout=version_result.stdout,
                stderr=version_result.stderr,
                message="The installed upstream CLI does not match the pinned adapter contract.",
                remediation=(
                    f"Install the audited gflow-cli {UPSTREAM_VERSION} artifact and "
                    "recheck its pinned source."
                ),
                elapsed_ms=_elapsed_ms(started),
            )

        cli_args = _build_cli_args(request)
        payload = json.dumps({"args": cli_args}, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        observed: dict[str, object] = {}

        def on_evidence(kind: str, data: Mapping[str, object]) -> None:
            if kind in {"SUBMIT_OBSERVATION", "PROVIDER_IDENTIFIED"}:
                observed.update(data)
            _emit_evidence(evidence_callback, kind, data)

        try:
            result = self._runner(
                (str(self._python), "-c", _CLI_LAUNCHER),
                payload,
                request.profile_home,
                environment,
                request.timeout_seconds,
                event_callback=on_evidence,
            )
        except ProcessLaunchError:
            return _failure(
                base,
                status=AdapterStatus.PRE_SUBMIT_FAILURE,
                error_code="UPSTREAM_PROCESS_NOT_STARTED",
                message="The upstream CLI process could not be started.",
                remediation=(
                    "Check the pinned interpreter and staging/profile access before "
                    "reevaluating the same job."
                ),
                elapsed_ms=_elapsed_ms(started),
            )
        except (OSError, subprocess.SubprocessError):
            return _failure(
                base,
                status=AdapterStatus.POST_SUBMIT_UNKNOWN,
                error_code="UPSTREAM_PROCESS_INTERRUPTED",
                submit_may_have_occurred=True,
                message="The upstream process was interrupted after launch.",
                remediation=(
                    "Inspect and reconcile this same project/job before any further invocation."
                ),
                elapsed_ms=_elapsed_ms(started),
            )

        common = replace(
            base,
            exit_code=result.returncode,
            stdout_digest=_digest(result.stdout),
            stderr_digest=_digest(result.stderr),
            elapsed_ms=_elapsed_ms(started),
            generate_action_observed=observed.get("generate_action_observed", "UNKNOWN"),
            generate_action_count=observed.get("generate_action_count", "UNKNOWN"),
            process_started_at=result.process_started_at,
            process_ended_at=result.process_ended_at,
            process_duration_ms=result.process_duration_ms,
            process_return_code=result.returncode,
            process_signal=result.process_signal
            if result.process_signal is not None
            else -result.returncode
            if os.name != "nt" and result.returncode is not None and result.returncode < 0
            else None,
            process_timed_out=result.timed_out,
            process_killed_by_caller=result.process_killed_by_caller
            if result.process_killed_by_caller is not None
            else result.timed_out,
            process_stdout_byte_count=result.stdout_byte_count
            if result.stdout_byte_count is not None
            else len(result.stdout),
            process_stderr_byte_count=result.stderr_byte_count
            if result.stderr_byte_count is not None
            else len(result.stderr),
            process_stdout_sha256=result.stdout_sha256 or _digest(result.stdout),
            process_stderr_sha256=result.stderr_sha256 or _digest(result.stderr),
            process_output_truncated=result.output_truncated,
            submit_action_observed=observed.get("submit_action_observed", "UNKNOWN"),
            submit_transport_observed=observed.get("submit_transport_observed", "UNKNOWN"),
            submit_timestamp=str(observed.get("submit_timestamp", "UNKNOWN")),
            provider_request_id=str(observed.get("provider_request_id", "UNKNOWN")),
            provider_operation_id=str(observed.get("operation_id", "UNKNOWN")),
            provider_media_id=str(observed.get("media_id", "UNKNOWN")),
            submission_state=_submission_state(None, observed),
        )
        _emit_process_ended(evidence_callback, result)
        if result.timed_out:
            return _failure(
                common,
                status=AdapterStatus.POST_SUBMIT_UNKNOWN,
                error_code="PROCESS_TIMEOUT_AFTER_LAUNCH",
                submit_may_have_occurred=True,
                message=(
                    "The upstream process timed out; Flow may have accepted or billed the request."
                ),
                remediation=(
                    "Inspect and reconcile this same project/job before any further invocation."
                ),
                submission_state="POST_SUBMIT_UNKNOWN",
            )
        if result.output_truncated:
            diagnostic = _parse_diagnostic(result.stdout, result.stderr)
            _emit_evidence(evidence_callback, "PARSE_DIAGNOSTIC", diagnostic)
            return _failure(
                common,
                status=AdapterStatus.POST_SUBMIT_UNKNOWN,
                error_code="UPSTREAM_OUTPUT_TRUNCATED",
                submit_may_have_occurred=True,
                message="The upstream process output exceeded the safe capture limit.",
                remediation=(
                    "Inspect and reconcile this same project/job; do not invoke Generate again."
                ),
                **cast(Any, diagnostic),
            )
        payload_obj = _parse_json_object(result.stdout)
        if payload_obj is None:
            diagnostic = _parse_diagnostic(result.stdout, result.stderr)
            _emit_evidence(evidence_callback, "PARSE_DIAGNOSTIC", diagnostic)
            return _failure(
                common,
                status=AdapterStatus.POST_SUBMIT_UNKNOWN,
                error_code="UPSTREAM_RESULT_UNREADABLE",
                submit_may_have_occurred=True,
                message="The upstream process returned no valid machine-readable result.",
                remediation=(
                    "Inspect and reconcile this same project/job before any further invocation."
                ),
                submission_state=_submission_state(AdapterStatus.POST_SUBMIT_UNKNOWN, observed),
                **cast(Any, diagnostic),
            )
        _emit_evidence(
            evidence_callback,
            "PARSER_RESULT",
            {"parse_result": "JSON_OBJECT_ACCEPTED", "output_top_level_type": "dict"},
        )
        explicit_ids = {
            "provider_request_id": _first_string(payload_obj, "provider_request_id", "request_id"),
            "operation_id": _first_string(payload_obj, "operation_id", "upstream_operation_id"),
            "media_id": _first_string(payload_obj, "media_id"),
        }
        explicit_ids = {key: value for key, value in explicit_ids.items() if value is not None}
        if explicit_ids:
            _emit_evidence(evidence_callback, "PROVIDER_IDENTIFIED", explicit_ids)
            observed.update(explicit_ids)
        common = replace(
            common,
            provider_request_id=str(observed.get("provider_request_id", "UNKNOWN")),
            provider_operation_id=str(observed.get("operation_id", "UNKNOWN")),
            provider_media_id=str(observed.get("media_id", "UNKNOWN")),
            submission_state=_submission_state(
                None,
                {
                    **observed,
                    "provider_status": _first_string(
                        payload_obj, "provider_status", "submission_status"
                    ),
                },
            ),
        )
        if result.returncode != 0 or payload_obj.get("status") != "ok":
            return _normalize_failure(common, payload_obj)

        if common.submission_state in {"SUBMITTED_PENDING", "SUBMITTED_PROVIDER_FAILURE"}:
            return _failure(
                common,
                status=AdapterStatus.POST_SUBMIT_UNKNOWN,
                error_code="UPSTREAM_PROVIDER_STATUS_NOT_TERMINAL_SUCCESS",
                submit_may_have_occurred=True,
                message="The upstream provider status does not confirm a completed generation.",
                remediation="Reconcile the same invocation before any further action.",
            )

        validated = _validate_success_payload(payload_obj, request)
        if validated is None:
            return _failure(
                common,
                status=AdapterStatus.POST_SUBMIT_UNKNOWN,
                error_code="UPSTREAM_SUCCESS_CONTRACT_MISMATCH",
                submit_may_have_occurred=True,
                message=(
                    "The upstream result did not match the requested generation or staged output."
                ),
                remediation="Inspect and reconcile the same project/job; do not retry generation.",
            )
        media_id, output_path = validated
        return replace(
            common,
            status=AdapterStatus.UPSTREAM_GENERATION_COMPLETED,
            upstream_operation_id=None,
            media_id=media_id,
            local_output_path=output_path,
            normalized_error_code=None,
            upstream_error_type=None,
            submit_may_have_occurred=True,
            auto_retry=False,
            safe_message="The upstream CLI completed and produced a local MP4.",
            remediation=(
                "Continue with the existing media validator and canonical storage workflow."
            ),
            submission_state="SUCCESS",
            generate_action_observed="UNKNOWN",
            generate_action_count="UNKNOWN",
        )


def _build_cli_args(request: AdapterRequest) -> list[str]:
    """Construct the pinned CLI's actual t2v arguments, keeping prompt in stdin JSON."""
    args = [
        "video",
        "t2v",
        request.approved_prompt,
        "--profile",
        request.profile_name,
        "--project",
        request.project_id,
        "--model",
        request.model,
        "--aspect",
        request.aspect_ratio,
        "--duration",
        str(request.duration_seconds),
        "--count",
        "1",
        "--output",
        str(request.staging_output_path.resolve(strict=False)),
        "--json",
    ]
    return args


def _validate_request(request: AdapterRequest) -> str | None:
    if not request.approved_prompt.strip() or len(request.approved_prompt) > _MAX_PROMPT_CHARS:
        return "Provide a non-empty approved prompt no longer than 65,536 characters."
    if not request.profile_id.strip() or not _PROFILE_RE.fullmatch(request.profile_name):
        return "A canonical profile ID and safe upstream profile name are required."
    if "@" not in request.expected_account or any(c.isspace() for c in request.expected_account):
        return "An exact expected account email is required from the production identity guard."
    if not request.profile_home.is_absolute() or not request.profile_home.is_dir():
        return "The exact profile home must exist and be an absolute directory."
    try:
        if str(UUID(request.project_id)) != request.project_id:
            return "An exact canonical existing Flow project UUID is required."
    except (ValueError, AttributeError):
        return "An exact canonical existing Flow project UUID is required."
    if request.model not in _MODEL_MAP:
        return "The requested model is not supported by the pinned upstream CLI contract."
    if request.aspect_ratio not in _ASPECT_MAP:
        return "The requested aspect ratio is not supported by the pinned upstream CLI contract."
    if isinstance(request.duration_seconds, bool) or request.duration_seconds not in {4, 6, 8, 10}:
        return "The requested duration is not supported by the pinned upstream CLI contract."
    if request.duration_seconds == 10 and request.model != "omni-flash":
        return "Ten-second duration is supported only for omni-flash by the pinned contract."
    if request.output_count != 1:
        return "Production adapter V1 is restricted to exactly one output."
    if request.timeout_seconds < 30 or request.timeout_seconds > 1800:
        return "The process timeout must be between 30 and 1,800 seconds."
    if not all(
        _ID_RE.fullmatch(value)
        for value in (
            request.production_job_id,
            request.scene_job_id,
            request.request_idempotency_key,
        )
    ):
        return "Production Job, Scene Job, and idempotency identities must be stable safe tokens."
    if not request.profile_home.resolve(strict=False).is_absolute():
        return "The profile home could not be resolved safely."
    if not request.staging_root.is_absolute() or not request.staging_root.is_dir():
        return "The staging root must be an existing absolute directory."
    if request.staging_root.is_symlink():
        return "The staging root cannot be a symlink."
    root = request.staging_root.resolve(strict=True)
    output = request.staging_output_path.resolve(strict=False)
    if output.suffix.lower() != ".mp4" or not output.is_relative_to(root):
        return "The output must be an MP4 path inside the exact staging root."
    if output.exists():
        return "The output path already exists; use a fresh job-specific staging path."
    cursor = request.staging_output_path.parent
    while cursor != request.staging_root and request.staging_root in cursor.parents:
        if cursor.is_symlink():
            return "The output path cannot traverse a symlink inside staging."
        cursor = cursor.parent
    return None


def _validate_success_payload(
    payload: Mapping[str, object], request: AdapterRequest
) -> tuple[str, Path] | None:
    if (
        payload.get("status") != "ok"
        or payload.get("command") != "video t2v"
        or payload.get("succeeded") is not True
        or payload.get("generation_status") != "MEDIA_GENERATION_STATUS_SUCCESSFUL"
    ):
        return None
    media_id = payload.get("media_id")
    if not isinstance(media_id, str):
        return None
    try:
        if str(UUID(media_id)) != media_id.lower():
            return None
    except ValueError:
        return None
    request_data = payload.get("request")
    if not isinstance(request_data, dict):
        return None
    request_data = cast(dict[str, object], request_data)
    if (
        request_data.get("model") != _MODEL_MAP[request.model]
        or request_data.get("mode") != "t2v"
        or request_data.get("aspect") != _ASPECT_MAP[request.aspect_ratio]
        or request_data.get("duration") != request.duration_seconds
        or request_data.get("count") != 1
    ):
        return None
    local_path = payload.get("local_path")
    if not isinstance(local_path, str):
        return None
    output = Path(local_path)
    try:
        resolved = output.resolve(strict=True)
        root = request.staging_root.resolve(strict=True)
    except OSError:
        return None
    if (
        not resolved.is_file()
        or resolved.suffix.lower() != ".mp4"
        or not resolved.is_relative_to(root)
    ):
        return None
    if resolved != request.staging_output_path.resolve(strict=False):
        return None
    return media_id, resolved


def _normalize_failure(base: AdapterResult, payload: Mapping[str, object]) -> AdapterResult:
    error_value = payload.get("error")
    error = cast(dict[str, object], error_value) if isinstance(error_value, dict) else {}
    error_type = error.get("class")
    error_type = error_type if isinstance(error_type, str) and error_type.isidentifier() else None
    if error_type in _AUTH_ERRORS:
        return _failure(
            base,
            status=AdapterStatus.AUTH_FAILURE,
            error_code="UPSTREAM_AUTH_FAILURE",
            upstream_error_type=error_type,
            submit_may_have_occurred=False,
            message="The pinned CLI could not verify the requested authenticated profile.",
            remediation=(
                "Have an operator verify the exact profile/account, then reevaluate this same job."
            ),
        )
    if error_type in _CREDIT_ERRORS:
        return _failure(
            base,
            status=AdapterStatus.INSUFFICIENT_CREDITS,
            error_code="UPSTREAM_INSUFFICIENT_CREDITS",
            upstream_error_type=error_type,
            submit_may_have_occurred=False,
            message="Flow reported insufficient credits before generation.",
            remediation="Recheck the canonical quote and credit guard; do not retry automatically.",
        )
    if error_type in _DOWNLOAD_ERRORS:
        return _failure(
            base,
            status=AdapterStatus.DOWNLOAD_FAILURE_AFTER_GENERATION,
            error_code="UPSTREAM_DOWNLOAD_FAILURE",
            upstream_error_type=error_type,
            submit_may_have_occurred=True,
            message="The upstream CLI reports that generation completed but media download failed.",
            remediation="Recover the existing asset in Flow; do not Generate again.",
        )
    if error_type in _UNSUPPORTED_ERRORS:
        return _failure(
            base,
            status=AdapterStatus.UNSUPPORTED_FLOW_STATE,
            error_code="UPSTREAM_FLOW_STATE_UNSUPPORTED",
            upstream_error_type=error_type,
            submit_may_have_occurred=False,
            message="The current Flow host or account state is unsupported by the pinned CLI path.",
            remediation="Stop and review the exact account/project and supported Flow state.",
        )
    if error_type in _PROJECT_ERRORS:
        return _failure(
            base,
            status=AdapterStatus.PROJECT_NAVIGATION_FAILURE,
            error_code="UPSTREAM_PROJECT_OR_UI_NAVIGATION_FAILURE",
            upstream_error_type=error_type,
            submit_may_have_occurred=True,
            message=(
                "The pinned CLI could not establish or reliably observe the expected project UI."
            ),
            remediation=(
                "Inspect and reconcile this same project/job before any further invocation."
            ),
        )
    # The CLI does not expose a durable submit-stage marker. Everything else is
    # conservatively ambiguous, regardless of the upstream retryable flag.
    return _failure(
        base,
        status=AdapterStatus.POST_SUBMIT_UNKNOWN,
        error_code="UPSTREAM_ERROR_SUBMISSION_STATE_UNKNOWN",
        upstream_error_type=error_type,
        submit_may_have_occurred=True,
        message="The upstream CLI error does not prove that Flow did not accept the request.",
        remediation="Inspect and reconcile this same project/job before any further invocation.",
    )


def _result_base(request: AdapterRequest) -> AdapterResult:
    return AdapterResult(
        schema_version=RESULT_SCHEMA_VERSION,
        status=AdapterStatus.PRE_SUBMIT_FAILURE,
        exit_code=None,
        upstream_operation_id=None,
        requested_project_id=request.project_id,
        project_binding_source="REQUEST_ARGUMENT_ONLY",
        profile_id=request.profile_id,
        production_job_id=request.production_job_id,
        scene_job_id=request.scene_job_id,
        request_idempotency_key=request.request_idempotency_key,
        media_id=None,
        local_output_path=None,
        stdout_digest=_digest(b""),
        stderr_digest=_digest(b""),
        elapsed_ms=0,
        normalized_error_code=None,
        upstream_error_type=None,
        submit_may_have_occurred=False,
        auto_retry=False,
        safe_message="",
        remediation="",
    )


def _failure(
    base: AdapterResult,
    *,
    status: AdapterStatus,
    error_code: str,
    message: str,
    remediation: str,
    upstream_error_type: str | None = None,
    submit_may_have_occurred: bool = False,
    exit_code: int | None = None,
    stdout: bytes | None = None,
    stderr: bytes | None = None,
    elapsed_ms: int | None = None,
    **diagnostics: object,
) -> AdapterResult:
    submission_state = (
        "PRE_SUBMIT_FAILURE"
        if status
        in {
            AdapterStatus.PRE_SUBMIT_FAILURE,
            AdapterStatus.AUTH_FAILURE,
            AdapterStatus.UNSUPPORTED_FLOW_STATE,
            AdapterStatus.INSUFFICIENT_CREDITS,
        }
        else "DOWNLOAD_FAILURE_AFTER_GENERATION"
        if status is AdapterStatus.DOWNLOAD_FAILURE_AFTER_GENERATION
        else "POST_SUBMIT_UNKNOWN"
    )
    if base.submission_state in {
        "SUBMITTED_PENDING",
        "SUBMITTED_PROVIDER_FAILURE",
        "SUCCESS",
        "DOWNLOAD_FAILURE_AFTER_GENERATION",
    }:
        submission_state = base.submission_state
    submission_state = str(diagnostics.pop("submission_state", submission_state))
    return replace(
        base,
        status=status,
        exit_code=exit_code,
        normalized_error_code=error_code,
        upstream_error_type=upstream_error_type,
        submit_may_have_occurred=submit_may_have_occurred,
        auto_retry=False,
        safe_message=message,
        remediation=remediation,
        stdout_digest=_digest(stdout) if stdout is not None else base.stdout_digest,
        stderr_digest=_digest(stderr) if stderr is not None else base.stderr_digest,
        elapsed_ms=elapsed_ms if elapsed_ms is not None else base.elapsed_ms,
        submission_state=submission_state,
        **diagnostics,
    )


def _parse_json_object(output: bytes) -> dict[str, object] | None:
    try:
        decoded = output.decode("utf-8")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    return cast(dict[str, object], value)


def upstream_version_probe_command(python_executable: Path) -> tuple[str, ...]:
    """Return the single module-version command shared by preflight and runtime."""
    return (str(python_executable), "-m", "gflow_cli", "--version")


def upstream_version_probe_cwd(profile_home: Path) -> Path:
    """Use the exact profile working directory for preflight and invocation probes."""
    return profile_home.resolve(strict=True)


def upstream_version_probe_environment(profile_home: Path) -> dict[str, str]:
    """Build the deterministic subprocess environment used by both version probes."""
    return _child_environment(profile_home)


def upstream_version_matches(output: bytes | str) -> bool:
    """Require the exact Click label emitted by ``python -m gflow_cli --version``."""
    try:
        text = output.decode("utf-8").strip() if isinstance(output, bytes) else output.strip()
    except UnicodeDecodeError:
        return False
    return text == f"python -m gflow_cli, version {UPSTREAM_VERSION}"


def _child_environment(profile_home: Path) -> dict[str, str]:
    """Pass only platform essentials and the explicitly bound CLI home."""
    names = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE")
    env = {name: os.environ[name] for name in names if os.environ.get(name)}
    env["GFLOW_CLI_HOME"] = str(profile_home.resolve(strict=True))
    return env


def _run_bounded_process(
    command: tuple[str, ...],
    stdin: bytes,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: float,
    *,
    event_callback: EvidenceCallback | None = None,
) -> CommandResult:
    """Run one command with bounded capture and process-tree termination."""
    if not command or not Path(command[0]).is_absolute():
        raise OSError("pinned executable must be absolute")
    stdout_capture = _StreamCapture()
    stderr_capture = _StreamCapture()
    truncated = threading.Event()
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=dict(env),
            shell=False,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
        )
    except OSError as exc:
        raise ProcessLaunchError("pinned upstream process did not start") from exc
    assert process.stdout is not None and process.stderr is not None and process.stdin is not None

    process_started = datetime.now(UTC)
    started_monotonic = time.monotonic()
    try:
        _emit_evidence(
            event_callback,
            "PROCESS_STARTED",
            {"process_started_at": _utc_string(process_started)},
        )
    except BaseException:
        _terminate_process_tree(process)
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()
        raise

    out_thread = threading.Thread(
        target=_drain_pipe, args=(process.stdout, stdout_capture, truncated), daemon=True
    )
    err_thread = threading.Thread(
        target=_drain_pipe, args=(process.stderr, stderr_capture, truncated), daemon=True
    )
    out_thread.start()
    err_thread.start()
    try:
        process.stdin.write(stdin)
        process.stdin.close()
        process.wait(timeout=timeout_seconds)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_tree(process)
    except BrokenPipeError:
        try:
            process.stdin.close()
        except OSError:
            pass
        process.wait(timeout=5)
        timed_out = False
    finally:
        if process.poll() is None:
            _terminate_process_tree(process)
        out_thread.join(timeout=5)
        err_thread.join(timeout=5)
        process.stdout.close()
        process.stderr.close()
    ended = datetime.now(UTC)
    returncode = process.returncode
    return CommandResult(
        returncode=returncode,
        stdout=bytes(stdout_capture.buffer),
        stderr=bytes(stderr_capture.buffer),
        timed_out=timed_out,
        output_truncated=truncated.is_set(),
        process_started_at=_utc_string(process_started),
        process_ended_at=_utc_string(ended),
        process_duration_ms=max(0, int((time.monotonic() - started_monotonic) * 1000)),
        process_signal=-returncode
        if os.name != "nt" and returncode is not None and returncode < 0
        else None,
        process_killed_by_caller=timed_out,
        stdout_byte_count=stdout_capture.byte_count,
        stderr_byte_count=stderr_capture.byte_count,
        stdout_sha256=stdout_capture.digest.hexdigest(),
        stderr_sha256=stderr_capture.digest.hexdigest(),
    )


@dataclass(slots=True)
class _StreamCapture:
    buffer: bytearray = dataclass_field(default_factory=bytearray)
    digest: Any = dataclass_field(default_factory=hashlib.sha256)
    byte_count: int = 0


def _drain_pipe(stream: BinaryIO, target: _StreamCapture, truncated: threading.Event) -> None:
    while True:
        chunk = stream.read(64 * 1024)
        if not chunk:
            return
        target.byte_count += len(chunk)
        target.digest.update(chunk)
        remaining = _MAX_CAPTURE_BYTES - len(target.buffer)
        if remaining > 0:
            target.buffer.extend(chunk[:remaining])
        if len(chunk) > remaining:
            truncated.set()


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        # Descendants can outlive the process leader; still signal its group below.
        pass
    if os.name == "nt":
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        taskkill = Path(system_root) / "System32" / "taskkill.exe"
        try:
            subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                timeout=10,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            if process.poll() is None:
                process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            time.sleep(0.2)
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _emit_evidence(
    callback: EvidenceCallback | None, event_type: str, evidence: Mapping[str, object]
) -> None:
    if callback is not None:
        callback(event_type, evidence)


def _emit_process_ended(callback: EvidenceCallback | None, result: CommandResult) -> None:
    _emit_evidence(
        callback,
        "PROCESS_ENDED",
        {
            "process_ended_at": result.process_ended_at or "UNKNOWN",
            "process_duration_ms": result.process_duration_ms
            if result.process_duration_ms is not None
            else "UNKNOWN",
            "process_return_code": result.returncode
            if result.returncode is not None
            else "UNKNOWN",
            "process_signal": result.process_signal
            if result.process_signal is not None
            else "UNKNOWN",
            "process_timed_out": result.timed_out,
            "process_killed_by_caller": result.process_killed_by_caller
            if result.process_killed_by_caller is not None
            else "UNKNOWN",
            "process_stdout_byte_count": result.stdout_byte_count
            if result.stdout_byte_count is not None
            else len(result.stdout),
            "process_stderr_byte_count": result.stderr_byte_count
            if result.stderr_byte_count is not None
            else len(result.stderr),
            "process_stdout_sha256": result.stdout_sha256 or _digest(result.stdout),
            "process_stderr_sha256": result.stderr_sha256 or _digest(result.stderr),
            "process_output_truncated": result.output_truncated,
        },
    )


def _parse_diagnostic(stdout: bytes, stderr: bytes) -> dict[str, object]:
    error_type = "JSONDecodeError"
    error_message = "output is not a JSON object"
    top_level_type = "UNKNOWN"
    try:
        decoded = stdout.decode("utf-8")
        value = json.loads(decoded)
        top_level_type = type(value).__name__
        if isinstance(value, dict):
            error_type = "UNKNOWN"
            error_message = "JSON object failed strict result parsing"
        else:
            error_type = "UnexpectedTopLevelType"
            error_message = f"expected object; received {top_level_type}"
    except UnicodeDecodeError as exc:
        error_type = "UnicodeDecodeError"
        error_message = str(exc)
    except json.JSONDecodeError as exc:
        error_type = "JSONDecodeError"
        error_message = exc.msg
    try:
        safe_error_message = _safe_text(error_message, _MAX_DIAGNOSTIC_CHARS)
        safe_stdout_prefix = _safe_excerpt(stdout, prefix=True)
        safe_stdout_suffix = _safe_excerpt(stdout, prefix=False)
        safe_stderr_prefix = _safe_excerpt(stderr, prefix=True)
        safe_stderr_suffix = _safe_excerpt(stderr, prefix=False)
    except Exception:
        safe_error_message = "diagnostic redaction failed; omitted"
        safe_stdout_prefix = None
        safe_stdout_suffix = None
        safe_stderr_prefix = None
        safe_stderr_suffix = None
    return {
        "parse_error_type": error_type,
        "parse_error_message": safe_error_message,
        "output_top_level_type": top_level_type,
        "safe_stdout_prefix": safe_stdout_prefix,
        "safe_stdout_suffix": safe_stdout_suffix,
        "safe_stderr_prefix": safe_stderr_prefix,
        "safe_stderr_suffix": safe_stderr_suffix,
    }


def _safe_excerpt(raw: bytes, *, prefix: bool) -> str | None:
    try:
        text = raw.decode("utf-8", errors="replace")
        safe = _safe_text(text, None)
        excerpt = safe[:_MAX_DIAGNOSTIC_CHARS] if prefix else safe[-_MAX_DIAGNOSTIC_CHARS:]
        return _OPAQUE_SECRET_RE.sub("[REDACTED]", excerpt)
    except Exception:
        return None


def _safe_text(value: str, max_chars: int | None) -> str:
    safe = redact_sensitive_diagnostic(value)
    safe = _OPAQUE_SECRET_RE.sub("[REDACTED]", safe)
    return safe if max_chars is None else safe[:max_chars]


def _first_string(payload: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _submission_state(status: AdapterStatus | None, observed: Mapping[str, object]) -> str:
    explicit = observed.get("submission_state")
    if explicit in {"SUBMITTED_PENDING", "SUBMITTED_PROVIDER_FAILURE"}:
        return str(explicit)
    provider_status = observed.get("provider_status")
    if isinstance(provider_status, str):
        if provider_status.upper() in {"PENDING", "RUNNING", "IN_PROGRESS"}:
            return "SUBMITTED_PENDING"
        if provider_status.upper() in {"FAILED", "ERROR", "REJECTED"}:
            return "SUBMITTED_PROVIDER_FAILURE"
    if status is AdapterStatus.UPSTREAM_GENERATION_COMPLETED:
        return "SUCCESS"
    if status is AdapterStatus.DOWNLOAD_FAILURE_AFTER_GENERATION:
        return "DOWNLOAD_FAILURE_AFTER_GENERATION"
    if status in {
        AdapterStatus.PRE_SUBMIT_FAILURE,
        AdapterStatus.AUTH_FAILURE,
        AdapterStatus.UNSUPPORTED_FLOW_STATE,
        AdapterStatus.INSUFFICIENT_CREDITS,
    }:
        return "PRE_SUBMIT_FAILURE"
    if (
        observed.get("submit_action_observed") is True
        or observed.get("submit_transport_observed") is True
        or status is AdapterStatus.POST_SUBMIT_UNKNOWN
    ):
        return "POST_SUBMIT_UNKNOWN"
    return "UNKNOWN"


def _utc_string(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))

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
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, cast
from uuid import UUID

UPSTREAM_VERSION = "0.92.1"
UPSTREAM_COMMIT = "63dd0081e117dc081ed7fc774d3dd19ac91ec9aa"
RESULT_SCHEMA_VERSION = "UPSTREAM_GFLOW_ADAPTER_RESULT_V1"
DEFAULT_TIMEOUT_SECONDS = 900.0
_MAX_PROMPT_CHARS = 65_536
_MAX_CAPTURE_BYTES = 1_048_576
_MAX_IDEMPOTENCY_KEYS = 10_000
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
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
        }


Runner = Callable[[tuple[str, ...], bytes, Path, Mapping[str, str], float], CommandResult]


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
    ) -> None:
        if not python_executable.is_absolute():
            raise ValueError("the pinned upstream Python executable must be absolute")
        if pinned_version != UPSTREAM_VERSION:
            raise ValueError("adapter V1 only supports the audited upstream CLI version")
        self._python = python_executable
        self._runner = _run_bounded_process if runner is None else runner
        self._seen_keys: set[str] = set()
        self._seen_lock = threading.Lock()

    def generate_once(self, request: AdapterRequest) -> AdapterResult:
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

        environment = _child_environment(request.profile_home)
        version_result: CommandResult
        try:
            version_result = self._runner(
                (str(self._python), "-m", "gflow_cli", "--version"),
                b"",
                request.profile_home,
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
            or not _version_matches(version_result.stdout)
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
        try:
            result = self._runner(
                (str(self._python), "-c", _CLI_LAUNCHER),
                payload,
                request.profile_home,
                environment,
                request.timeout_seconds,
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
        )
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
            )
        if result.output_truncated:
            return _failure(
                common,
                status=AdapterStatus.POST_SUBMIT_UNKNOWN,
                error_code="UPSTREAM_OUTPUT_TRUNCATED",
                submit_may_have_occurred=True,
                message="The upstream process output exceeded the safe capture limit.",
                remediation=(
                    "Inspect and reconcile this same project/job; do not invoke Generate again."
                ),
            )
        payload_obj = _parse_json_object(result.stdout)
        if payload_obj is None:
            return _failure(
                common,
                status=AdapterStatus.POST_SUBMIT_UNKNOWN,
                error_code="UPSTREAM_RESULT_UNREADABLE",
                submit_may_have_occurred=True,
                message="The upstream process returned no valid machine-readable result.",
                remediation=(
                    "Inspect and reconcile this same project/job before any further invocation."
                ),
            )
        if result.returncode != 0 or payload_obj.get("status") != "ok":
            return _normalize_failure(common, payload_obj)

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
) -> AdapterResult:
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


def _version_matches(output: bytes) -> bool:
    try:
        text = output.decode("utf-8").strip()
    except UnicodeDecodeError:
        return False
    return text == f"gflow, version {UPSTREAM_VERSION}"


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
) -> CommandResult:
    """Run one command with bounded capture and process-tree termination."""
    if not command or not Path(command[0]).is_absolute():
        raise OSError("pinned executable must be absolute")
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
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

    out_thread = threading.Thread(
        target=_drain_pipe, args=(process.stdout, stdout_buffer, truncated), daemon=True
    )
    err_thread = threading.Thread(
        target=_drain_pipe, args=(process.stderr, stderr_buffer, truncated), daemon=True
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
    return CommandResult(
        returncode=process.returncode,
        stdout=bytes(stdout_buffer),
        stderr=bytes(stderr_buffer),
        timed_out=timed_out,
        output_truncated=truncated.is_set(),
    )


def _drain_pipe(stream: BinaryIO, target: bytearray, truncated: threading.Event) -> None:
    while True:
        chunk = stream.read(64 * 1024)
        if not chunk:
            return
        remaining = _MAX_CAPTURE_BYTES - len(target)
        if remaining > 0:
            target.extend(chunk[:remaining])
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


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))

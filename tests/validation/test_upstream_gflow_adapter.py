from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from gflow_cli.v2_runtime.upstream_gflow_adapter import (
    AdapterRequest,
    AdapterStatus,
    CommandResult,
    ProcessLaunchError,
    UpstreamGflowVideoAdapter,
)

_MEDIA_ID = "01234567-89ab-cdef-0123-456789abcdef"
_PROJECT_ID = "aafe9282-071f-4333-baa4-92b9f76a9c27"
_SECRET_SENTINEL = "COOKIE_SENTINEL_DO_NOT_RETURN"


def _request(tmp_path: Path, **changes: Any) -> AdapterRequest:
    profile_home = tmp_path / "profile-home"
    profile_home.mkdir(parents=True, exist_ok=True)
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    values: dict[str, Any] = {
        "approved_prompt": 'A quiet night sky; $(echo "not shell")\nwith exact punctuation',
        "profile_id": "GPR_0002",
        "profile_name": "gflow-prod",
        "expected_account": "production@example.com",
        "profile_home": profile_home,
        "project_id": _PROJECT_ID,
        "model": "veo-lite",
        "aspect_ratio": "9:16",
        "duration_seconds": 8,
        "output_count": 1,
        "staging_root": staging,
        "staging_output_path": staging / "episode.mp4",
        "production_job_id": "PRD_000001",
        "scene_job_id": "SJB_000001",
        "request_idempotency_key": "UPSTREAM_V1_TEST_KEY_0001",
        "generation_authorized": True,
    }
    values.update(changes)
    return AdapterRequest(**values)


def _success_json(request: AdapterRequest, output: Path) -> bytes:
    model = {
        "omni-flash": "omni_flash",
        "veo-lite": "veo_3_1_lite",
        "veo-fast": "veo_3_1_fast",
        "veo-quality": "veo_3_1_quality",
        "veo-lite-lp": "veo_3_1_lite_lower_priority",
    }[request.model]
    aspect = {"9:16": "portrait", "16:9": "landscape"}[request.aspect_ratio]
    return json.dumps(
        {
            "status": "ok",
            "command": "video t2v",
            "media_id": _MEDIA_ID,
            "generation_status": "MEDIA_GENERATION_STATUS_SUCCESSFUL",
            "succeeded": True,
            "local_path": str(output),
            "failure_reasons": [],
            "error_message": None,
            "request": {
                "model": model,
                "mode": "t2v",
                "aspect": aspect,
                "duration": request.duration_seconds,
                "count": 1,
                "seed": None,
            },
        }
    ).encode()


class FakeRunner:
    def __init__(self, request: AdapterRequest, *, mode: str = "success") -> None:
        self.request = request
        self.mode = mode
        self.calls: list[tuple[tuple[str, ...], bytes, Path, float]] = []
        self.output = request.staging_output_path
        self.last_stdout = b""
        self.last_stderr = b""

    def __call__(
        self,
        command: tuple[str, ...],
        stdin: bytes,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> CommandResult:
        self.calls.append((command, stdin, cwd, timeout_seconds))
        if "--version" in command:
            if self.mode == "version-timeout":
                return CommandResult(None, b"", b"", timed_out=True)
            if self.mode == "version-mismatch":
                self.last_stdout = b"gflow, version 0.91.0\n"
                return CommandResult(0, self.last_stdout, b"")
            self.last_stdout = b"gflow, version 0.92.1\n"
            return CommandResult(0, self.last_stdout, b"")
        if self.mode == "timeout":
            return CommandResult(None, b"", b"", timed_out=True)
        if self.mode == "terminated":
            return CommandResult(-9, b"", b"", output_truncated=False)
        if self.mode == "truncated":
            return CommandResult(0, b"", b"", output_truncated=True)
        if self.mode == "malformed":
            return CommandResult(0, b"not-json", b"")
        if self.mode == "stderr-only":
            self.last_stdout = b""
            self.last_stderr = _SECRET_SENTINEL.encode()
            return CommandResult(1, self.last_stdout, self.last_stderr)
        if self.mode == "auth":
            return self._error("AuthExpiredError", exit_code=3)
        if self.mode == "project":
            return self._error("UiSelectorDriftError", exit_code=23)
        if self.mode == "credits":
            return self._error("InsufficientCreditsError", exit_code=37)
        if self.mode == "download":
            return self._error("MediaDownloadError", exit_code=12)
        if self.mode == "unknown":
            return self._error("SomeFutureError", exit_code=1)
        if self.mode == "agent":
            return self._error("FlowAgentUiError", exit_code=25)
        payload = json.loads(stdin.decode("utf-8"))
        assert payload["args"][0:2] == ["video", "t2v"]
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_bytes(b"synthetic-mp4-placeholder")
        body = json.loads(_success_json(self.request, self.output))
        if self.mode == "count-mismatch":
            body["request"]["count"] = 2
        if self.mode == "missing-output":
            self.output.unlink()
        self.last_stdout = json.dumps(body).encode()
        return CommandResult(0, self.last_stdout, b"")

    @staticmethod
    def _error(error_type: str, *, exit_code: int) -> CommandResult:
        return CommandResult(
            exit_code,
            json.dumps(
                {
                    "status": "fail",
                    "error": {
                        "class": error_type,
                        "exit_code": exit_code,
                        "retryable": False,
                        "detail": _SECRET_SENTINEL,
                    },
                }
            ).encode(),
            _SECRET_SENTINEL.encode(),
        )


def _adapter(request: AdapterRequest, fake: FakeRunner) -> UpstreamGflowVideoAdapter:
    return UpstreamGflowVideoAdapter(
        python_executable=Path("C:/upstream-venv/Scripts/python.exe"), runner=fake
    )


def test_unauthorized_request_does_not_spawn_version_or_generation_process(tmp_path: Path) -> None:
    request = _request(tmp_path, generation_authorized=False)
    fake = FakeRunner(request)
    result = _adapter(request, fake).generate_once(request)
    assert result.status is AdapterStatus.PRE_SUBMIT_FAILURE
    assert result.normalized_error_code == "GENERATION_NOT_AUTHORIZED"
    assert result.auto_retry is False
    assert fake.calls == []


def test_command_contains_exact_policy_and_prompt_is_not_in_process_argv(tmp_path: Path) -> None:
    request = _request(tmp_path)
    fake = FakeRunner(request)
    result = _adapter(request, fake).generate_once(request)
    generation_command, stdin, cwd, _timeout = fake.calls[1]
    payload = json.loads(stdin)
    args = payload["args"]
    assert Path(generation_command[0]) == Path("C:/upstream-venv/Scripts/python.exe")
    assert generation_command[1:2] == ("-c",)
    assert request.approved_prompt not in generation_command
    assert args[:3] == ["video", "t2v", request.approved_prompt]
    assert args[args.index("--profile") + 1] == "gflow-prod"
    assert args[args.index("--project") + 1] == _PROJECT_ID
    assert args[args.index("--model") + 1] == "veo-lite"
    assert args[args.index("--aspect") + 1] == "9:16"
    assert args[args.index("--duration") + 1] == "8"
    assert args[args.index("--count") + 1] == "1"
    assert args[args.index("--output") + 1] == str(request.staging_output_path.resolve())
    assert args[-1] == "--json"
    assert cwd == request.profile_home
    assert result.status is AdapterStatus.UPSTREAM_GENERATION_COMPLETED


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"project_id": ""}, "canonical existing Flow project UUID"),
        ({"project_id": "not-a-uuid"}, "canonical existing Flow project UUID"),
        ({"output_count": 2}, "exactly one output"),
        ({"model": "future-model"}, "model is not supported"),
        ({"aspect_ratio": "1:1"}, "aspect ratio is not supported"),
        ({"model": "veo-lite", "duration_seconds": 10}, "only for omni-flash"),
        ({"timeout_seconds": 20}, "between 30 and 1,800 seconds"),
    ],
)
def test_invalid_request_does_not_invoke_upstream(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    request = _request(tmp_path, **changes)
    fake = FakeRunner(request)
    result = _adapter(request, fake).generate_once(request)
    assert result.status is AdapterStatus.PRE_SUBMIT_FAILURE
    assert result.safe_message
    assert message in result.remediation
    assert fake.calls == []


def test_staging_escape_and_existing_output_are_rejected(tmp_path: Path) -> None:
    request = _request(tmp_path, staging_output_path=tmp_path / "outside.mp4")
    fake = FakeRunner(request)
    result = _adapter(request, fake).generate_once(request)
    assert result.status is AdapterStatus.PRE_SUBMIT_FAILURE
    assert fake.calls == []

    request2 = _request(tmp_path / "second")
    request2.staging_output_path.write_bytes(b"old")
    fake2 = FakeRunner(request2)
    result2 = _adapter(request2, fake2).generate_once(request2)
    assert result2.status is AdapterStatus.PRE_SUBMIT_FAILURE
    assert fake2.calls == []


def test_success_requires_strict_json_and_returns_hashes_not_production_proof(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    fake = FakeRunner(request)
    result = _adapter(request, fake).generate_once(request)
    assert result.status is AdapterStatus.UPSTREAM_GENERATION_COMPLETED
    assert result.media_id == _MEDIA_ID
    assert result.local_output_path == request.staging_output_path
    assert result.requested_project_id == _PROJECT_ID
    assert result.project_binding_source == "REQUEST_ARGUMENT_ONLY"
    assert result.upstream_operation_id is None
    assert result.drive_file_id is None
    assert result.d2_run_id is None
    assert result.auto_retry is False
    normalized = result.to_dict()
    assert normalized["status"] == "UPSTREAM_GENERATION_COMPLETED"
    assert "approved_prompt" not in normalized
    assert "expected_account" not in normalized
    assert result.stdout_digest == hashlib.sha256(fake.last_stdout).hexdigest()
    assert len(result.stderr_digest) == 64


@pytest.mark.parametrize(
    ("mode", "expected", "may_have_submitted"),
    [
        ("auth", AdapterStatus.AUTH_FAILURE, False),
        ("project", AdapterStatus.PROJECT_NAVIGATION_FAILURE, True),
        ("credits", AdapterStatus.INSUFFICIENT_CREDITS, False),
        ("download", AdapterStatus.DOWNLOAD_FAILURE_AFTER_GENERATION, True),
        ("unknown", AdapterStatus.POST_SUBMIT_UNKNOWN, True),
        ("agent", AdapterStatus.POST_SUBMIT_UNKNOWN, True),
        ("malformed", AdapterStatus.POST_SUBMIT_UNKNOWN, True),
        ("stderr-only", AdapterStatus.POST_SUBMIT_UNKNOWN, True),
        ("terminated", AdapterStatus.POST_SUBMIT_UNKNOWN, True),
        ("truncated", AdapterStatus.POST_SUBMIT_UNKNOWN, True),
    ],
)
def test_upstream_error_normalization_is_conservative(
    tmp_path: Path,
    mode: str,
    expected: AdapterStatus,
    may_have_submitted: bool,
) -> None:
    request = _request(tmp_path)
    fake = FakeRunner(request, mode=mode)
    result = _adapter(request, fake).generate_once(request)
    assert result.status is expected
    assert result.submit_may_have_occurred is may_have_submitted
    assert result.auto_retry is False
    assert _SECRET_SENTINEL not in result.safe_message
    assert _SECRET_SENTINEL not in result.remediation
    assert _SECRET_SENTINEL not in str(result)


def test_timeout_and_count_mismatch_do_not_retry(tmp_path: Path) -> None:
    request = _request(tmp_path)
    timeout_runner = FakeRunner(request, mode="timeout")
    timed_out = _adapter(request, timeout_runner).generate_once(request)
    assert timed_out.status is AdapterStatus.POST_SUBMIT_UNKNOWN
    assert timed_out.auto_retry is False
    assert timed_out.submit_may_have_occurred is True

    other = _request(tmp_path / "count")
    mismatch_runner = FakeRunner(other, mode="count-mismatch")
    mismatch = _adapter(other, mismatch_runner).generate_once(other)
    assert mismatch.status is AdapterStatus.POST_SUBMIT_UNKNOWN
    assert mismatch.auto_retry is False


def test_duplicate_caller_key_never_dispatches_generation_twice(tmp_path: Path) -> None:
    request = _request(tmp_path)
    fake = FakeRunner(request)
    adapter = _adapter(request, fake)
    first = adapter.generate_once(request)
    second = adapter.generate_once(request)
    assert first.status is AdapterStatus.UPSTREAM_GENERATION_COMPLETED
    assert second.normalized_error_code == "DUPLICATE_INVOCATION"
    assert sum("-c" in call[0] for call in fake.calls) == 1


def test_pinned_version_mismatch_stops_before_generation(tmp_path: Path) -> None:
    request = _request(tmp_path)
    fake = FakeRunner(request, mode="version-mismatch")
    result = _adapter(request, fake).generate_once(request)
    assert result.status is AdapterStatus.PRE_SUBMIT_FAILURE
    assert result.normalized_error_code == "UPSTREAM_VERSION_MISMATCH"
    assert len(fake.calls) == 1


def test_pre_submit_version_timeout_does_not_run_generation(tmp_path: Path) -> None:
    request = _request(tmp_path)
    fake = FakeRunner(request, mode="version-timeout")
    result = _adapter(request, fake).generate_once(request)
    assert result.status is AdapterStatus.PRE_SUBMIT_FAILURE
    assert result.submit_may_have_occurred is False
    assert len(fake.calls) == 1


def test_process_launch_failure_is_proven_pre_submit(tmp_path: Path) -> None:
    request = _request(tmp_path)
    calls = 0

    def runner(
        command: tuple[str, ...],
        stdin: bytes,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> CommandResult:
        nonlocal calls
        calls += 1
        if "--version" in command:
            return CommandResult(0, b"gflow, version 0.92.1\n", b"")
        raise ProcessLaunchError("not launched")

    result = UpstreamGflowVideoAdapter(
        python_executable=Path("C:/upstream-venv/Scripts/python.exe"), runner=runner
    ).generate_once(request)
    assert result.status is AdapterStatus.PRE_SUBMIT_FAILURE
    assert result.submit_may_have_occurred is False
    assert calls == 2


def test_non_json_failure_digests_cannot_leak_raw_stderr(tmp_path: Path) -> None:
    request = _request(tmp_path)
    fake = FakeRunner(request, mode="stderr-only")
    result = _adapter(request, fake).generate_once(request)
    assert result.stderr_digest == hashlib.sha256(fake.last_stderr).hexdigest()
    assert _SECRET_SENTINEL not in str(result)

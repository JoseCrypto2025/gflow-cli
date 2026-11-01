"""BDD binding for the offline-only upstream video adapter contract."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, scenarios, then, when

from gflow_cli.v2_runtime.upstream_gflow_adapter import (
    AdapterRequest,
    AdapterStatus,
    CommandResult,
    UpstreamGflowVideoAdapter,
)

scenarios("upstream_first_adapter_v1.feature")


@pytest.fixture
def adapter_state(tmp_path: Path) -> dict[str, Any]:
    state: dict[str, Any] = {"calls": [], "result": None, "mode": "success", "tmp": tmp_path}

    def runner(
        command: tuple[str, ...],
        stdin: bytes,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> CommandResult:
        state["calls"].append((command, stdin, timeout_seconds))
        if "--version" in command:
            return CommandResult(0, b"gflow, version 0.92.1\n", b"")
        if state["mode"] == "timeout":
            return CommandResult(None, b"", b"", timed_out=True)
        output = tmp_path / "staging" / "episode.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"not-a-validated-production-video")
        import json

        return CommandResult(
            0,
            json.dumps(
                {
                    "status": "ok",
                    "command": "video t2v",
                    "media_id": "01234567-89ab-cdef-0123-456789abcdef",
                    "generation_status": "MEDIA_GENERATION_STATUS_SUCCESSFUL",
                    "succeeded": True,
                    "local_path": str(output),
                    "failure_reasons": [],
                    "error_message": None,
                    "request": {
                        "model": "veo_3_1_lite",
                        "mode": "t2v",
                        "aspect": "portrait",
                        "duration": 8,
                        "count": 1,
                        "seed": None,
                    },
                }
            ).encode(),
            b"",
        )

    state["runner"] = runner
    return state


def _request(state: dict[str, Any], *, authorized: bool) -> AdapterRequest:
    root = state["tmp"] / "staging"
    root.mkdir(parents=True, exist_ok=True)
    home = state["tmp"] / "gflow-home"
    home.mkdir(parents=True, exist_ok=True)
    return AdapterRequest(
        approved_prompt='A quiet night sky; $(echo "not shell")',
        profile_id="GPR_0002",
        profile_name="gflow-prod",
        expected_account="production@example.com",
        profile_home=home,
        project_id="aafe9282-071f-4333-baa4-92b9f76a9c27",
        model="veo-lite",
        aspect_ratio="9:16",
        duration_seconds=8,
        output_count=1,
        staging_root=root,
        staging_output_path=root / "episode.mp4",
        production_job_id="PRD_000001",
        scene_job_id="SJB_000001",
        request_idempotency_key="UPSTREAM_V1_TEST_KEY_0001",
        generation_authorized=authorized,
    )


@given("an unauthorized upstream video request")
def unauthorized_request(adapter_state: dict[str, Any]) -> None:
    adapter_state["request"] = _request(adapter_state, authorized=False)


@given("an authorized upstream video request")
def authorized_request(adapter_state: dict[str, Any]) -> None:
    adapter_state["request"] = _request(adapter_state, authorized=True)


@given("the upstream command runner times out after launch")
def runner_times_out(adapter_state: dict[str, Any]) -> None:
    adapter_state["mode"] = "timeout"


@given("the upstream command runner returns a completed staged MP4")
def runner_succeeds(adapter_state: dict[str, Any]) -> None:
    adapter_state["mode"] = "success"


@when("the adapter generates once")
def generate_once(adapter_state: dict[str, Any]) -> None:
    adapter = UpstreamGflowVideoAdapter(
        python_executable=Path(sys.executable),
        pinned_version="0.92.1",
        runner=adapter_state["runner"],
    )
    adapter_state["adapter"] = adapter
    adapter_state["result"] = adapter.generate_once(adapter_state["request"])


@then("the adapter reports a pre-submit failure")
def pre_submit_failure(adapter_state: dict[str, Any]) -> None:
    assert adapter_state["result"].status is AdapterStatus.PRE_SUBMIT_FAILURE


@then("the upstream command runner was not called")
def runner_not_called(adapter_state: dict[str, Any]) -> None:
    assert adapter_state["calls"] == []


@then("the adapter reports post-submit unknown")
def post_submit_unknown(adapter_state: dict[str, Any]) -> None:
    assert adapter_state["result"].status is AdapterStatus.POST_SUBMIT_UNKNOWN


@then("automatic retry is disabled")
def no_automatic_retry(adapter_state: dict[str, Any]) -> None:
    assert adapter_state["result"].auto_retry is False


@then("the adapter reports upstream generation completed")
def upstream_generation_completed(adapter_state: dict[str, Any]) -> None:
    assert adapter_state["result"].status is AdapterStatus.UPSTREAM_GENERATION_COMPLETED


@then("the result does not claim production finalization")
def no_production_finalization(adapter_state: dict[str, Any]) -> None:
    result = adapter_state["result"]
    assert result.drive_file_id is None
    assert result.d2_run_id is None

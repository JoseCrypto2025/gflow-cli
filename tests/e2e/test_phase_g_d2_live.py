"""One-shot Phase G live test, deliberately opt-in and credit-consuming.

After pushing the implementation, export a fresh read-only Master snapshot to
JSON and set the Phase G environment variables below. The test uses only the
repository-owned ``gflow-v2-test`` profile and performs at most one Generate
action. Drive upload and hash verification remain explicit operator steps.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from gflow_cli.v2_runtime.phase_g import D2RetryAuthorization
from gflow_cli.v2_runtime.phase_g_run import (
    build_phase_g_request_from_snapshot,
    execute_and_validate,
    run_fresh_presubmit,
)

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_video]

_RUN_CONFIRMATION = "GFLOW_CLI_E2E_RUN_PHASE_G"
_SNAPSHOT = "GFLOW_CLI_E2E_PHASE_G_SNAPSHOT"
_ACCOUNT_EMAIL = "GFLOW_CLI_E2E_PHASE_G_ACCOUNT_EMAIL"
_STATE_ROOT = "GFLOW_CLI_E2E_PHASE_G_STATE_ROOT"
_TEMP_ROOT = "GFLOW_CLI_E2E_PHASE_G_TEMP_ROOT"
_RETRY_PREDECESSOR = "GFLOW_CLI_E2E_PHASE_G_RETRY_PREDECESSOR"
_RETRY_EXPECTED_STATE = "GFLOW_CLI_E2E_PHASE_G_RETRY_EXPECTED_STATE"


@pytest.mark.asyncio
async def test_phase_g_one_approved_test_clip(e2e_profile_dir: Path) -> None:
    """Run one exact approved D2 request and validate its attributable MP4."""
    if os.environ.get(_RUN_CONFIRMATION) != "I_CONFIRM_ONE_D2_GENERATION":
        pytest.skip(f"set {_RUN_CONFIRMATION}=I_CONFIRM_ONE_D2_GENERATION to opt in")
    if os.environ.get("GFLOW_CLI_E2E_PROFILE", "").strip() != "gflow-v2-test":
        pytest.skip("Phase G accepts only the dedicated gflow-v2-test profile")

    snapshot_path = _required_path(_SNAPSHOT)
    state_root = _required_path(_STATE_ROOT)
    temp_root = _required_path(_TEMP_ROOT)
    expected_email = os.environ.get(_ACCOUNT_EMAIL, "").strip()
    if not expected_email or "@" not in expected_email:
        pytest.skip(f"set {_ACCOUNT_EMAIL} to the verified TEST account identity")
    retry_predecessor = os.environ.get(_RETRY_PREDECESSOR, "").strip()
    retry_expected_state = os.environ.get(_RETRY_EXPECTED_STATE, "").strip()
    if bool(retry_predecessor) != bool(retry_expected_state):
        pytest.fail("retry authorization requires both predecessor and expected-state variables")
    retry_authorization = (
        D2RetryAuthorization(retry_predecessor, retry_expected_state) if retry_predecessor else None
    )

    request = build_phase_g_request_from_snapshot(
        snapshot_path,
        d2_run_id=f"D2_{uuid4().hex[:16]}",
        runtime_state_root=state_root,
        temp_root=temp_root,
    )
    await run_fresh_presubmit(request, expected_email=expected_email)
    run = await execute_and_validate(
        request,
        expected_email=expected_email,
        retry_authorization=retry_authorization,
    )
    assert run.result.submission_state == "GENERATION_SUCCEEDED"
    assert run.result.provider_media_id
    assert run.result.provider_project_id == request.provider_project_id
    assert run.video.duration_sec == pytest.approx(request.duration_sec, abs=0.05)
    assert run.video.aspect_ratio == request.aspect_ratio
    assert run.local_file.is_file()

    ledger_path = state_root / "phase-g" / "d2-credit-ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    attempt = next(row for row in ledger["attempts"] if row["d2_run_id"] == request.d2_run_id)
    assert attempt["generate_click_dispatched"] is True
    assert attempt["submission_surface"] == "AGENT_ONLY"
    assert attempt["agent_turn_dispatched"] is True
    assert attempt["provider_directive_sha256"]
    if attempt["provider_submit_request_count"]:
        assert attempt["provider_submit_rpc_observed"] is True
        assert attempt["provider_submit_rpc_id"] == "YhhmEf"
        assert attempt["provider_submit_http_method"] == "POST"
        assert attempt["provider_submit_project_id"] == request.provider_project_id
    else:
        assert attempt["provider_submit_rpc_id"] is None
    assert attempt["provider_submit_request_count"] <= 1

    evidence_path = state_root / "phase-g-live-result.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        json.dumps(
            {
                "d2_run_id": request.d2_run_id,
                "scene_prompt_id": request.scene_prompt_id,
                "provider_project_id": request.provider_project_id,
                "provider_execution_id": run.result.provider_execution_id,
                "provider_media_id": run.result.provider_media_id,
                "local_file": str(run.local_file),
                "size_bytes": run.video.size_bytes,
                "sha256": run.video.sha256,
                "duration_sec": run.video.duration_sec,
                "width": run.video.width,
                "height": run.video.height,
                "audio_present": run.video.audio_present,
                "full_decode": True,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _required_path(name: str) -> Path:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.skip(f"set {name} for the explicitly authorized Phase G run")
    return Path(value).expanduser().resolve()

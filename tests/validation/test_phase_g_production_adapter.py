from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gflow_cli.v2_runtime import phase_g_production_adapter as adapter_module
from gflow_cli.v2_runtime.production_jobs import SceneJob
from gflow_cli.v2_runtime.production_orchestrator import GeneratedSceneAsset
from gflow_cli.v2_runtime.workspace import JobWorkspace


def _context(tmp_path: Path) -> tuple[object, object, object, JobWorkspace]:
    generated = tmp_path / "generated"
    generated.mkdir()
    workspace = JobWorkspace(
        job_id="SCJ_000001",
        root=tmp_path,
        directories={"generated": generated},
    )
    request = SimpleNamespace(
        scene_id="SCN_000001",
        scene_prompt_id="SPV_000007",
        generation_profile_id="GPR_0002",
        provider_account_id="PVA_0002",
        provider_project_id="project-1",
        prompt_sha256="a" * 64,
        d2_run_id="D2_12345678",
        output_count=1,
        presubmit_request=SimpleNamespace(temp_root=generated),
    )
    preflight = SimpleNamespace(
        status="PREFLIGHT_PASS",
        ready_for_provider_adapter=True,
        scene_id="SCN_000001",
        generation_profile_id="GPR_0002",
        prompt_sha256="a" * 64,
        resolved={"provider_account_id": "PVA_0002"},
    )
    scene_job = SceneJob(
        scene_job_id="SCJ_000001",
        production_job_id="PRD_000001",
        scene_prompt_id="SPV_000007",
        attempt_no=1,
        job_status="RUNNING",
    )
    return request, preflight, scene_job, workspace


def test_phase_g_adapter_returns_exact_validated_attempt_without_rebinding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, preflight, scene_job, workspace = _context(tmp_path)
    video_path = workspace.directories["generated"] / "result.mp4"
    video_path.write_bytes(b"validated fixture")
    phase_g_run = SimpleNamespace(
        request=request,
        result=SimpleNamespace(
            d2_run_id=request.d2_run_id,
            submission_state="GENERATION_SUCCEEDED",
            provider_project_id=request.provider_project_id,
            provider_execution_id="execution-1",
            provider_media_id="media-1",
        ),
        local_file=video_path,
    )
    execute = AsyncMock(return_value=phase_g_run)
    monkeypatch.setattr(adapter_module, "execute_and_validate", execute)
    generator = adapter_module.PhaseGSceneGenerationAdapter(
        request, expected_email="operator@example.test", workspace=workspace
    )

    result = generator.generate_once(scene_job, preflight)

    assert result == GeneratedSceneAsset(
        video_path,
        "PVA_0002",
        "execution-1",
        "media-1",
        "D2_12345678",
    )
    execute.assert_awaited_once_with(request, expected_email="operator@example.test")


@pytest.mark.parametrize(
    ("request_field", "request_value"),
    [
        ("scene_prompt_id", "SPV_000008"),
        ("prompt_sha256", "b" * 64),
        ("provider_account_id", "PVA_9999"),
        ("output_count", 2),
    ],
)
def test_phase_g_adapter_fails_closed_before_execution_on_mismatched_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_field: str,
    request_value: object,
) -> None:
    request, preflight, scene_job, workspace = _context(tmp_path)
    setattr(request, request_field, request_value)
    execute = AsyncMock(side_effect=AssertionError("provider execution must not start"))
    monkeypatch.setattr(adapter_module, "execute_and_validate", execute)
    generator = adapter_module.PhaseGSceneGenerationAdapter(
        request, expected_email="operator@example.test", workspace=workspace
    )

    with pytest.raises(ValueError, match="Phase G request differs"):
        generator.generate_once(scene_job, preflight)

    execute.assert_not_awaited()


def test_phase_g_adapter_refuses_nested_event_loop_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, preflight, scene_job, workspace = _context(tmp_path)
    execute = AsyncMock(side_effect=AssertionError("provider execution must not start"))
    monkeypatch.setattr(adapter_module, "execute_and_validate", execute)
    generator = adapter_module.PhaseGSceneGenerationAdapter(
        request, expected_email="operator@example.test", workspace=workspace
    )

    async def invoke() -> None:
        with pytest.raises(RuntimeError, match="synchronous orchestration"):
            generator.generate_once(scene_job, preflight)

    asyncio.run(invoke())
    execute.assert_not_awaited()

"""Bind the existing one-shot Phase G executor to production Scene Jobs."""

from __future__ import annotations

import asyncio
from pathlib import Path

from .phase_e import PreflightResult
from .phase_g import PhaseGExecutionRequest
from .phase_g_run import PhaseGD2Run, execute_and_validate, recover_and_validate_existing
from .production_jobs import SceneJob
from .production_orchestrator import GeneratedSceneAsset
from .workspace import JobWorkspace


class PhaseGSceneGenerationAdapter:
    """Execute exactly the already-pinned Phase G request for one claimed job."""

    def __init__(
        self,
        request: PhaseGExecutionRequest,
        *,
        expected_email: str,
        workspace: JobWorkspace,
    ) -> None:
        if not expected_email.strip():
            raise ValueError("expected authenticated account email is required")
        generated_root = workspace.directories.get("generated")
        if generated_root is None:
            raise ValueError("job workspace has no generated-media directory")
        try:
            expected_root = Path(generated_root).resolve(strict=True)
            request_root = request.presubmit_request.temp_root.resolve(strict=True)
        except OSError as exc:
            raise ValueError("Phase G and Scene Job workspace roots must exist") from exc
        if request_root != expected_root:
            raise ValueError("Phase G temp root must be the claimed workspace generated directory")
        self._request = request
        self._expected_email = expected_email
        self._workspace = workspace

    def generate_once(self, scene_job: SceneJob, preflight: PreflightResult) -> GeneratedSceneAsset:
        """Run Phase G only after exact request, preflight, and job binding checks."""
        self._validate_binding(scene_job, preflight)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("Phase G production adapter requires synchronous orchestration")

        result = asyncio.run(
            execute_and_validate(self._request, expected_email=self._expected_email)
        )
        return self._asset_from_result(result)

    def recover_existing(
        self, scene_job: SceneJob, preflight: PreflightResult
    ) -> GeneratedSceneAsset:
        """Recover the same deterministic Scene Job attempt without submitting again."""
        self._validate_binding(scene_job, preflight)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("Phase G recovery requires synchronous orchestration")
        result = asyncio.run(
            recover_and_validate_existing(self._request, expected_email=self._expected_email)
        )
        return self._asset_from_result(result)

    def _validate_binding(self, scene_job: SceneJob, preflight: PreflightResult) -> None:
        if (
            self._workspace.job_id != scene_job.scene_job_id
            or self._request.scene_prompt_id != scene_job.scene_prompt_id
            or self._request.scene_id != preflight.scene_id
            or self._request.generation_profile_id != preflight.generation_profile_id
            or self._request.prompt_sha256 != preflight.prompt_sha256
            or self._request.provider_account_id != preflight.resolved.get("provider_account_id")
            or self._request.output_count != 1
            or preflight.status != "PREFLIGHT_PASS"
            or not preflight.ready_for_provider_adapter
        ):
            raise ValueError("Phase G request differs from claimed Scene Job or fresh Phase E")

    def _asset_from_result(self, result: PhaseGD2Run) -> GeneratedSceneAsset:
        self._validate_result(result)
        execution_id = result.result.provider_execution_id
        media_id = result.result.provider_media_id
        if not execution_id or not media_id:
            raise ValueError("Phase G success must include exact provider execution and media IDs")
        return GeneratedSceneAsset(
            video_path=result.local_file,
            provider_account_id=self._request.provider_account_id,
            provider_execution_id=execution_id,
            provider_media_id=media_id,
            d2_run_id=self._request.d2_run_id,
        )

    def _validate_result(self, result: PhaseGD2Run) -> None:
        if (
            result.request != self._request
            or result.result.d2_run_id != self._request.d2_run_id
            or result.result.submission_state != "GENERATION_SUCCEEDED"
            or result.result.provider_project_id != self._request.provider_project_id
        ):
            raise ValueError("Phase G result does not prove this exact successful attempt")
        try:
            local_file = result.local_file.resolve(strict=True)
            generated_root = self._workspace.directories["generated"].resolve(strict=True)
            local_file.relative_to(generated_root)
        except (OSError, KeyError, ValueError) as exc:
            raise ValueError("Phase G artifact is outside the claimed workspace") from exc
        if local_file.is_symlink() or not local_file.is_file():
            raise ValueError("Phase G artifact is not a regular local video file")

"""Single-scene production lifecycle composition over existing V2 contracts."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from .audio_intent import AudioIntent, resolve_audio_intent
from .finalize_scene import (
    FinalOutputDrivePort,
    FinalOutputFinalization,
    FinalOutputStore,
    finalize_merged_output,
    finalize_no_audio,
)
from .media_merge import MergeResult
from .phase_e import PreflightResult
from .phase_g_artifacts import validate_video
from .production_jobs import (
    JobClaim,
    JobConflictError,
    ProductionJob,
    ProductionJobCoordinator,
    ProductionJobStore,
    ProductionRequest,
    SceneJob,
    validate_final_output_evidence,
)
from .workspace import JobWorkspace, cleanup_workspace, validate_workspace

if TYPE_CHECKING:
    from .phase_g import PhaseGExecutionRequest


class ProductionOrchestrationError(RuntimeError):
    """A production job cannot advance without exact validated evidence."""


@dataclass(frozen=True, slots=True)
class GeneratedSceneAsset:
    """One generated Flow asset returned by the existing Phase G adapter."""

    video_path: Path
    provider_account_id: str
    provider_execution_id: str
    provider_media_id: str
    d2_run_id: str

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.provider_account_id,
                self.provider_execution_id,
                self.provider_media_id,
                self.d2_run_id,
            )
        ):
            raise ValueError("exact provider account, execution, and media identities are required")


class SceneGenerationPort(Protocol):
    def generate_once(
        self, scene_job: SceneJob, preflight: PreflightResult
    ) -> GeneratedSceneAsset: ...


class PhaseGRequestFactory(Protocol):
    """Build a Phase G request after the production Scene Job is claimed."""

    def __call__(self, scene_job: SceneJob, d2_run_id: str) -> PhaseGExecutionRequest: ...


def scene_phase_g_run_id(scene_job: SceneJob) -> str:
    """Return the stable D2 identity bound to this Scene Job attempt."""
    if (
        not re.fullmatch(r"SJB_[0-9]{6}", scene_job.scene_job_id)
        or isinstance(scene_job.attempt_no, bool)
        or scene_job.attempt_no < 1
    ):
        raise ValueError("Scene Job identity and positive attempt number are required")
    value = f"PRODUCTION_SCENE_D2_V1:{scene_job.scene_job_id}:{scene_job.attempt_no}"
    return f"D2_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:32]}"


class SpeechFinalizationPort(Protocol):
    def merge_approved_dialogue(
        self,
        scene_job: SceneJob,
        generated_asset: GeneratedSceneAsset,
        scene: Mapping[str, object],
        dialogue_rows: tuple[Mapping[str, object], ...],
        workspace: JobWorkspace,
        *,
        expected_duration_sec: float,
        expected_aspect_ratio: str,
        occurred_at: str,
    ) -> MergeResult: ...


@dataclass(frozen=True, slots=True)
class ProductionOrchestrationResult:
    claim: JobClaim
    scene_job: SceneJob
    final_output: FinalOutputFinalization | None
    audio_intent: AudioIntent
    tts_job_id: str


class ProductionOrchestrator:
    """Own claim, one-shot generation, audio routing, finalization, and completion."""

    def __init__(
        self,
        jobs: ProductionJobStore,
        final_outputs: FinalOutputStore,
        drive: FinalOutputDrivePort,
        *,
        temp_root: Path,
    ) -> None:
        self._jobs = jobs
        self._coordinator = ProductionJobCoordinator(jobs)
        self._final_outputs = final_outputs
        self._drive = drive
        self._temp_root = temp_root

    def run_scene_once(
        self,
        request: ProductionRequest,
        *,
        scene: Mapping[str, object],
        approved_prompt: Mapping[str, object],
        preflight: PreflightResult,
        dialogue_rows: Sequence[Mapping[str, object]],
        expected_duration_sec: float,
        expected_aspect_ratio: str,
        workspace: JobWorkspace,
        workspace_created_at: datetime,
        verification_temp_dir: Path,
        occurred_at: str,
        generator: SceneGenerationPort | None = None,
        phase_g_request_factory: PhaseGRequestFactory | None = None,
        expected_email: str | None = None,
        speech: SpeechFinalizationPort | None = None,
    ) -> ProductionOrchestrationResult:
        """Execute or resume one scene without repeating ambiguous provider work."""
        prompt_id = approved_prompt.get("Scene_Prompt_ID")
        scene_id = scene.get("Scene_ID")
        if (
            len(request.scene_prompt_ids) != 1
            or not isinstance(prompt_id, str)
            or request.scene_prompt_ids != (prompt_id,)
            or not isinstance(scene_id, str)
            or approved_prompt.get("Scene_ID") != scene_id
            or approved_prompt.get("Prompt_Status") != "APPROVED"
            or approved_prompt.get("Generation_Profile_ID") != preflight.generation_profile_id
            or approved_prompt.get("Prompt_SHA256") != preflight.prompt_sha256
            or not isinstance(preflight.resolved.get("provider_account_id"), str)
            or scene.get("Scene_Definition_Status") != "LOCKED"
            or preflight.status != "PREFLIGHT_PASS"
            or not preflight.ready_for_provider_adapter
            or preflight.scene_id != scene_id
        ):
            raise ProductionOrchestrationError(
                "one locked scene, its approved prompt, and passing Phase E evidence are required"
            )

        if phase_g_request_factory is not None:
            if generator is not None or expected_email is None:
                raise ProductionOrchestrationError(
                    "provide either one generation port or one Phase G request factory and identity"
                )
        elif generator is None or expected_email is not None:
            raise ProductionOrchestrationError(
                "one generation port or one Phase G request factory and identity is required"
            )
        if request.execution_environment == "PROD" and phase_g_request_factory is None:
            raise ProductionOrchestrationError(
                "PROD generation requires the bound Phase G evidence adapter"
            )

        claim = self._coordinator.submit_request(request)
        if len(claim.scene_jobs) != 1 or claim.scene_jobs[0].scene_prompt_id != prompt_id:
            raise ProductionOrchestrationError("Production Job claim does not match one scene")
        job = claim.scene_jobs[0]
        if job.job_status == "COMPLETED":
            output = self._find_final_output(claim.production_job.production_job_id)
            if output is None:
                raise ProductionOrchestrationError(
                    "completed Scene Job has no durable FINAL_OUTPUT; hold required"
                )
            if claim.production_job.job_status == "RUNNING":
                production = self._jobs.complete_production(
                    claim.production_job.production_job_id, output, occurred_at
                )
                claim = JobClaim(production, claim.scene_jobs, claim.replayed)
            elif claim.production_job.job_status != "COMPLETED":
                raise ProductionOrchestrationError(
                    "completed Scene Job has an unresolved Production Job state"
                )
            return ProductionOrchestrationResult(
                claim, job, None, resolve_audio_intent(scene, dialogue_rows), "NOT_REQUIRED"
            )
        if job.job_status == "REMOTE_VERIFYING":
            intent = resolve_audio_intent(scene, dialogue_rows)
            if intent is not AudioIntent.NO_AUDIO:
                raise ProductionOrchestrationError(
                    "speech cleanup recovery requires its persisted TTS evidence"
                )
            return recover_verified_scene_cleanup(
                self._jobs,
                self._final_outputs,
                production_job=claim.production_job,
                scene_job=job,
                temp_root=self._temp_root,
                workspace_id=workspace.job_id,
                workspace_created_at=workspace_created_at,
                occurred_at=occurred_at,
                audio_intent=intent,
                tts_job_id="NOT_REQUIRED",
            )
        if workspace.job_id != job.scene_job_id:
            raise ProductionOrchestrationError(
                "job workspace must be bound to the claimed Scene Job before execution"
            )
        workspace_paths = validate_workspace(self._temp_root, workspace)
        phase_g_adapter = None
        if phase_g_request_factory is not None:
            d2_run_id = scene_phase_g_run_id(job)
            phase_g_request = phase_g_request_factory(job, d2_run_id)
            context = phase_g_request.execution_context
            if context is not None and request.execution_environment == "PROD":
                if context.production_job_id not in {
                    None,
                    claim.production_job.production_job_id,
                } or context.scene_job_id not in {None, job.scene_job_id}:
                    raise ProductionOrchestrationError(
                        "PROD execution context names a different production or scene job"
                    )
                context = replace(
                    context,
                    production_job_id=claim.production_job.production_job_id,
                    scene_job_id=job.scene_job_id,
                )
                phase_g_request = replace(phase_g_request, execution_context=context)
            generated_root = workspace.directories.get("generated")
            if (
                phase_g_request.d2_run_id != d2_run_id
                or context is None
                or context.environment != request.execution_environment
                or context.attempt_id != d2_run_id
                or context.scene_id != scene_id
                or context.scene_prompt_id != prompt_id
                or context.generation_profile_id != preflight.generation_profile_id
                or context.provider_account_id != phase_g_request.provider_account_id
                or context.provider_project_id != phase_g_request.provider_project_id
                or request.execution_environment not in {"TEST", "PROD"}
                or phase_g_request.scene_id != scene_id
                or phase_g_request.scene_prompt_id != prompt_id
                or phase_g_request.generation_profile_id != preflight.generation_profile_id
                or phase_g_request.provider_account_id
                != preflight.resolved.get("provider_account_id")
                or phase_g_request.prompt_sha256 != preflight.prompt_sha256
                or phase_g_request.compiled_prompt_text != preflight.compiled_prompt
                or phase_g_request.compiled_negative_prompt
                != (preflight.compiled_negative_prompt or "")
                or phase_g_request.output_count != 1
                or phase_g_request.duration_sec != expected_duration_sec
                or phase_g_request.aspect_ratio != expected_aspect_ratio
                or generated_root is None
                or phase_g_request.presubmit_request.temp_root.resolve() != generated_root.resolve()
            ):
                raise ProductionOrchestrationError(
                    "bound Phase G request differs from the claimed Scene, Phase E, or workspace"
                )
            from .phase_g_production_adapter import PhaseGSceneGenerationAdapter

            phase_g_adapter = PhaseGSceneGenerationAdapter(
                phase_g_request, expected_email=expected_email or "", workspace=workspace
            )
            generator = phase_g_adapter
        if generator is None:
            raise ProductionOrchestrationError("generation adapter was not constructed")
        if job.job_status == "PENDING":
            claim = self._start_production(claim, occurred_at)
            job = self._jobs.transition_scene(
                job.scene_job_id, "PENDING", "VALIDATING", occurred_at
            )
            job = self._jobs.transition_scene(job.scene_job_id, "VALIDATING", "QUEUED", occurred_at)
            try:
                generated = generator.generate_once(job, preflight)
            except Exception:
                self._hold_before_execution_proof(claim, job, occurred_at)
                raise
            job = self._jobs.transition_scene(job.scene_job_id, "QUEUED", "RUNNING", occurred_at)
            claim = replace(
                claim,
                production_job=self._start_running_production(claim.production_job, occurred_at),
            )
        elif job.job_status in {"RUNNING", "UPLOADING"}:
            if phase_g_adapter is not None:
                generated = phase_g_adapter.recover_existing(job, preflight)
            else:
                raise ProductionOrchestrationError(
                    f"{job.job_status} Scene Job requires the bound Phase G recovery adapter; "
                    "Generate is blocked"
                )
        elif job.job_status == "HOLD":
            if phase_g_adapter is None:
                raise ProductionOrchestrationError(
                    "HOLD Scene Job requires the bound Phase G recovery adapter; "
                    "Generate is blocked"
                )
            generated = phase_g_adapter.recover_existing(job, preflight)
            job = self._jobs.transition_scene(job.scene_job_id, "HOLD", "RUNNING", occurred_at)
            claim = replace(
                claim,
                production_job=self._start_running_production(claim.production_job, occurred_at),
            )
        else:
            raise ProductionOrchestrationError(
                f"Scene Job state {job.job_status} requires explicit recovery evidence"
            )

        try:
            video_path = generated.video_path.resolve(strict=True)
            video_path.relative_to(workspace_paths["generated"])
        except (OSError, ValueError) as exc:
            raise ProductionOrchestrationError(
                "generated asset must be inside the validated job workspace"
            ) from exc
        validate_video(
            video_path,
            expected_duration_sec=expected_duration_sec,
            expected_aspect_ratio=expected_aspect_ratio,
        )
        if generated.provider_account_id != preflight.resolved.get("provider_account_id"):
            raise ProductionOrchestrationError(
                "generated asset provider account differs from passing Phase E evidence"
            )
        intent = resolve_audio_intent(scene, dialogue_rows)

        if intent is AudioIntent.NO_AUDIO:
            if job.job_status != "UPLOADING":
                job = self._jobs.transition_scene(
                    job.scene_job_id, "RUNNING", "UPLOADING", occurred_at
                )
            finalization = finalize_no_audio(
                self._final_outputs,
                self._drive,
                production_job_id=claim.production_job.production_job_id,
                scene_prompt_id=str(prompt_id),
                generation_profile_id=preflight.generation_profile_id,
                d2_run_id=generated.d2_run_id,
                provider_account_id=generated.provider_account_id,
                provider_execution_id=generated.provider_execution_id,
                provider_media_id=generated.provider_media_id,
                scene=dict(scene),
                dialogue_rows=tuple(dict(row) for row in dialogue_rows),
                video_path=video_path,
                expected_duration_sec=expected_duration_sec,
                expected_aspect_ratio=expected_aspect_ratio,
                verification_temp_dir=verification_temp_dir,
                occurred_at=occurred_at,
            )
            tts_job_id = "NOT_REQUIRED"
        else:
            if speech is None:
                raise ProductionOrchestrationError(
                    "TTS_REQUIRED scene has no configured speech finalization adapter"
                )
            merged = speech.merge_approved_dialogue(
                job,
                generated,
                scene,
                tuple(dialogue_rows),
                workspace,
                expected_duration_sec=expected_duration_sec,
                expected_aspect_ratio=expected_aspect_ratio,
                occurred_at=occurred_at,
            )
            if job.job_status != "UPLOADING":
                job = self._jobs.transition_scene(
                    job.scene_job_id, "RUNNING", "UPLOADING", occurred_at
                )
            finalization = finalize_merged_output(
                self._final_outputs,
                self._drive,
                production_job_id=claim.production_job.production_job_id,
                scene_prompt_id=str(prompt_id),
                generation_profile_id=preflight.generation_profile_id,
                d2_run_id=generated.d2_run_id,
                provider_account_id=generated.provider_account_id,
                provider_execution_id=generated.provider_execution_id,
                provider_media_id=generated.provider_media_id,
                scene=dict(scene),
                dialogue_rows=tuple(dict(row) for row in dialogue_rows),
                merge_result=merged,
                expected_duration_sec=expected_duration_sec,
                expected_aspect_ratio=expected_aspect_ratio,
                verification_temp_dir=verification_temp_dir,
                occurred_at=occurred_at,
            )
            tts_job_id = ",".join(merged.source_tts_job_ids)

        job = self._jobs.transition_scene(
            job.scene_job_id, "UPLOADING", "REMOTE_VERIFYING", occurred_at
        )
        output_row = self._find_final_output(claim.production_job.production_job_id)
        if output_row is None or output_row.get("Final_Output_ID") != finalization.final_output_id:
            raise ProductionOrchestrationError(
                "FINAL_OUTPUT readback is absent or differs from the finalizer result"
            )
        self._jobs.record_remote_evidence(
            job.scene_job_id,
            provider_execution_id=generated.provider_execution_id,
            provider_media_id=generated.provider_media_id,
            output_sha256=finalization.output_sha256,
            output_size_bytes=finalization.size_bytes,
            output_duration_sec=finalization.video.duration_sec,
            remote_output_uri=finalization.remote_output_uri,
            remote_file_id=finalization.remote_file_id,
            remote_sha256=finalization.output_sha256,
            local_cleanup_status="RETAINED",
            occurred_at=occurred_at,
        )

        try:
            cleaned = cleanup_workspace(
                self._temp_root,
                workspace.job_id,
                "SUCCESS",
                workspace_created_at,
                success_retention=timedelta(0),
                remote_assets_verified=True,
            )
        except Exception as exc:
            self._jobs.record_cleanup_failure(job.scene_job_id, "LOCAL_CLEANUP_FAILED", occurred_at)
            raise ProductionOrchestrationError(
                "verified FINAL_OUTPUT is durable but local cleanup failed"
            ) from exc
        if not cleaned:
            self._jobs.record_cleanup_failure(job.scene_job_id, "LOCAL_CLEANUP_FAILED", occurred_at)
            raise ProductionOrchestrationError(
                "verified FINAL_OUTPUT is durable but local cleanup remains unresolved"
            )
        job = self._jobs.mark_local_cleaned(job.scene_job_id, occurred_at)
        job = self._jobs.complete_scene(job.scene_job_id, occurred_at)
        completed_production = self._jobs.complete_production(
            claim.production_job.production_job_id, output_row, occurred_at
        )
        if completed_production.job_status != "COMPLETED":
            raise ProductionOrchestrationError("Production Job completion readback failed")
        claim = replace(claim, production_job=completed_production)
        return ProductionOrchestrationResult(claim, job, finalization, intent, tts_job_id)

    def _start_production(self, claim: JobClaim, occurred_at: str) -> JobClaim:
        production = claim.production_job
        if production.job_status != "REQUESTED":
            if production.job_status == "QUEUED":
                return claim
            raise JobConflictError("Production Job is unresolved and cannot start generation")
        for expected, following in (
            ("REQUESTED", "VALIDATING"),
            ("VALIDATING", "READY"),
            ("READY", "QUEUED"),
        ):
            production = self._jobs.transition_production(
                production.production_job_id, expected, following, occurred_at
            )
        return replace(claim, production_job=production)

    def _start_running_production(
        self, production: ProductionJob, occurred_at: str
    ) -> ProductionJob:
        if production.job_status == "QUEUED":
            return self._jobs.transition_production(
                production.production_job_id, "QUEUED", "RUNNING", occurred_at
            )
        elif production.job_status == "HOLD":
            return self._jobs.transition_production(
                production.production_job_id, "HOLD", "RUNNING", occurred_at
            )
        elif production.job_status != "RUNNING":
            raise JobConflictError("Production Job cannot enter RUNNING without execution proof")
        return production

    def _hold_before_execution_proof(
        self, claim: JobClaim, scene_job: SceneJob, occurred_at: str
    ) -> None:
        """Keep an unresolved provider attempt held without claiming execution."""
        current = scene_job
        if current.job_status == "QUEUED":
            self._jobs.transition_scene(
                current.scene_job_id,
                "QUEUED",
                "HOLD",
                occurred_at,
                "PROVIDER_RECONCILIATION_REQUIRED",
            )
        production = claim.production_job
        if production.job_status == "QUEUED":
            self._jobs.transition_production(
                production.production_job_id,
                "QUEUED",
                "HOLD",
                occurred_at,
                "PROVIDER_RECONCILIATION_REQUIRED",
            )

    def _find_final_output(self, production_job_id: str) -> dict[str, object] | None:
        rows = [
            dict(row)
            for row in self._final_outputs.read_rows("FINAL_OUTPUT")
            if row.get("Production_Job_ID") == production_job_id
            and row.get("Output_Type") == "FINAL_VIDEO"
            and row.get("Output_Status") == "READY"
        ]
        if len(rows) > 1:
            raise ProductionOrchestrationError(
                "multiple FINAL_VIDEO outputs require operator review"
            )
        return rows[0] if rows else None


def recover_verified_scene_cleanup(
    jobs: ProductionJobStore,
    final_outputs: FinalOutputStore,
    *,
    production_job: ProductionJob,
    scene_job: SceneJob,
    temp_root: Path,
    workspace_id: str,
    workspace_created_at: datetime,
    occurred_at: str,
    audio_intent: AudioIntent,
    tts_job_id: str,
) -> ProductionOrchestrationResult:
    """Resume cleanup only from matching durable output and remote verification."""
    if (
        scene_job.job_status != "REMOTE_VERIFYING"
        or scene_job.local_cleanup_status not in {"RETAINED", "CLEANUP_FAILED"}
        or not scene_job.remote_verified
        or not scene_job.remote_sha256
        or scene_job.remote_sha256 != scene_job.output_sha256
        or production_job.production_job_id != scene_job.production_job_id
        or production_job.job_status != "RUNNING"
        or workspace_id != scene_job.scene_job_id
    ):
        raise ProductionOrchestrationError(
            "cleanup recovery requires a running production and remotely verified scene"
        )
    candidates = [
        dict(row)
        for row in final_outputs.read_rows("FINAL_OUTPUT")
        if row.get("Production_Job_ID") == production_job.production_job_id
        and row.get("Output_Type") == "FINAL_VIDEO"
        and row.get("Remote_File_ID") == scene_job.remote_file_id
    ]
    if len(candidates) != 1:
        raise ProductionOrchestrationError("matching FINAL_OUTPUT readback is absent or ambiguous")
    output = candidates[0]
    validate_final_output_evidence(production_job.production_job_id, output)
    if (
        output.get("Remote_File_ID") != scene_job.remote_file_id
        or output.get("Remote_Output_URI") != scene_job.remote_output_uri
        or output.get("Output_SHA256") != scene_job.output_sha256
        or output.get("Output_Size_Bytes") != scene_job.output_size_bytes
    ):
        raise ProductionOrchestrationError("FINAL_OUTPUT differs from verified Scene Job evidence")
    try:
        cleaned = cleanup_workspace(
            temp_root,
            workspace_id,
            "SUCCESS",
            workspace_created_at,
            success_retention=timedelta(0),
            remote_assets_verified=True,
        )
    except Exception as exc:
        jobs.record_cleanup_failure(scene_job.scene_job_id, "LOCAL_CLEANUP_FAILED", occurred_at)
        raise ProductionOrchestrationError("local cleanup recovery remains unresolved") from exc
    if not cleaned:
        jobs.record_cleanup_failure(scene_job.scene_job_id, "LOCAL_CLEANUP_FAILED", occurred_at)
        raise ProductionOrchestrationError("local cleanup recovery did not remove the workspace")
    jobs.mark_local_cleaned(scene_job.scene_job_id, occurred_at)
    completed_scene = jobs.complete_scene(scene_job.scene_job_id, occurred_at)
    completed_production = jobs.complete_production(
        production_job.production_job_id, output, occurred_at
    )
    return ProductionOrchestrationResult(
        JobClaim(completed_production, (completed_scene,), True),
        completed_scene,
        None,
        audio_intent,
        tts_job_id,
    )

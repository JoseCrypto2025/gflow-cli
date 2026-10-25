from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import av
import pytest

from gflow_cli.v2_runtime.assets import AssetReference
from gflow_cli.v2_runtime.audio_intent import AudioIntent
from gflow_cli.v2_runtime.phase_e import PreflightResult
from gflow_cli.v2_runtime.phase_g import PhaseGExecutionRequest
from gflow_cli.v2_runtime.production_execution import (
    CreditBudgetContext,
    ProductionExecutionContext,
)
from gflow_cli.v2_runtime.production_jobs import (
    InMemoryProductionJobStore,
    ProductionRequest,
)
from gflow_cli.v2_runtime.production_orchestrator import (
    GeneratedSceneAsset,
    ProductionOrchestrationError,
    ProductionOrchestrator,
    recover_verified_scene_cleanup,
)
from gflow_cli.v2_runtime.storage import AssetMetadata
from gflow_cli.v2_runtime.workspace import create_workspace


class MemoryFinalOutputStore:
    def __init__(self) -> None:
        schema = json.loads(
            (Path(__file__).parents[2] / "schemas/transaction/FINAL_OUTPUT.schema.json").read_text()
        )
        self.headers = tuple(field["field_name"] for field in schema["fields"])
        self.rows: list[dict[str, object]] = []

    def read_rows(self, tab: str) -> tuple[dict[str, object], ...]:
        assert tab == "FINAL_OUTPUT"
        return tuple(dict(row) for row in self.rows)

    def schema_headers(self, tab: str) -> tuple[str, ...]:
        assert tab == "FINAL_OUTPUT"
        return self.headers

    def append_if_unique(
        self,
        tab: str,
        row: dict[str, object],
        *,
        unique_constraints: tuple[tuple[str, ...], ...],
        identity_fields: tuple[str, ...],
    ) -> dict[str, object]:
        assert tab == "FINAL_OUTPUT"
        matches = [
            saved
            for saved in self.rows
            if all(
                all(saved.get(field) == row.get(field) for field in fields)
                for fields in unique_constraints
            )
        ]
        if matches:
            if any(matches[0].get(field) != row.get(field) for field in identity_fields):
                raise ValueError("final output key conflicts")
            return dict(matches[0])
        self.rows.append(dict(row))
        return dict(row)


class MemoryDrive:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.items: dict[str, Path] = {}
        self.keys: dict[str, str] = {}

    def upload_asset(self, source: Path, logical_key: str) -> AssetReference:
        file_id = self.keys.get(logical_key)
        if file_id is None:
            file_id = f"file-{len(self.items) + 1}"
            self.keys[logical_key] = file_id
        destination = self.root / file_id
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        self.items[file_id] = destination
        return AssetReference("google-drive", file_id, logical_key, remote_file_id=file_id)

    def verify_remote_asset(self, reference: AssetReference) -> AssetMetadata:
        path = self.items[reference.asset_id]
        return AssetMetadata(True, path.stat().st_size, None, "video/mp4")

    def download_asset(self, reference: AssetReference, destination: Path) -> Path:
        shutil.copyfile(self.items[reference.asset_id], destination)
        return destination


class SilentSceneGenerator:
    def __init__(self) -> None:
        self.calls = 0

    def generate_once(self, scene_job, preflight) -> GeneratedSceneAsset:
        self.calls += 1
        destination = Path(preflight.technical_configuration["video_path"])
        _write_silent_video(destination)
        return GeneratedSceneAsset(
            destination, "PVA_0001", "execution-1", "media-1", "D2_test-run-0001"
        )


class UnavailableSpeech:
    def merge_approved_dialogue(self, *args, **kwargs):
        raise AssertionError("NO_AUDIO must not create or execute a TTS job")


def _write_silent_video(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), mode="w", format="mp4") as container:
        stream = container.add_stream("mpeg4", rate=10)
        stream.width = 160
        stream.height = 90
        stream.pix_fmt = "yuv420p"
        for index in range(20):
            frame = av.VideoFrame(160, 90, "yuv420p")
            frame.pts = index
            frame.time_base = Fraction(1, 10)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def _evidence(root: Path) -> tuple[dict[str, object], dict[str, object], PreflightResult]:
    scene = {
        "Scene_ID": "SCN_000001",
        "Scene_Definition_Status": "LOCKED",
        "Dialogue_Mode": "NONE",
        "Scene_Summary": "Narrative prose that mentions a conversation.",
    }
    prompt = {
        "Scene_Prompt_ID": "SPV_000001",
        "Scene_ID": "SCN_000001",
        "Generation_Profile_ID": "GPR_0001",
        "Prompt_Status": "APPROVED",
        "Prompt_SHA256": "b" * 64,
    }
    preflight = PreflightResult(
        "PREFLIGHT_PASS",
        True,
        "SCN_000001",
        "GPR_0001",
        resolved={"provider_account_id": "PVA_0001"},
        prompt_sha256="b" * 64,
        technical_configuration={"video_path": str(root / "generated" / "generated.mp4")},
    )
    return scene, prompt, preflight


def test_no_audio_orchestrator_completes_without_tts_and_reuses_final_output(
    tmp_path: Path,
) -> None:
    temp_root = tmp_path / "workspaces"
    workspace = create_workspace(temp_root, "SJB_000001")
    scene, prompt, preflight = _evidence(workspace.root)
    preflight = PreflightResult(
        preflight.status,
        preflight.ready_for_provider_adapter,
        preflight.scene_id,
        preflight.generation_profile_id,
        preflight.resolved,
        {"video_path": str(workspace.directories["generated"] / "generated.mp4")},
        preflight.compiled_prompt,
        preflight.compiled_negative_prompt,
        preflight.source_context_sha256,
        preflight.prompt_sha256,
    )
    jobs = InMemoryProductionJobStore()
    outputs = MemoryFinalOutputStore()
    generator = SilentSceneGenerator()
    orchestrator = ProductionOrchestrator(
        jobs, outputs, MemoryDrive(tmp_path / "drive"), temp_root=temp_root
    )

    result = orchestrator.run_scene_once(
        ProductionRequest("SCR_000001", "TEST", "request-1", ("SPV_000001",)),
        scene=scene,
        approved_prompt=prompt,
        preflight=preflight,
        dialogue_rows=(),
        expected_duration_sec=2.0,
        expected_aspect_ratio="16:9",
        workspace=workspace,
        workspace_created_at=datetime.now(UTC),
        verification_temp_dir=tmp_path / "verify",
        occurred_at="2026-10-24T00:00:00Z",
        generator=generator,
        speech=UnavailableSpeech(),
    )

    assert result.audio_intent.value == "NO_AUDIO"
    assert result.tts_job_id == "NOT_REQUIRED"
    assert result.scene_job.job_status == "COMPLETED"
    assert result.claim.production_job.job_status == "COMPLETED"
    assert jobs.production_jobs()[0].job_status == "COMPLETED"
    assert generator.calls == 1
    assert len(outputs.rows) == 1
    assert not workspace.root.exists()

    replay = ProductionOrchestrator(
        jobs, outputs, MemoryDrive(tmp_path / "drive-replay"), temp_root=temp_root
    ).run_scene_once(
        ProductionRequest("SCR_000001", "TEST", "request-1", ("SPV_000001",)),
        scene=scene,
        approved_prompt=prompt,
        preflight=preflight,
        dialogue_rows=(),
        expected_duration_sec=2.0,
        expected_aspect_ratio="16:9",
        workspace=workspace,
        workspace_created_at=datetime.now(UTC),
        verification_temp_dir=tmp_path / "verify",
        occurred_at="2026-10-24T00:01:00Z",
        generator=generator,
    )
    assert replay.final_output is None
    assert generator.calls == 1


def test_orchestrator_composes_the_pinned_phase_g_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gflow_cli.v2_runtime import phase_g_production_adapter as phase_g_adapter

    temp_root = tmp_path / "workspaces"
    workspace = create_workspace(temp_root, "SJB_000001")
    scene, prompt, preflight = _evidence(workspace.root)
    video_path = workspace.directories["generated"] / "generated.mp4"
    _write_silent_video(video_path)
    preflight = PreflightResult(
        preflight.status,
        preflight.ready_for_provider_adapter,
        preflight.scene_id,
        preflight.generation_profile_id,
        preflight.resolved,
        {"video_path": str(video_path)},
        prompt_sha256=preflight.prompt_sha256,
    )

    def make_phase_g_request(scene_job, d2_run_id):
        phase_g_request = PhaseGExecutionRequest(
            scene_id="SCN_000001",
            scene_prompt_id=scene_job.scene_prompt_id,
            generation_profile_id="GPR_0001",
            provider_id="PRV_0001",
            provider_account_id="PVA_0001",
            provider_project_id="project-1",
            model_id="VEO_3",
            generation_mode="TEXT_TO_VIDEO",
            compiled_prompt_text=preflight.compiled_prompt,
            compiled_negative_prompt=preflight.compiled_negative_prompt or "",
            source_context_sha256="a" * 64,
            prompt_sha256="b" * 64,
            duration_sec=2.0,
            aspect_ratio="16:9",
            d2_run_id=d2_run_id,
            output_count=1,
            execution_context=ProductionExecutionContext(
                "PROD",
                "PRV_0001",
                "PVA_0001",
                "project-1",
                "GFLOW_PROD_AUTH",
                "SCN_000001",
                "GPR_0001",
                scene_job.scene_prompt_id,
                d2_run_id,
                CreditBudgetContext(),
            ),
            presubmit_request=SimpleNamespace(temp_root=workspace.directories["generated"]),
        )
        return phase_g_request

    async def execute(request, *, expected_email):
        assert expected_email == "operator@example.test"
        return SimpleNamespace(
            request=request,
            result=SimpleNamespace(
                d2_run_id=request.d2_run_id,
                submission_state="GENERATION_SUCCEEDED",
                provider_project_id="project-1",
                provider_execution_id="execution-1",
                provider_media_id="media-1",
            ),
            local_file=video_path,
        )

    monkeypatch.setattr(phase_g_adapter, "execute_and_validate", execute)
    jobs = InMemoryProductionJobStore()
    outputs = MemoryFinalOutputStore()
    result = ProductionOrchestrator(
        jobs, outputs, MemoryDrive(tmp_path / "drive"), temp_root=temp_root
    ).run_scene_once(
        ProductionRequest("SCR_000001", "PROD", "production-request-1", ("SPV_000001",)),
        scene=scene,
        approved_prompt=prompt,
        preflight=preflight,
        dialogue_rows=(),
        expected_duration_sec=2.0,
        expected_aspect_ratio="16:9",
        workspace=workspace,
        workspace_created_at=datetime.now(UTC),
        verification_temp_dir=tmp_path / "verify",
        occurred_at="2026-10-24T00:00:00Z",
        phase_g_request_factory=make_phase_g_request,
        expected_email="operator@example.test",
    )

    assert result.audio_intent.value == "NO_AUDIO"
    assert result.tts_job_id == "NOT_REQUIRED"
    assert result.scene_job.job_status == "COMPLETED"
    assert result.final_output is not None


def test_ambiguous_generation_is_not_repeated_on_replay(tmp_path: Path) -> None:
    temp_root = tmp_path / "workspaces"
    workspace = create_workspace(temp_root, "SJB_000001")
    scene, prompt, preflight = _evidence(workspace.root)
    preflight = PreflightResult(
        preflight.status,
        preflight.ready_for_provider_adapter,
        preflight.scene_id,
        preflight.generation_profile_id,
        preflight.resolved,
        {"video_path": str(workspace.directories["generated"] / "generated.mp4")},
        prompt_sha256=preflight.prompt_sha256,
    )
    jobs = InMemoryProductionJobStore()
    outputs = MemoryFinalOutputStore()
    generator = SilentSceneGenerator()
    original_generate = generator.generate_once

    def ambiguous(scene_job, result):
        assert scene_job.job_status == "QUEUED"
        assert jobs.production_jobs()[0].job_status == "QUEUED"
        generator.calls += 1
        raise RuntimeError("provider acknowledgement was lost")

    generator.generate_once = ambiguous
    request = ProductionRequest("SCR_000001", "TEST", "request-ambiguous", ("SPV_000001",))
    args = {
        "scene": scene,
        "approved_prompt": prompt,
        "preflight": preflight,
        "dialogue_rows": (),
        "expected_duration_sec": 2.0,
        "expected_aspect_ratio": "16:9",
        "workspace": workspace,
        "workspace_created_at": datetime.now(UTC),
        "verification_temp_dir": tmp_path / "verify",
        "occurred_at": "2026-10-24T00:00:00Z",
        "generator": generator,
    }
    with pytest.raises(RuntimeError, match="acknowledgement was lost"):
        ProductionOrchestrator(
            jobs, outputs, MemoryDrive(tmp_path / "drive"), temp_root=temp_root
        ).run_scene_once(request, **args)
    generator.generate_once = original_generate
    with pytest.raises(ProductionOrchestrationError, match="bound Phase G recovery adapter"):
        ProductionOrchestrator(
            jobs, outputs, MemoryDrive(tmp_path / "drive"), temp_root=temp_root
        ).run_scene_once(request, **args)
    assert generator.calls == 1
    assert jobs.scene_jobs()[0].job_status == "HOLD"
    assert jobs.production_jobs()[0].job_status == "HOLD"


def test_orchestrator_rejects_workspace_not_bound_to_claimed_scene_job(
    tmp_path: Path,
) -> None:
    temp_root = tmp_path / "workspaces"
    workspace = create_workspace(temp_root, "SJB_999999")
    scene, prompt, preflight = _evidence(workspace.root)
    generator = SilentSceneGenerator()
    jobs = InMemoryProductionJobStore()

    with pytest.raises(ProductionOrchestrationError, match="bound to the claimed Scene Job"):
        ProductionOrchestrator(
            jobs, MemoryFinalOutputStore(), MemoryDrive(tmp_path / "drive"), temp_root=temp_root
        ).run_scene_once(
            ProductionRequest("SCR_000001", "TEST", "workspace-mismatch", ("SPV_000001",)),
            scene=scene,
            approved_prompt=prompt,
            preflight=preflight,
            dialogue_rows=(),
            expected_duration_sec=2.0,
            expected_aspect_ratio="16:9",
            workspace=workspace,
            workspace_created_at=datetime.now(UTC),
            verification_temp_dir=tmp_path / "verify",
            occurred_at="2026-10-24T00:00:00Z",
            generator=generator,
        )

    assert generator.calls == 0
    assert workspace.root.exists()
    assert jobs.scene_jobs()[0].job_status == "PENDING"


def test_cleanup_failure_is_durable_and_recoverable_without_regeneration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gflow_cli.v2_runtime import production_orchestrator as module

    temp_root = tmp_path / "workspaces"
    workspace = create_workspace(temp_root, "SJB_000001")
    scene, prompt, preflight = _evidence(workspace.root)
    preflight = PreflightResult(
        preflight.status,
        preflight.ready_for_provider_adapter,
        preflight.scene_id,
        preflight.generation_profile_id,
        {"provider_account_id": "PVA_0001"},
        {"video_path": str(workspace.directories["generated"] / "generated.mp4")},
        prompt_sha256=preflight.prompt_sha256,
    )
    jobs = InMemoryProductionJobStore()
    outputs = MemoryFinalOutputStore()
    drive = MemoryDrive(tmp_path / "drive")
    generator = SilentSceneGenerator()
    monkeypatch.setattr(module, "cleanup_workspace", lambda *args, **kwargs: False)
    with pytest.raises(ProductionOrchestrationError, match="cleanup remains unresolved"):
        ProductionOrchestrator(jobs, outputs, drive, temp_root=temp_root).run_scene_once(
            ProductionRequest("SCR_000001", "TEST", "request-cleanup", ("SPV_000001",)),
            scene=scene,
            approved_prompt=prompt,
            preflight=preflight,
            dialogue_rows=(),
            expected_duration_sec=2.0,
            expected_aspect_ratio="16:9",
            workspace=workspace,
            workspace_created_at=datetime.now(UTC),
            verification_temp_dir=tmp_path / "verify",
            occurred_at="2026-10-24T00:00:00Z",
            generator=generator,
        )
    assert jobs.scene_jobs()[0].job_status == "REMOTE_VERIFYING"
    assert jobs.scene_jobs()[0].local_cleanup_status == "CLEANUP_FAILED"
    assert jobs.scene_jobs()[0].remote_verified
    assert len(outputs.rows) == 1
    monkeypatch.undo()
    shutil.rmtree(workspace.root)

    recovered = recover_verified_scene_cleanup(
        jobs,
        outputs,
        production_job=jobs.production_jobs()[0],
        scene_job=jobs.scene_jobs()[0],
        temp_root=temp_root,
        workspace_id=workspace.job_id,
        workspace_created_at=datetime.now(UTC),
        occurred_at="2026-10-24T00:01:00Z",
        audio_intent=AudioIntent.NO_AUDIO,
        tts_job_id="NOT_REQUIRED",
    )
    assert recovered.scene_job.job_status == "COMPLETED"
    assert jobs.production_jobs()[0].job_status == "COMPLETED"
    assert generator.calls == 1

"""Drive persistence boundary for a completed Scene Job artifact."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol
from urllib.parse import quote
from uuid import uuid4

from .assets import AssetReference
from .google_drive_storage import GoogleDriveStorageAdapter
from .phase_g_artifacts import verify_remote_redownload
from .production_jobs import SceneJob
from .storage import AssetMetadata


class SceneJobPersistenceStore(Protocol):
    def transition_scene(
        self,
        scene_job_id: str,
        expected_status: str,
        next_status: str,
        occurred_at: str,
        reason_code: str | None = None,
    ) -> SceneJob: ...

    def record_remote_evidence(
        self,
        scene_job_id: str,
        *,
        provider_execution_id: str,
        provider_media_id: str,
        output_sha256: str,
        output_size_bytes: int,
        output_duration_sec: float,
        remote_output_uri: str,
        remote_file_id: str,
        remote_sha256: str,
        local_cleanup_status: str,
        occurred_at: str,
    ) -> SceneJob: ...

    def mark_local_cleaned(self, scene_job_id: str, occurred_at: str) -> SceneJob: ...
    def complete_scene(self, scene_job_id: str, occurred_at: str) -> SceneJob: ...


class SceneJobDrivePort(Protocol):
    def upload_asset(self, source: Path, logical_key: str) -> AssetReference: ...
    def verify_remote_asset(self, reference: AssetReference) -> AssetMetadata: ...
    def download_asset(self, reference: AssetReference, destination: Path) -> Path: ...


def persist_scene_output(
    store: SceneJobPersistenceStore,
    drive: GoogleDriveStorageAdapter | SceneJobDrivePort,
    job: SceneJob,
    local_file: Path,
    *,
    provider_execution_id: str,
    provider_media_id: str,
    output_duration_sec: float,
    verification_temp_dir: Path,
    occurred_at: str,
) -> SceneJob:
    """Upload, verify by re-download, record evidence, then clean and complete.

    The Drive key and Scene Job ID are stable across retries. If a process stops
    after upload, Drive's idempotent key resolves the same file on the next call.
    A returned error keeps the local artifact; callers must hold ambiguous state.
    """
    if job.job_status not in {"UPLOADING", "REMOTE_VERIFYING"}:
        raise ValueError("Scene Job persistence requires UPLOADING or REMOTE_VERIFYING")
    if not local_file.is_file() or local_file.is_symlink() or local_file.stat().st_size <= 0:
        raise ValueError("local Scene Job output is missing or unsafe")

    status = job.job_status
    try:
        reference = drive.upload_asset(local_file, f"scene-job:{job.scene_job_id}")
    except Exception:
        _best_effort_hold(store, job.scene_job_id, status, occurred_at)
        raise
    if job.job_status == "UPLOADING":
        try:
            store.transition_scene(job.scene_job_id, "UPLOADING", "REMOTE_VERIFYING", occurred_at)
            status = "REMOTE_VERIFYING"
        except Exception:
            _best_effort_hold(store, job.scene_job_id, "UPLOADING", occurred_at)
            raise

    verification_path: Path | None = None
    try:
        try:
            metadata = drive.verify_remote_asset(reference)
            if not metadata.exists or metadata.size_bytes != local_file.stat().st_size:
                raise ValueError("Drive metadata does not match the local Scene Job output")
            verification_temp_dir.mkdir(parents=True, exist_ok=True)
            verification_path = verification_temp_dir / f"{job.scene_job_id}-{uuid4().hex}.verify"
            downloaded = drive.download_asset(reference, verification_path)
            if downloaded != verification_path or not verification_path.is_file():
                raise ValueError("Drive verification download did not produce its target")
            evidence = verify_remote_redownload(
                local_file,
                verification_path,
                remote_file_id=reference.asset_id,
                remote_filename=reference.logical_key,
            )
        finally:
            if (
                verification_path is not None
                and verification_path.is_file()
                and not verification_path.is_symlink()
            ):
                verification_path.unlink()
    except Exception:
        _best_effort_hold(store, job.scene_job_id, status, occurred_at)
        raise

    remote_uri = f"https://drive.google.com/file/d/{quote(reference.asset_id, safe='')}/view"
    try:
        store.record_remote_evidence(
            job.scene_job_id,
            provider_execution_id=provider_execution_id,
            provider_media_id=provider_media_id,
            output_sha256=evidence.local_sha256,
            output_size_bytes=evidence.size_bytes,
            output_duration_sec=output_duration_sec,
            remote_output_uri=remote_uri,
            remote_file_id=evidence.remote_file_id,
            remote_sha256=evidence.remote_sha256,
            local_cleanup_status="RETAINED",
            occurred_at=occurred_at,
        )

        # The durable row records successful remote verification before unlinking.
        local_file.unlink()
        store.mark_local_cleaned(job.scene_job_id, occurred_at)
        return store.complete_scene(job.scene_job_id, occurred_at)
    except Exception:
        _best_effort_hold(store, job.scene_job_id, status, occurred_at)
        raise


def _best_effort_hold(
    store: SceneJobPersistenceStore,
    scene_job_id: str,
    expected_status: str,
    occurred_at: str,
) -> None:
    try:
        store.transition_scene(
            scene_job_id,
            expected_status,
            "HOLD",
            occurred_at,
            reason_code="DRIVE_PERSISTENCE_UNRESOLVED",
        )
    except Exception:
        # Preserve the original failure; the caller must treat any unresolved
        # transition as held and inspect the stable Scene Job ID before retrying.
        return

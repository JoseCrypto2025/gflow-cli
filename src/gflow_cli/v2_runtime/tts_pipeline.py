"""Single-submit TTS execution, Drive verification, and durable job completion."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol
from urllib.parse import quote
from uuid import uuid4

from .assets import AssetReference
from .google_drive_storage import GoogleDriveStorageAdapter
from .media_audio import (
    AudioInfo,
    AudioValidationError,
    canonicalize_audio,
    inspect_audio,
    plan_audio_timeline,
)
from .media_merge import MergeResult, merge_audio_video
from .phase_g_artifacts import sha256_file, verify_remote_redownload
from .segmented_tts import SegmentedTTSProvider, TTSProviderAuthError, TTSProviderError
from .tts import TTSArtifact, TTSJobClaim, TTSJobRequest, TTSJobStore
from .workspace import JobWorkspace, validate_workspace


class TTSLifecycleStore(TTSJobStore, Protocol):
    def validate_completed_claim(self, claim: TTSJobClaim) -> TTSArtifact: ...

    def transition(
        self,
        tts_job_id: str,
        *,
        expected_status: str,
        next_status: str,
        occurred_at: str,
        error_code: str | None = None,
    ) -> object: ...

    def record_remote_evidence(
        self,
        tts_job_id: str,
        *,
        provider_execution_id: str,
        remote_audio_uri: str,
        remote_file_id: str,
        audio_sha256: str,
        audio_size_bytes: int,
        audio_duration_ms: int,
        occurred_at: str,
    ) -> object: ...

    def record_provider_receipt(
        self, tts_job_id: str, provider_execution_id: str, occurred_at: str
    ) -> object: ...

    def mark_local_cleaned(self, tts_job_id: str, occurred_at: str) -> object: ...
    def complete(self, tts_job_id: str, occurred_at: str) -> object: ...

    def cleanup_state(self, tts_job_id: str) -> dict[str, object]: ...

    def record_cleanup_failure(
        self, tts_job_id: str, reason_code: str, occurred_at: str
    ) -> object: ...


class TTSCleanupIncompleteError(RuntimeError):
    """Verified remote audio remains durable but local cleanup needs recovery."""


class TTSDrivePort(Protocol):
    def upload_asset(self, source: Path, logical_key: str) -> AssetReference: ...
    def verify_remote_asset(self, reference: AssetReference) -> object: ...
    def download_asset(self, reference: AssetReference, destination: Path) -> Path: ...


@dataclass(frozen=True, slots=True)
class TTSExecutionResult:
    claim: TTSJobClaim
    provider_execution_id: str
    remote_audio_uri: str
    remote_file_id: str
    audio_sha256: str
    audio_size_bytes: int
    audio_duration_ms: int


@dataclass(frozen=True, slots=True)
class TTSAudioSegment:
    result: TTSExecutionResult
    pause_after_ms: int = 0

    def __post_init__(self) -> None:
        if (
            isinstance(self.pause_after_ms, bool)
            or type(self.pause_after_ms) is not int
            or self.pause_after_ms < 0
        ):
            raise ValueError("dialogue pause must be a nonnegative integer")


def execute_tts_once(
    store: TTSLifecycleStore,
    provider: SegmentedTTSProvider,
    drive: GoogleDriveStorageAdapter | TTSDrivePort,
    request: TTSJobRequest,
    *,
    workspace: JobWorkspace,
    temp_root: Path,
    occurred_at: str,
) -> TTSExecutionResult:
    """Claim and execute one attempt; ambiguous submits are never retried here."""
    directories = validate_workspace(temp_root, workspace)
    audio_dir = directories["audio"]
    claim = store.claim(request, occurred_at)
    if claim.job_status == "COMPLETED":
        artifact = store.completed_artifact(claim.tts_job_id)
        if artifact is None:
            raise ValueError("completed TTS attempt has no durable artifact evidence")
        return _result_from_artifact(claim, artifact)
    if claim.job_status != "PENDING":
        raise ValueError(
            f"TTS attempt {claim.tts_job_id} is unresolved; provider submit is blocked"
        )
    tts_job_id = claim.tts_job_id
    raw_audio = audio_dir / f"{tts_job_id}-provider.mp3"
    canonical_audio = audio_dir / f"{tts_job_id}-canonical.wav"
    if raw_audio.exists() or raw_audio.is_symlink() or canonical_audio.exists():
        raise ValueError("TTS attempt workspace already contains output files")

    store.transition(
        tts_job_id,
        expected_status="PENDING",
        next_status="VALIDATING",
        occurred_at=occurred_at,
    )
    try:
        provider.health()
    except TTSProviderError:
        store.transition(
            tts_job_id,
            expected_status="VALIDATING",
            next_status="HOLD",
            occurred_at=occurred_at,
            error_code="TTS_PROVIDER_UNAVAILABLE",
        )
        raise
    store.transition(
        tts_job_id,
        expected_status="VALIDATING",
        next_status="RUNNING",
        occurred_at=occurred_at,
    )
    try:
        provider_result = provider.synthesize(
            request,
            tts_job_id=tts_job_id,
            destination=raw_audio,
            rate=request.rate,
            pitch=request.pitch,
            volume=request.volume,
        )
    except TTSProviderAuthError:
        _fail(store, tts_job_id, occurred_at, "TTS_PROVIDER_AUTH_REJECTED")
        raise
    except Exception:
        _hold(store, tts_job_id, occurred_at, "TTS_SUBMIT_OUTCOME_UNKNOWN")
        raise

    try:
        store.record_provider_receipt(
            tts_job_id,
            provider_result.provider_execution_id,
            occurred_at,
        )
        if (
            provider_result.audio_path.resolve(strict=True) != raw_audio.resolve(strict=True)
            or provider_result.audio_path.is_symlink()
        ):
            raise AudioValidationError("TTS provider artifact escaped its attempt workspace")
    except Exception:
        _hold(store, tts_job_id, occurred_at, "TTS_PROVIDER_RECEIPT_UNRESOLVED")
        raise

    try:
        plan_audio_timeline(
            video_duration_sec=request.target_duration_sec,
            audio_duration_sec=provider_result.audio.duration_sec,
        )
        canonical_audio_info = canonicalize_audio(
            provider_result.audio_path,
            canonical_audio,
            max_duration_sec=request.target_duration_sec,
        )
        plan_audio_timeline(
            video_duration_sec=request.target_duration_sec,
            audio_duration_sec=canonical_audio_info.duration_sec,
        )
    except ValueError:
        _fail(store, tts_job_id, occurred_at, "TTS_DURATION_OR_AUDIO_INVALID")
        raise
    except Exception:
        _hold(store, tts_job_id, occurred_at, "TTS_AUDIO_VALIDATION_UNRESOLVED")
        raise

    store.transition(
        tts_job_id,
        expected_status="RUNNING",
        next_status="UPLOADING",
        occurred_at=occurred_at,
    )
    try:
        persisted = _persist_tts_audio(
            store,
            drive,
            claim,
            provider_result.provider_execution_id,
            canonical_audio,
            raw_audio,
            occurred_at=occurred_at,
            verification_temp_dir=directories["validated"],
        )
    except TTSCleanupIncompleteError:
        raise
    except Exception:
        _hold(store, tts_job_id, occurred_at, "TTS_PERSISTENCE_UNRESOLVED")
        raise
    return TTSExecutionResult(
        replace(claim, job_status="COMPLETED"),
        provider_result.provider_execution_id,
        f"https://drive.google.com/file/d/{quote(persisted.remote_file_id, safe='')}/view",
        persisted.remote_file_id,
        persisted.audio.sha256,
        persisted.audio.size_bytes,
        round(persisted.audio.duration_sec * 1000),
    )


def _result_from_artifact(claim: TTSJobClaim, artifact: TTSArtifact) -> TTSExecutionResult:
    return TTSExecutionResult(
        claim,
        artifact.provider_execution_id,
        artifact.remote_audio_uri,
        artifact.remote_file_id,
        artifact.audio_sha256,
        artifact.audio_size_bytes,
        artifact.audio_duration_ms,
    )


def merge_verified_tts_audio(
    store: TTSLifecycleStore,
    drive: GoogleDriveStorageAdapter | TTSDrivePort,
    tts_result: TTSExecutionResult,
    video_path: Path,
    output_path: Path,
    *,
    workspace: JobWorkspace,
    temp_root: Path,
    expected_duration_sec: float,
    expected_aspect_ratio: str,
) -> MergeResult:
    """Backward-compatible one-line entry point for verified TTS assembly."""
    return merge_verified_tts_segments(
        store,
        drive,
        (TTSAudioSegment(tts_result),),
        video_path,
        output_path,
        workspace=workspace,
        temp_root=temp_root,
        expected_duration_sec=expected_duration_sec,
        expected_aspect_ratio=expected_aspect_ratio,
    )


def merge_verified_tts_segments(
    store: TTSLifecycleStore,
    drive: GoogleDriveStorageAdapter | TTSDrivePort,
    segments: tuple[TTSAudioSegment, ...],
    video_path: Path,
    output_path: Path,
    *,
    workspace: JobWorkspace,
    temp_root: Path,
    expected_duration_sec: float,
    expected_aspect_ratio: str,
    ffmpeg: str = "ffmpeg",
) -> MergeResult:
    """Assemble ordered persisted dialogue-line audio, then mux it onto video."""
    if not segments or any(segment.result.claim.job_status != "COMPLETED" for segment in segments):
        raise ValueError("merge requires at least one completed TTS job")
    tts_job_ids = tuple(segment.result.claim.tts_job_id for segment in segments)
    dialogue_line_ids = tuple(segment.result.claim.request.dialogue_line_id for segment in segments)
    if any(not re.fullmatch(r"TTS_[0-9]{6}", job_id) for job_id in tts_job_ids):
        raise ValueError("merge requires persisted TTS Job IDs in the canonical format")
    if len(set(tts_job_ids)) != len(tts_job_ids) or len(set(dialogue_line_ids)) != len(
        dialogue_line_ids
    ):
        raise ValueError("ordered TTS segments must have unique job and dialogue identities")
    directories = validate_workspace(temp_root, workspace)
    audio_dir = directories["audio"]
    safe_output = output_path.resolve(strict=False)
    if safe_output.parent != directories["validated"] or output_path.is_symlink():
        raise ValueError("merge output must remain inside the validated job workspace")
    sources: list[Path] = []
    concat_audio = audio_dir / f"{'-'.join(tts_job_ids)}-assembly.wav"
    try:
        if concat_audio.exists() or concat_audio.is_symlink():
            raise ValueError("TTS assembly workspace path is occupied")
        for segment in segments:
            result = segment.result
            _verify_persisted_tts_result(store, result)
            audio_path = audio_dir / f"{result.claim.tts_job_id}-merge-source.wav"
            if audio_path.exists() or audio_path.is_symlink():
                raise ValueError("merge source workspace path is occupied")
            reference = AssetReference(
                "GOOGLE_DRIVE",
                result.remote_file_id,
                f"tts-job:{result.claim.tts_job_id}",
                remote_file_id=result.remote_file_id,
                size_bytes=result.audio_size_bytes,
            )
            metadata = drive.verify_remote_asset(reference)
            if (
                not getattr(metadata, "exists", False)
                or getattr(metadata, "size_bytes", None) != result.audio_size_bytes
            ):
                raise AudioValidationError(
                    "persisted TTS file is outside its verified artifact contract"
                )
            downloaded = drive.download_asset(reference, audio_path)
            if downloaded != audio_path or not audio_path.is_file() or audio_path.is_symlink():
                raise AudioValidationError("verified TTS audio could not be retrieved for merge")
            sources.append(audio_path)
            if (
                audio_path.stat().st_size != result.audio_size_bytes
                or sha256_file(audio_path) != result.audio_sha256
            ):
                raise AudioValidationError("retrieved TTS audio differs from its completed job")
            audio = inspect_audio(
                audio_path, max_duration_sec=result.claim.request.target_duration_sec
            )
            if abs(audio.duration_sec * 1000 - result.audio_duration_ms) > 20:
                raise AudioValidationError("retrieved TTS duration differs from its completed job")
        _assemble_ordered_audio(sources, segments, concat_audio, ffmpeg=ffmpeg)
        inspect_audio(concat_audio, max_duration_sec=expected_duration_sec)
        if concat_audio.stat().st_size <= 0:
            raise AudioValidationError("assembled TTS audio is empty")
        return merge_audio_video(
            video_path,
            concat_audio,
            output_path,
            expected_aspect_ratio=expected_aspect_ratio,
            expected_duration_sec=expected_duration_sec,
            source_tts_job_ids=tts_job_ids,
            source_dialogue_line_ids=dialogue_line_ids,
        )
    finally:
        for audio_path in (*sources, concat_audio):
            if audio_path.is_file() and not audio_path.is_symlink():
                audio_path.unlink()


def _verify_persisted_tts_result(store: TTSLifecycleStore, result: TTSExecutionResult) -> None:
    artifact = store.validate_completed_claim(result.claim)
    if (
        artifact.provider_execution_id != result.provider_execution_id
        or artifact.remote_audio_uri != result.remote_audio_uri
        or artifact.remote_file_id != result.remote_file_id
        or artifact.audio_sha256 != result.audio_sha256
        or artifact.audio_size_bytes != result.audio_size_bytes
        or artifact.audio_duration_ms != result.audio_duration_ms
    ):
        raise AudioValidationError("TTS result differs from its persisted completed attempt")


def _assemble_ordered_audio(
    sources: list[Path],
    segments: tuple[TTSAudioSegment, ...],
    output: Path,
    *,
    ffmpeg: str,
) -> None:
    executable = shutil.which(ffmpeg)
    if executable is None:
        raise AudioValidationError("ffmpeg is required for ordered TTS assembly")
    command = [executable, "-nostdin", "-hide_banner", "-loglevel", "error"]
    for source in sources:
        command.extend(["-i", str(source)])
    labels: list[str] = []
    filters: list[str] = []
    for index, segment in enumerate(segments):
        label = f"line{index}"
        pause_sec = segment.pause_after_ms / 1000
        if pause_sec > 0 and index < len(segments) - 1:
            filters.append(f"[{index}:a]apad=pad_dur={pause_sec:.3f}[{label}]")
        else:
            filters.append(f"[{index}:a]anull[{label}]")
        labels.append(f"[{label}]")
    if len(sources) == 1:
        filters.append(f"{labels[0]}anull[outa]")
    else:
        filters.append(f"{''.join(labels)}concat=n={len(sources)}:v=0:a=1[outa]")
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[outa]",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "1",
            str(output),
        ]
    )
    try:
        subprocess.run(command, capture_output=True, check=True, timeout=60)
    except Exception as exc:  # noqa: BLE001 — subprocess output may contain source data
        output.unlink(missing_ok=True)
        raise AudioValidationError("ordered TTS audio assembly failed") from exc


@dataclass(frozen=True, slots=True)
class _PersistedAudio:
    remote_file_id: str
    audio: AudioInfo


def _persist_tts_audio(
    store: TTSLifecycleStore,
    drive: GoogleDriveStorageAdapter | TTSDrivePort,
    claim: TTSJobClaim,
    provider_execution_id: str,
    source: Path,
    raw_source: Path,
    *,
    occurred_at: str,
    verification_temp_dir: Path,
) -> _PersistedAudio:
    if not source.is_file() or source.is_symlink() or source.stat().st_size <= 0:
        raise AudioValidationError("canonical TTS audio is missing or unsafe")
    reference = drive.upload_asset(source, f"tts-job:{claim.tts_job_id}")
    store.transition(
        claim.tts_job_id,
        expected_status="UPLOADING",
        next_status="REMOTE_VERIFYING",
        occurred_at=occurred_at,
    )
    metadata = drive.verify_remote_asset(reference)
    if (
        getattr(metadata, "exists", False) is not True
        or getattr(metadata, "size_bytes", None) != source.stat().st_size
    ):
        raise AudioValidationError("remote TTS audio metadata differs from the local artifact")
    verification_temp_dir.mkdir(parents=True, exist_ok=True)
    verify_path = verification_temp_dir / f"{claim.tts_job_id}-{uuid4().hex}.verify"
    if verify_path.exists() or verify_path.is_symlink():
        raise AudioValidationError("TTS verification path is occupied")
    try:
        downloaded = drive.download_asset(reference, verify_path)
        if downloaded != verify_path or not verify_path.is_file() or verify_path.is_symlink():
            raise AudioValidationError("remote TTS verification download is missing")
        evidence = verify_remote_redownload(
            source,
            verify_path,
            remote_file_id=reference.asset_id,
            remote_filename=reference.logical_key,
        )
    finally:
        if verify_path.is_file() and not verify_path.is_symlink():
            verify_path.unlink()
    audio = inspect_audio(source, max_duration_sec=claim.request.target_duration_sec)
    store.record_remote_evidence(
        claim.tts_job_id,
        provider_execution_id=provider_execution_id,
        remote_audio_uri=(
            f"https://drive.google.com/file/d/{quote(reference.asset_id, safe='')}/view"
        ),
        remote_file_id=evidence.remote_file_id,
        audio_sha256=evidence.local_sha256,
        audio_size_bytes=evidence.size_bytes,
        audio_duration_ms=round(audio.duration_sec * 1000),
        occurred_at=occurred_at,
    )
    try:
        for local_path in (source, raw_source):
            if local_path.is_symlink():
                raise AudioValidationError("TTS cleanup path became a symlink")
            local_path.unlink(missing_ok=True)
        store.mark_local_cleaned(claim.tts_job_id, occurred_at)
        store.complete(claim.tts_job_id, occurred_at)
    except Exception as exc:
        try:
            state = store.cleanup_state(claim.tts_job_id)
            if state.get("Local_Cleanup_Status") != "CLEANED":
                store.record_cleanup_failure(
                    claim.tts_job_id, "TTS_LOCAL_CLEANUP_FAILED", occurred_at
                )
        except Exception as recovery_error:
            raise TTSCleanupIncompleteError(
                "verified TTS audio is durable but cleanup evidence is unresolved"
            ) from recovery_error
        raise TTSCleanupIncompleteError(
            "verified TTS audio is durable but local cleanup needs recovery"
        ) from exc
    return _PersistedAudio(evidence.remote_file_id, audio)


def recover_tts_cleanup(
    store: TTSLifecycleStore,
    claim: TTSJobClaim,
    *,
    workspace: JobWorkspace,
    temp_root: Path,
    occurred_at: str,
) -> TTSExecutionResult:
    """Finish only cleanup for a row with already verified remote TTS evidence."""
    state = store.cleanup_state(claim.tts_job_id)
    if (
        state.get("Job_Status") != "REMOTE_VERIFYING"
        or state.get("Remote_Verified") is not True
        or not state.get("Remote_File_ID")
        or state.get("Local_Cleanup_Status") not in {"RETAINED", "CLEANUP_FAILED", "CLEANED"}
    ):
        raise ValueError("TTS cleanup recovery requires verified remote audio")
    if state.get("Local_Cleanup_Status") == "CLEANED":
        store.complete(claim.tts_job_id, occurred_at)
        completed_claim = replace(claim, job_status="COMPLETED")
        return _result_from_artifact(
            completed_claim, store.validate_completed_claim(completed_claim)
        )
    directories = validate_workspace(temp_root, workspace)
    expected = (
        directories["audio"] / f"{claim.tts_job_id}-canonical.wav",
        directories["audio"] / f"{claim.tts_job_id}-provider.mp3",
    )
    for path in expected:
        try:
            path.resolve(strict=False).relative_to(directories["audio"])
        except ValueError as exc:
            raise AudioValidationError("TTS recovery path escaped its attempt workspace") from exc
        if path.is_symlink():
            raise AudioValidationError("TTS recovery refuses a symlink cleanup target")
    try:
        for path in expected:
            path.unlink(missing_ok=True)
        store.mark_local_cleaned(claim.tts_job_id, occurred_at)
        store.complete(claim.tts_job_id, occurred_at)
    except Exception as exc:
        current = store.cleanup_state(claim.tts_job_id)
        if current.get("Local_Cleanup_Status") != "CLEANED":
            store.record_cleanup_failure(claim.tts_job_id, "TTS_LOCAL_CLEANUP_FAILED", occurred_at)
        raise TTSCleanupIncompleteError("TTS local cleanup recovery remains unresolved") from exc
    completed_claim = replace(claim, job_status="COMPLETED")
    return _result_from_artifact(completed_claim, store.validate_completed_claim(completed_claim))


def _hold(store: TTSLifecycleStore, tts_job_id: str, occurred_at: str, code: str) -> None:
    for expected in ("VALIDATING", "RUNNING", "UPLOADING", "REMOTE_VERIFYING"):
        try:
            store.transition(
                tts_job_id,
                expected_status=expected,
                next_status="HOLD",
                occurred_at=occurred_at,
                error_code=code,
            )
            return
        except Exception:
            continue


def _fail(store: TTSLifecycleStore, tts_job_id: str, occurred_at: str, code: str) -> None:
    try:
        store.transition(
            tts_job_id,
            expected_status="RUNNING",
            next_status="FAILED",
            occurred_at=occurred_at,
            error_code=code,
        )
    except Exception:
        return

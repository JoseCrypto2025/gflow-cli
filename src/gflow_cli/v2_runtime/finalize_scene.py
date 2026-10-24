"""Verified FINAL_OUTPUT persistence for speech and NO_AUDIO scene paths."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote
from uuid import uuid4

from .assets import AssetReference
from .google_drive_storage import GoogleDriveStorageAdapter
from .media_audio import AudioValidationError, inspect_audio
from .media_merge import MergeResult
from .phase_g_artifacts import (
    ValidatedVideo,
    VerifiedRemoteArtifact,
    validate_video,
    verify_remote_redownload,
)
from .storage import AssetMetadata


class NoAudioFinalizationError(ValueError):
    """The approved scene evidence does not authorize a NO_AUDIO finalization."""


class FinalOutputStore(Protocol):
    def read_rows(self, tab: str) -> tuple[dict[str, object], ...]: ...

    def schema_headers(self, tab: str) -> tuple[str, ...]: ...

    def append_if_unique(
        self,
        tab: str,
        row: dict[str, object],
        *,
        unique_constraints: tuple[tuple[str, ...], ...],
        identity_fields: tuple[str, ...],
    ) -> dict[str, object]: ...


class FinalOutputDrivePort(Protocol):
    def upload_asset(self, source: Path, logical_key: str) -> AssetReference: ...

    def verify_remote_asset(self, reference: AssetReference) -> AssetMetadata: ...

    def download_asset(self, reference: AssetReference, destination: Path) -> Path: ...


@dataclass(frozen=True, slots=True)
class FinalOutputFinalization:
    final_output_id: str
    production_job_id: str
    remote_file_id: str
    remote_output_uri: str
    output_sha256: str
    size_bytes: int
    video: ValidatedVideo


def finalize_no_audio(
    store: FinalOutputStore,
    drive: GoogleDriveStorageAdapter | FinalOutputDrivePort,
    *,
    production_job_id: str,
    scene_prompt_id: str,
    generation_profile_id: str,
    d2_run_id: str,
    provider_account_id: str,
    provider_execution_id: str,
    provider_media_id: str,
    scene: dict[str, object],
    dialogue_rows: tuple[dict[str, object], ...],
    video_path: Path,
    expected_duration_sec: float,
    expected_aspect_ratio: str,
    verification_temp_dir: Path,
    occurred_at: str,
    output_version: int = 1,
) -> FinalOutputFinalization:
    """Persist a validated silent video without inventing a TTS transaction."""
    scene_id = scene.get("Scene_ID")
    if (
        not isinstance(scene_id, str)
        or not scene_id.strip()
        or scene.get("Dialogue_Mode") != "NONE"
        or any(row.get("Scene_ID") == scene_id for row in dialogue_rows)
    ):
        raise NoAudioFinalizationError(
            "NO_AUDIO requires Dialogue_Mode=NONE and no linked dialogue rows"
        )
    if (
        not re.fullmatch(r"PRD_[0-9]{6}", production_job_id)
        or output_version <= 0
        or not occurred_at.strip()
        or not scene_prompt_id.strip()
        or not generation_profile_id.strip()
        or not re.fullmatch(r"D2_[A-Za-z0-9_-]{8,64}", d2_run_id)
        or not provider_account_id.strip()
        or not provider_execution_id.strip()
        or not provider_media_id.strip()
    ):
        raise ValueError("final output identity or timestamp is invalid")

    video = validate_video(
        video_path,
        expected_duration_sec=expected_duration_sec,
        expected_aspect_ratio=expected_aspect_ratio,
    )
    if video.audio_present:
        raise NoAudioFinalizationError("NO_AUDIO input video unexpectedly contains an audio stream")
    manifest_json, manifest = _manifest_json_sha256(
        {
            "schema_version": 1,
            "production_job_id": production_job_id,
            "scene_prompt_id": scene_prompt_id,
            "generation_profile_id": generation_profile_id,
            "d2_run_id": d2_run_id,
            "assembly_kind": "NO_AUDIO_V1",
            "scene_id": scene_id,
            "provider_account_id": provider_account_id,
            "provider_execution_id": provider_execution_id,
            "provider_media_id": provider_media_id,
            "audio_intent": "NO_AUDIO",
            "video_sha256": video.sha256,
            "duration_policy": "VIDEO_TIMELINE_IS_AUTHORITATIVE",
        }
    )
    return _persist_final_video(
        store,
        drive,
        production_job_id=production_job_id,
        video_path=video_path,
        video=video,
        manifest_json=manifest_json,
        manifest_sha256=manifest,
        validation_message="NO_AUDIO; full video decode passed",
        verification_temp_dir=verification_temp_dir,
        occurred_at=occurred_at,
        output_version=output_version,
    )


def finalize_merged_output(
    store: FinalOutputStore,
    drive: GoogleDriveStorageAdapter | FinalOutputDrivePort,
    *,
    production_job_id: str,
    scene_prompt_id: str,
    generation_profile_id: str,
    d2_run_id: str,
    provider_account_id: str,
    provider_execution_id: str,
    provider_media_id: str,
    scene: dict[str, object],
    dialogue_rows: tuple[dict[str, object], ...],
    merge_result: MergeResult,
    expected_duration_sec: float,
    expected_aspect_ratio: str,
    verification_temp_dir: Path,
    occurred_at: str,
    output_version: int = 1,
) -> FinalOutputFinalization:
    """Persist a completed speech/merge output after validating both streams."""
    scene_id = scene.get("Scene_ID")
    audio_intent = scene.get("Dialogue_Mode")
    if (
        not isinstance(scene_id, str)
        or not scene_prompt_id.strip()
        or not generation_profile_id.strip()
        or not re.fullmatch(r"D2_[A-Za-z0-9_-]{8,64}", d2_run_id)
        or not provider_account_id.strip()
        or not provider_execution_id.strip()
        or not provider_media_id.strip()
        or audio_intent not in {"DIALOGUE", "NARRATION", "MIXED"}
        or not any(row.get("Scene_ID") == scene_id for row in dialogue_rows)
    ):
        raise NoAudioFinalizationError(
            "speech output requires an audio-intent scene with linked dialogue"
        )
    scene_dialogue_rows = [row for row in dialogue_rows if row.get("Scene_ID") == scene_id]
    line_numbers = [_integer(row.get("Line_No")) for row in scene_dialogue_rows]
    if any(line_number is None or line_number < 1 for line_number in line_numbers) or len(
        set(line_numbers)
    ) != len(line_numbers):
        raise NoAudioFinalizationError(
            "speech output requires unique positive dialogue line numbers"
        )
    scene_dialogue_rows.sort(key=lambda row: _integer(row.get("Line_No")) or 0)
    expected_dialogue_line_ids = tuple(
        str(row.get("Dialogue_Line_ID", "")) for row in scene_dialogue_rows
    )
    if (
        not all(expected_dialogue_line_ids)
        or not merge_result.source_tts_job_ids
        or merge_result.source_dialogue_line_ids != expected_dialogue_line_ids
        or len(merge_result.source_tts_job_ids) != len(expected_dialogue_line_ids)
    ):
        raise NoAudioFinalizationError(
            "merged output must identify one ordered completed TTS job per dialogue line"
        )
    video = validate_video(
        merge_result.path,
        expected_duration_sec=expected_duration_sec,
        expected_aspect_ratio=expected_aspect_ratio,
    )
    if not video.audio_present or video.sha256 != merge_result.sha256:
        raise NoAudioFinalizationError("merged video does not match its validated result")
    audio = inspect_audio(merge_result.path, max_duration_sec=video.duration_sec + 0.15)
    if audio.codec != "aac" or abs(audio.duration_sec - video.duration_sec) > 0.15:
        raise AudioValidationError("merged final output failed AAC timeline validation")
    manifest_json, manifest_sha256 = _manifest_json_sha256(
        {
            "schema_version": 1,
            "production_job_id": production_job_id,
            "scene_prompt_id": scene_prompt_id,
            "generation_profile_id": generation_profile_id,
            "d2_run_id": d2_run_id,
            "assembly_kind": "TTS_MERGE_V2",
            "scene_id": scene_id,
            "provider_account_id": provider_account_id,
            "provider_execution_id": provider_execution_id,
            "provider_media_id": provider_media_id,
            "audio_intent": str(audio_intent),
            "video_sha256": video.sha256,
            "audio_sha256": audio.sha256,
            "source_audio_sha256": merge_result.source_audio_sha256,
            "source_video_sha256": merge_result.source_video_sha256,
            "merge_job_sha256": merge_result.merge_job_sha256,
            "source_tts_job_ids": list(merge_result.source_tts_job_ids),
            "source_dialogue_line_ids": list(merge_result.source_dialogue_line_ids),
            "video_strategy": merge_result.video_strategy,
            "video_duration_sec": video.duration_sec,
            "aspect_ratio": video.aspect_ratio,
            "duration_policy": "VIDEO_TIMELINE_IS_AUTHORITATIVE",
        }
    )
    return _persist_final_video(
        store,
        drive,
        production_job_id=production_job_id,
        video_path=merge_result.path,
        video=video,
        manifest_json=manifest_json,
        manifest_sha256=manifest_sha256,
        validation_message=(
            "PASS; merged audio and video decode passed; "
            f"video_strategy={merge_result.video_strategy}"
        ),
        verification_temp_dir=verification_temp_dir,
        occurred_at=occurred_at,
        output_version=output_version,
    )


def _persist_final_video(
    store: FinalOutputStore,
    drive: GoogleDriveStorageAdapter | FinalOutputDrivePort,
    *,
    production_job_id: str,
    video_path: Path,
    video: ValidatedVideo,
    manifest_json: str,
    manifest_sha256: str,
    validation_message: str,
    verification_temp_dir: Path,
    occurred_at: str,
    output_version: int,
) -> FinalOutputFinalization:
    if (
        not re.fullmatch(r"PRD_[0-9]{6}", production_job_id)
        or output_version <= 0
        or not occurred_at.strip()
    ):
        raise ValueError("final output identity or timestamp is invalid")
    existing = [
        row
        for row in store.read_rows("FINAL_OUTPUT")
        if row.get("Production_Job_ID") == production_job_id
        and row.get("Output_Type") == "FINAL_VIDEO"
        and _integer(row.get("Output_Version")) == output_version
    ]
    if len(existing) > 1:
        raise NoAudioFinalizationError("final output key is duplicated; HOLD required")
    active_primary = [
        row
        for row in store.read_rows("FINAL_OUTPUT")
        if row.get("Production_Job_ID") == production_job_id
        and row.get("Output_Type") == "FINAL_VIDEO"
        and _truth(row.get("Is_Primary"))
        and row.get("Output_Status") != "ARCHIVED"
    ]
    if active_primary and (
        len(active_primary) != 1
        or not existing
        or active_primary[0].get("Final_Output_ID") != existing[0].get("Final_Output_ID")
    ):
        raise NoAudioFinalizationError("another active primary final video already exists")
    logical_key = f"final-output:{production_job_id}:FINAL_VIDEO:{output_version}"
    reference = drive.upload_asset(video_path, logical_key)
    metadata = drive.verify_remote_asset(reference)
    if not metadata.exists or metadata.size_bytes != video.size_bytes:
        raise NoAudioFinalizationError("remote final video metadata did not verify")
    verify_root = verification_temp_dir.resolve()
    verify_root.mkdir(parents=True, exist_ok=True)
    verify_path = verify_root / f"{production_job_id}-{output_version}-{video.sha256}.verify"
    if verify_path.exists() or verify_path.is_symlink():
        raise NoAudioFinalizationError("final output verification path is occupied")
    try:
        downloaded = drive.download_asset(reference, verify_path)
        if downloaded != verify_path or not verify_path.is_file() or verify_path.is_symlink():
            raise NoAudioFinalizationError("remote final video could not be re-downloaded")
        evidence = verify_remote_redownload(
            video_path,
            verify_path,
            remote_file_id=reference.asset_id,
            remote_filename=reference.logical_key,
        )
    finally:
        if verify_path.is_file() and not verify_path.is_symlink():
            verify_path.unlink()

    manifest_file_id = _persist_assembly_manifest(
        drive,
        manifest_json,
        manifest_sha256,
        logical_key=f"final-output-manifest:{production_job_id}:FINAL_VIDEO:{output_version}",
        verification_temp_dir=verification_temp_dir,
    )
    persisted_validation_message = (
        f"{validation_message}; assembly_manifest_file_id={manifest_file_id}; "
        f"assembly_manifest_sha256={manifest_sha256}"
    )

    if existing:
        current = existing[0]
        if (
            current.get("Output_SHA256") != evidence.local_sha256
            or current.get("Assembly_Manifest_SHA256") != manifest_sha256
            or current.get("Remote_File_ID") != evidence.remote_file_id
            or not _truth(current.get("Remote_Verified"))
            or current.get("Validation_Status") != "PASS"
            or current.get("Output_Status") != "READY"
            or current.get("Validation_Message") != persisted_validation_message
        ):
            raise NoAudioFinalizationError("existing final output conflicts with verified input")
        return _result(current, production_job_id, reference, evidence, video)

    headers = store.schema_headers("FINAL_OUTPUT")
    next_id = 1 + max(
        (_suffix(row.get("Final_Output_ID")) for row in store.read_rows("FINAL_OUTPUT")), default=0
    )
    row: dict[str, object] = {header: "" for header in headers}
    row.update(
        {
            "Final_Output_ID": f"FOUT_{next_id:06d}",
            "Production_Job_ID": production_job_id,
            "Output_Version": output_version,
            "Output_Type": "FINAL_VIDEO",
            "Is_Primary": True,
            "Assembly_Manifest_SHA256": manifest_sha256,
            "Remote_Output_URI": (
                f"https://drive.google.com/file/d/{quote(reference.asset_id, safe='')}/view"
            ),
            "Remote_File_ID": evidence.remote_file_id,
            "Output_SHA256": evidence.local_sha256,
            "Output_Size_Bytes": evidence.size_bytes,
            "Duration_Sec": video.duration_sec,
            "Width": video.width,
            "Height": video.height,
            "Aspect_Ratio": video.aspect_ratio,
            "Validation_Status": "PASS",
            "Validation_Message": persisted_validation_message,
            "Output_Status": "READY",
            "Remote_Verified": True,
            "Created_At": occurred_at,
            "Updated_At": occurred_at,
        }
    )
    persisted = store.append_if_unique(
        "FINAL_OUTPUT",
        row,
        unique_constraints=(("Production_Job_ID", "Output_Type", "Output_Version"),),
        identity_fields=(
            "Assembly_Manifest_SHA256",
            "Output_SHA256",
            "Remote_File_ID",
            "Remote_Output_URI",
            "Remote_Verified",
            "Output_Type",
            "Output_Version",
            "Output_Size_Bytes",
            "Duration_Sec",
            "Width",
            "Height",
            "Aspect_Ratio",
            "Is_Primary",
            "Validation_Status",
            "Output_Status",
        ),
    )
    if not _truth(persisted.get("Remote_Verified")):
        raise NoAudioFinalizationError("final output readback did not prove remote verification")
    return _result(persisted, production_job_id, reference, evidence, video)


def _persist_assembly_manifest(
    drive: GoogleDriveStorageAdapter | FinalOutputDrivePort,
    manifest_json: str,
    manifest_sha256: str,
    *,
    logical_key: str,
    verification_temp_dir: Path,
) -> str:
    if (
        not verification_temp_dir.is_absolute()
        or verification_temp_dir.is_symlink()
        or not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256)
        or hashlib.sha256(manifest_json.encode("utf-8")).hexdigest() != manifest_sha256
    ):
        raise NoAudioFinalizationError("assembly manifest workspace or checksum is invalid")
    verification_temp_dir.mkdir(parents=True, exist_ok=True)
    root = verification_temp_dir.resolve(strict=True)
    source = root / f"{uuid4().hex}.manifest.json"
    downloaded_path = root / f"{uuid4().hex}.manifest.verify"
    try:
        with source.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(manifest_json)
            handle.flush()
        reference = drive.upload_asset(source, logical_key)
        metadata = drive.verify_remote_asset(reference)
        if not metadata.exists or metadata.size_bytes != source.stat().st_size:
            raise NoAudioFinalizationError("remote assembly manifest metadata did not verify")
        downloaded = drive.download_asset(reference, downloaded_path)
        if (
            downloaded != downloaded_path
            or not downloaded_path.is_file()
            or downloaded_path.is_symlink()
        ):
            raise NoAudioFinalizationError("remote assembly manifest could not be re-downloaded")
        evidence = verify_remote_redownload(
            source,
            downloaded_path,
            remote_file_id=reference.asset_id,
            remote_filename=reference.logical_key,
        )
        if evidence.local_sha256 != manifest_sha256:
            raise NoAudioFinalizationError("remote assembly manifest checksum differs")
        return reference.asset_id
    finally:
        for path in (source, downloaded_path):
            if path.is_file() and not path.is_symlink():
                path.unlink()


def _result(
    row: dict[str, object],
    production_job_id: str,
    reference: AssetReference,
    evidence: VerifiedRemoteArtifact,
    video: ValidatedVideo,
) -> FinalOutputFinalization:
    remote_file_id = evidence.remote_file_id
    output_sha256 = evidence.local_sha256
    return FinalOutputFinalization(
        str(row["Final_Output_ID"]),
        production_job_id,
        remote_file_id,
        f"https://drive.google.com/file/d/{quote(reference.asset_id, safe='')}/view",
        output_sha256,
        video.size_bytes,
        video,
    )


def _manifest_json_sha256(value: dict[str, object]) -> tuple[str, str]:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if str(parsed) == str(value) else None


def _truth(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.strip().upper() == "TRUE")


def _suffix(value: object) -> int:
    match = re.fullmatch(r"FOUT_([0-9]{6})", str(value or ""))
    return int(match.group(1)) if match else 0

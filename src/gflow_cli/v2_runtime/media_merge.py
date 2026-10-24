"""Timeline-safe FFmpeg muxing and complete output validation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import av

from .media_audio import AudioInfo, AudioValidationError, inspect_audio, plan_audio_timeline
from .phase_g_artifacts import ValidatedVideo, sha256_file, validate_video

CommandRunner = Callable[[list[str]], None]


class MergeValidationError(ValueError):
    """Merged video/audio output failed an objective media gate."""


@dataclass(frozen=True, slots=True)
class MergeResult:
    path: Path
    sha256: str
    size_bytes: int
    duration_sec: float
    video_strategy: str
    video_validation: ValidatedVideo
    audio_validation: AudioInfo
    source_video_sha256: str
    source_audio_sha256: str
    merge_job_sha256: str
    source_tts_job_ids: tuple[str, ...] = ()
    source_dialogue_line_ids: tuple[str, ...] = ()


def _video_duration(path: Path) -> float:
    if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0:
        raise MergeValidationError("video input is missing or unsafe")
    try:
        with av.open(str(path), mode="r") as container:
            videos = [stream for stream in container.streams if stream.type == "video"]
            if len(videos) != 1:
                raise MergeValidationError("video input must contain exactly one video stream")
            stream = videos[0]
            if stream.duration is not None and stream.time_base is not None:
                duration = float(stream.duration * stream.time_base)
            elif container.duration is not None:
                duration = float(container.duration / av.time_base)
            else:
                raise MergeValidationError("video duration is unavailable")
            if duration <= 0:
                raise MergeValidationError("video duration is invalid")
            return duration
    except MergeValidationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise MergeValidationError("video probe failed") from exc


def _run_ffmpeg(command: list[str]) -> None:
    subprocess.run(command, capture_output=True, check=True, timeout=180)


def _command(
    ffmpeg: str,
    video: Path,
    audio: Path,
    target: Path,
    *,
    pad_duration: float,
    reencode_video: bool,
) -> list[str]:
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(video),
        "-i",
        str(audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
    ]
    if pad_duration > 0:
        command.extend(["-af", f"apad=pad_dur={pad_duration:.6f}"])
    if reencode_video:
        command.extend(
            ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
        )
    else:
        command.extend(["-c:v", "copy"])
    command.extend(
        [
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "1",
            "-movflags",
            "+faststart",
            str(target),
        ]
    )
    return command


def _validate_output(
    path: Path, *, duration: float, aspect: str
) -> tuple[ValidatedVideo, AudioInfo]:
    video = validate_video(
        path,
        expected_duration_sec=duration,
        expected_aspect_ratio=aspect,
        duration_tolerance_sec=0.15,
    )
    try:
        audio = inspect_audio(path, max_duration_sec=duration + 0.15)
    except AudioValidationError as exc:
        raise MergeValidationError("merged AAC stream failed probe or full decode") from exc
    if audio.codec != "aac":
        raise MergeValidationError("merged audio codec is not AAC")
    if abs(audio.duration_sec - duration) > 0.15:
        raise MergeValidationError("merged audio does not preserve the video timeline")
    return video, audio


def merge_audio_video(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    *,
    expected_aspect_ratio: str,
    expected_duration_sec: float,
    ffmpeg: str = "ffmpeg",
    command_runner: CommandRunner | None = None,
    source_tts_job_ids: tuple[str, ...] = (),
    source_dialogue_line_ids: tuple[str, ...] = (),
) -> MergeResult:
    """Mux one validated TTS stream while preserving the complete video timeline."""
    executable = shutil.which(ffmpeg)
    if executable is None:
        raise MergeValidationError("ffmpeg is required for video/audio merge")
    if output_path.exists() or output_path.is_symlink():
        return _reuse_completed_merge(
            video_path,
            audio_path,
            output_path,
            expected_aspect_ratio=expected_aspect_ratio,
            expected_duration_sec=expected_duration_sec,
            source_tts_job_ids=source_tts_job_ids,
            source_dialogue_line_ids=source_dialogue_line_ids,
        )
    manifest_path = _manifest_path(output_path)
    if manifest_path.exists() or manifest_path.is_symlink():
        raise MergeValidationError("merge manifest exists without a reusable output")
    source_video_sha256 = sha256_file(video_path)
    source_audio_sha256 = sha256_file(audio_path)
    duration = _video_duration(video_path)
    if expected_duration_sec <= 0 or abs(duration - expected_duration_sec) > 0.15:
        raise MergeValidationError("video duration differs from the approved scene timeline")
    source_audio = inspect_audio(audio_path, max_duration_sec=duration)
    timeline = plan_audio_timeline(
        video_duration_sec=duration,
        audio_duration_sec=source_audio.duration_sec,
    )
    validate_video(
        video_path,
        expected_duration_sec=expected_duration_sec,
        expected_aspect_ratio=expected_aspect_ratio,
        duration_tolerance_sec=0.15,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    merge_identity = _merge_identity(
        source_video_sha256,
        source_audio_sha256,
        duration,
        expected_duration_sec,
        expected_aspect_ratio,
        source_tts_job_ids,
        source_dialogue_line_ids,
    )
    runner = command_runner or _run_ffmpeg
    with tempfile.TemporaryDirectory(prefix=f".{output_path.stem}-", dir=output_path.parent) as raw:
        workspace = Path(raw)
        copy_path = workspace / "copy.mp4"
        fallback_path = workspace / "reencode.mp4"
        copy_command = _command(
            executable,
            video_path,
            audio_path,
            copy_path,
            pad_duration=timeline.audio_pad_duration_sec,
            reencode_video=False,
        )
        strategy = "VIDEO_COPY"
        try:
            runner(copy_command)
            video_info, audio_info = _validate_output(
                copy_path, duration=duration, aspect=expected_aspect_ratio
            )
            selected = copy_path
        except Exception as copy_error:
            fallback_command = _command(
                executable,
                video_path,
                audio_path,
                fallback_path,
                pad_duration=timeline.audio_pad_duration_sec,
                reencode_video=True,
            )
            try:
                runner(fallback_command)
                video_info, audio_info = _validate_output(
                    fallback_path, duration=duration, aspect=expected_aspect_ratio
                )
                selected = fallback_path
                strategy = "VIDEO_REENCODE"
            except Exception as fallback_error:
                raise MergeValidationError(
                    "copy and H.264 merge paths failed validation"
                ) from fallback_error
            del copy_error
        staged_output = workspace / "completed.mp4"
        shutil.copyfile(selected, staged_output)
        staged_output.rename(output_path)
    result_sha256 = sha256_file(output_path)
    manifest = {
        "schema_version": 1,
        "merge_job_sha256": merge_identity,
        "source_video_sha256": source_video_sha256,
        "source_audio_sha256": source_audio_sha256,
        "expected_aspect_ratio": expected_aspect_ratio,
        "duration_policy": "VIDEO_TIMELINE_IS_AUTHORITATIVE",
        "expected_duration_sec": expected_duration_sec,
        "source_tts_job_ids": list(source_tts_job_ids),
        "source_dialogue_line_ids": list(source_dialogue_line_ids),
        "video_strategy": strategy,
        "output_sha256": result_sha256,
        "output_size_bytes": output_path.stat().st_size,
        "output_duration_sec": video_info.duration_sec,
    }
    _write_manifest_once(manifest_path, manifest)
    return MergeResult(
        output_path,
        result_sha256,
        output_path.stat().st_size,
        video_info.duration_sec,
        strategy,
        video_info,
        audio_info,
        source_video_sha256,
        source_audio_sha256,
        merge_identity,
        source_tts_job_ids,
        source_dialogue_line_ids,
    )


def _manifest_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.name}.gflow-merge.json")


def _merge_identity(
    video_sha256: str,
    audio_sha256: str,
    duration: float,
    expected_duration_sec: float,
    aspect: str,
    tts_job_ids: tuple[str, ...],
    dialogue_line_ids: tuple[str, ...],
) -> str:
    value = {
        "schema_version": 1,
        "source_video_sha256": video_sha256,
        "source_audio_sha256": audio_sha256,
        "video_duration_sec": duration,
        "expected_duration_sec": expected_duration_sec,
        "expected_aspect_ratio": aspect,
        "duration_policy": "VIDEO_TIMELINE_IS_AUTHORITATIVE",
        "source_tts_job_ids": list(tts_job_ids),
        "source_dialogue_line_ids": list(dialogue_line_ids),
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _write_manifest_once(path: Path, value: Mapping[str, object]) -> None:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if path.exists() or path.is_symlink():
        raise MergeValidationError("merge manifest destination is occupied")
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise MergeValidationError("merge completion manifest could not be persisted") from exc


def _reuse_completed_merge(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    *,
    expected_aspect_ratio: str,
    expected_duration_sec: float,
    source_tts_job_ids: tuple[str, ...],
    source_dialogue_line_ids: tuple[str, ...],
) -> MergeResult:
    manifest_path = _manifest_path(output_path)
    if (
        output_path.is_symlink()
        or manifest_path.is_symlink()
        or not output_path.is_file()
        or not manifest_path.is_file()
    ):
        raise MergeValidationError("occupied merge output has no safe completion manifest")
    source_video_sha256 = sha256_file(video_path)
    source_audio_sha256 = sha256_file(audio_path)
    duration = _video_duration(video_path)
    if expected_duration_sec <= 0 or abs(duration - expected_duration_sec) > 0.15:
        raise MergeValidationError("video duration differs from the approved scene timeline")
    merge_identity = _merge_identity(
        source_video_sha256,
        source_audio_sha256,
        duration,
        expected_duration_sec,
        expected_aspect_ratio,
        source_tts_job_ids,
        source_dialogue_line_ids,
    )
    try:
        decoded: object = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MergeValidationError("merge completion manifest is unreadable") from exc
    if not isinstance(decoded, dict):
        raise MergeValidationError("merge completion manifest has an invalid shape")
    decoded_mapping = cast(dict[object, object], decoded)
    if any(not isinstance(key, str) for key in decoded_mapping):
        raise MergeValidationError("merge completion manifest has an invalid shape")
    manifest = cast(dict[str, object], decoded_mapping)
    if any(
        manifest.get(key) != value
        for key, value in {
            "schema_version": 1,
            "merge_job_sha256": merge_identity,
            "source_video_sha256": source_video_sha256,
            "source_audio_sha256": source_audio_sha256,
            "expected_aspect_ratio": expected_aspect_ratio,
            "duration_policy": "VIDEO_TIMELINE_IS_AUTHORITATIVE",
            "expected_duration_sec": expected_duration_sec,
            "source_tts_job_ids": list(source_tts_job_ids),
            "source_dialogue_line_ids": list(source_dialogue_line_ids),
            "output_sha256": sha256_file(output_path),
            "output_size_bytes": output_path.stat().st_size,
        }.items()
    ):
        raise MergeValidationError("existing merge output conflicts with its request or manifest")
    strategy = manifest.get("video_strategy")
    if strategy not in {"VIDEO_COPY", "VIDEO_REENCODE"}:
        raise MergeValidationError("existing merge strategy is invalid")
    video_info, audio_info = _validate_output(
        output_path, duration=expected_duration_sec, aspect=expected_aspect_ratio
    )
    if manifest.get("output_duration_sec") != video_info.duration_sec:
        raise MergeValidationError("existing merge duration differs from its manifest")
    return MergeResult(
        output_path,
        str(manifest["output_sha256"]),
        output_path.stat().st_size,
        video_info.duration_sec,
        str(strategy),
        video_info,
        audio_info,
        source_video_sha256,
        source_audio_sha256,
        merge_identity,
        source_tts_job_ids,
        source_dialogue_line_ids,
    )

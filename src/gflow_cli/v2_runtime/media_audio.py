"""Audio probing, full decode validation, and deterministic normalization."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import av
import numpy as np

from .phase_g_artifacts import sha256_file

TTS_DURATION_MISMATCH = "TTS_DURATION_MISMATCH"
CANONICAL_SAMPLE_RATE_HZ = 48_000
CANONICAL_CHANNELS = 1
MIN_AUDIO_SIZE_BYTES = 128


class AudioValidationError(ValueError):
    """Audio is absent, malformed, incomplete, or outside the media contract."""


@dataclass(frozen=True, slots=True)
class AudioInfo:
    codec: str
    sample_rate_hz: int
    channels: int
    duration_sec: float
    bitrate: int
    size_bytes: int
    sha256: str
    silence_ratio: float
    clipped_sample_ratio: float


@dataclass(frozen=True, slots=True)
class AudioTimelinePlan:
    video_duration_sec: float
    source_audio_duration_sec: float
    audio_pad_duration_sec: float


def inspect_audio(
    path: Path,
    *,
    ffprobe: str = "ffprobe",
    max_duration_sec: float | None = None,
) -> AudioInfo:
    if not path.is_file() or path.is_symlink():
        raise AudioValidationError("audio file is missing or unsafe")
    size = path.stat().st_size
    if size == 0:
        raise AudioValidationError("audio file is empty")
    if size < MIN_AUDIO_SIZE_BYTES:
        raise AudioValidationError("audio file is below the minimum nontrivial size")
    if max_duration_sec is not None and max_duration_sec <= 0:
        raise ValueError("maximum audio duration must be positive")
    probe_path = shutil.which(ffprobe)
    if probe_path is None:
        raise AudioValidationError("ffprobe is required for audio validation")
    try:
        completed = subprocess.run(
            [
                probe_path,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,sample_rate,channels,bit_rate,duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            check=True,
            timeout=20,
        )
        payload: object = json.loads(completed.stdout)
    except Exception as exc:  # noqa: BLE001 — media tool output may contain local paths
        raise AudioValidationError("ffprobe could not read the audio file") from exc
    if not isinstance(payload, dict):
        raise AudioValidationError("ffprobe returned an invalid audio response")
    payload_map = cast(dict[str, object], payload)
    stream_value = payload_map.get("streams")
    if not isinstance(stream_value, list):
        raise AudioValidationError("ffprobe returned an invalid audio response")
    streams = cast(list[object], stream_value)
    if not streams or not isinstance(streams[0], dict):
        raise AudioValidationError("audio stream is missing")
    stream = cast(dict[str, object], streams[0])
    codec = stream.get("codec_name")
    rate = stream.get("sample_rate")
    channels = stream.get("channels")
    bitrate = stream.get("bit_rate")
    if not isinstance(codec, str) or not codec:
        raise AudioValidationError("audio codec is unknown")
    sample_rate = _metadata_int(rate)
    channel_count = _metadata_int(channels)
    bit_rate = _metadata_int(bitrate) if bitrate is not None else 0
    if sample_rate is None or channel_count is None or bit_rate is None:
        raise AudioValidationError("audio stream metadata is invalid")
    probe_duration = _metadata_float(stream.get("duration"))
    if max_duration_sec is not None and probe_duration is not None:
        if probe_duration > max_duration_sec:
            raise AudioValidationError(TTS_DURATION_MISMATCH)
    if not 8_000 <= sample_rate <= 192_000 or channel_count not in {1, 2}:
        raise AudioValidationError(
            "audio sample rate or channel layout is outside supported bounds"
        )
    try:
        with av.open(str(path), mode="r") as container:
            audio_streams = [item for item in container.streams if item.type == "audio"]
            if len(audio_streams) != 1:
                raise AudioValidationError("audio file must contain exactly one audio stream")
            decoded_samples = 0
            silence_samples = 0
            clipped_samples = 0
            total_samples = 0
            peak = 0.0
            count_nonzero = cast(Callable[[object], int], np.__dict__["count_nonzero"])
            for frame in container.decode(audio=0):
                if frame.sample_rate != sample_rate:
                    raise AudioValidationError("audio sample rate changes during decode")
                if (
                    max_duration_sec is not None
                    and (decoded_samples + frame.samples) / sample_rate > max_duration_sec
                ):
                    raise AudioValidationError(TTS_DURATION_MISMATCH)
                data = cast(np.ndarray, frame.to_ndarray())
                if np.issubdtype(data.dtype, np.integer):
                    scale = float(2 ** (8 * data.dtype.itemsize - 1) - 1)
                    samples = data.astype(np.float64) / scale
                else:
                    samples = data.astype(np.float64)
                absolute = np.abs(samples)
                peak = max(peak, float(np.max(absolute, initial=0.0)))
                total_samples += int(samples.size)
                silence_samples += count_nonzero(absolute < 0.001)
                clipped_samples += count_nonzero(absolute >= 0.999)
                decoded_samples += frame.samples
            if decoded_samples <= 0:
                raise AudioValidationError("audio stream contains no decodable samples")
            duration = decoded_samples / sample_rate
            if max_duration_sec is not None and duration > max_duration_sec:
                raise AudioValidationError(TTS_DURATION_MISMATCH)
            silence_ratio = silence_samples / max(total_samples, 1)
            clipped_ratio = clipped_samples / max(total_samples, 1)
            if peak <= 0.001 or silence_ratio >= 0.999:
                raise AudioValidationError("audio is silent or has pathological silence")
            if clipped_ratio > 0.01:
                raise AudioValidationError("audio is clipped or distorted")
    except AudioValidationError:
        raise
    except Exception as exc:  # noqa: BLE001 — decoder errors may expose source paths
        raise AudioValidationError("complete audio decode failed") from exc
    return AudioInfo(
        codec,
        sample_rate,
        channel_count,
        duration,
        bit_rate,
        size,
        sha256_file(path),
        silence_ratio,
        clipped_ratio,
    )


def plan_audio_timeline(
    *, video_duration_sec: float, audio_duration_sec: float
) -> AudioTimelinePlan:
    if video_duration_sec <= 0 or audio_duration_sec <= 0:
        raise ValueError("video and audio durations must be positive")
    if audio_duration_sec > video_duration_sec:
        raise ValueError(TTS_DURATION_MISMATCH)
    return AudioTimelinePlan(
        video_duration_sec,
        audio_duration_sec,
        video_duration_sec - audio_duration_sec,
    )


def _metadata_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _metadata_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if np.isfinite(parsed) and parsed > 0 else None


def canonicalize_audio(
    source: Path,
    destination: Path,
    *,
    ffmpeg: str = "ffmpeg",
    max_duration_sec: float | None = None,
) -> AudioInfo:
    """Decode once to mono 48 kHz PCM and validate the resulting WAV."""
    inspect_audio(source, max_duration_sec=max_duration_sec)
    executable = shutil.which(ffmpeg)
    if executable is None:
        raise AudioValidationError("ffmpeg is required for audio normalization")
    if destination.exists() or destination.is_symlink():
        raise AudioValidationError("canonical audio destination must be unused")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.stem}-", suffix=".wav", dir=destination.parent, delete=False
    ) as temp:
        temporary = Path(temp.name)
    temporary.unlink()
    try:
        subprocess.run(
            [
                executable,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                str(CANONICAL_CHANNELS),
                "-ar",
                str(CANONICAL_SAMPLE_RATE_HZ),
                "-c:a",
                "pcm_s16le",
                "-f",
                "wav",
                str(temporary),
            ],
            capture_output=True,
            check=True,
            timeout=60,
        )
        info = inspect_audio(temporary, max_duration_sec=max_duration_sec)
        if info.sample_rate_hz != CANONICAL_SAMPLE_RATE_HZ or info.channels != CANONICAL_CHANNELS:
            raise AudioValidationError("canonical audio format did not match its contract")
        temporary.replace(destination)
        return inspect_audio(destination, max_duration_sec=max_duration_sec)
    except AudioValidationError:
        temporary.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        temporary.unlink(missing_ok=True)
        raise AudioValidationError("canonical audio conversion failed") from exc

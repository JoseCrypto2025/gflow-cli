"""Phase G media verification and upload/re-download persistence gates."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from .assets import AssetReference
from .errors import AssetVerificationError
from .storage import AssetMetadata

# Historical Flow evidence in PR #824 reported 4.01 s for an exact 4 s request.
# This is a narrow tolerance, not a provider guarantee; a live clip outside it is
# rejected and never normalized to make it pass.
D2_DURATION_TOLERANCE_SEC = 0.05


@dataclass(frozen=True, slots=True)
class ValidatedVideo:
    path: Path
    size_bytes: int
    sha256: str
    duration_sec: float
    width: int
    height: int
    aspect_ratio: str
    audio_present: bool
    decoded_video_frames: int


@dataclass(frozen=True, slots=True)
class PersistedArtifact:
    reference: AssetReference
    size_bytes: int
    sha256: str
    remote_sha256: str
    remote_verified: bool


class PhaseGStoragePort(Protocol):
    """Storage operations required to prove remote retrievability."""

    def upload_asset(self, source: Path, logical_key: str) -> AssetReference: ...
    def get_metadata(self, reference: AssetReference) -> AssetMetadata: ...
    def download_asset(self, reference: AssetReference, destination: Path) -> Path: ...


def sha256_file(path: Path) -> str:
    """Hash a local artifact using bounded-memory reads."""
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_video(
    path: Path,
    *,
    expected_duration_sec: float,
    expected_aspect_ratio: str,
    duration_tolerance_sec: float = D2_DURATION_TOLERANCE_SEC,
) -> ValidatedVideo:
    """Require an MP4 container, full video decode, duration and aspect match."""
    if not path.is_file() or path.is_symlink():
        raise AssetVerificationError("Phase G artifact is missing or is a symlink")
    size = path.stat().st_size
    if size <= 0:
        raise AssetVerificationError("Phase G artifact is empty")
    with path.open("rb") as stream:
        header = stream.read(12)
    if len(header) < 8 or header[4:8] != b"ftyp":
        raise AssetVerificationError("Phase G artifact does not have an MP4 ftyp signature")
    if expected_duration_sec <= 0 or duration_tolerance_sec < 0:
        raise ValueError("expected duration and tolerance must be positive")
    try:
        import av
    except ImportError as exc:  # pragma: no cover - depends on optional media extra
        raise AssetVerificationError("PyAV is required for full Phase G media validation") from exc
    try:
        with av.open(str(path), mode="r") as container:
            video_streams = [stream for stream in container.streams if stream.type == "video"]
            if len(video_streams) != 1:
                raise AssetVerificationError(
                    "Phase G artifact must contain exactly one video stream"
                )
            video = video_streams[0]
            width = height = 0
            decoded = 0
            last_pts: int | None = None
            for frame in container.decode(video=0):
                decoded += 1
                if decoded == 1:
                    width, height = frame.width, frame.height
                last_pts = frame.pts
            if decoded == 0:
                raise AssetVerificationError("Phase G video has no decodable frames")
            if width <= 0 or height <= 0:
                raise AssetVerificationError("Phase G video dimensions are invalid")
            duration: float | None = None
            if video.duration is not None and video.time_base is not None:
                duration = float(video.duration * video.time_base)
            elif container.duration is not None:
                duration = float(container.duration / av.time_base)
            if duration is None and video.average_rate is not None and last_pts is not None:
                duration = float((last_pts + 1) / video.average_rate)
            if duration is None or duration <= 0:
                raise AssetVerificationError("Phase G video duration is unavailable")
            if abs(duration - expected_duration_sec) > duration_tolerance_sec:
                raise AssetVerificationError(
                    f"Phase G video duration {duration:.3f}s is outside the approved tolerance"
                )
            expected_aspect = _parse_aspect(expected_aspect_ratio)
            actual_aspect = width / height
            if abs(actual_aspect - expected_aspect) / expected_aspect > 0.01:
                raise AssetVerificationError(
                    "Phase G video aspect ratio does not match the request"
                )
            audio_present = any(stream.type == "audio" for stream in container.streams)
    except AssetVerificationError:
        raise
    except Exception as exc:
        raise AssetVerificationError("Phase G MP4 container or full decode failed") from exc
    return ValidatedVideo(
        path,
        size,
        sha256_file(path),
        duration,
        width,
        height,
        expected_aspect_ratio,
        audio_present,
        decoded,
    )


def persist_and_verify(
    storage: PhaseGStoragePort,
    local_file: Path,
    logical_key: str,
    *,
    verification_temp_dir: Path,
    cleanup_local: bool = True,
) -> PersistedArtifact:
    """Upload, retrieve into a separate path, hash-verify and only then clean up."""
    if not local_file.is_file() or local_file.is_symlink():
        raise AssetVerificationError("local Phase G artifact is missing or unsafe")
    local_hash = sha256_file(local_file)
    local_size = local_file.stat().st_size
    reference = storage.upload_asset(local_file, logical_key)
    metadata = storage.get_metadata(reference)
    if not metadata.exists:
        raise AssetVerificationError("uploaded Phase G artifact is not remotely retrievable")
    if metadata.size_bytes is not None and metadata.size_bytes != local_size:
        raise AssetVerificationError("remote Phase G artifact size differs from local file")
    if metadata.checksum_sha256:
        remote_hash = metadata.checksum_sha256.lower()
    else:
        verification_temp_dir.mkdir(parents=True, exist_ok=True)
        verify_path = verification_temp_dir / f"{reference.asset_id}.verify"
        if verify_path.resolve() == local_file.resolve() or verify_path.exists():
            raise AssetVerificationError("remote verification path is not a separate unused file")
        downloaded = storage.download_asset(reference, verify_path)
        if downloaded != verify_path or not verify_path.is_file() or verify_path.is_symlink():
            raise AssetVerificationError(
                "remote verification download did not produce the target file"
            )
        remote_hash = sha256_file(verify_path)
        if verify_path.stat().st_size != local_size:
            raise AssetVerificationError("remote re-download size differs from local file")
        verify_path.unlink()
    if remote_hash != local_hash:
        raise AssetVerificationError("remote Phase G artifact SHA-256 differs from local file")
    if cleanup_local:
        local_file.unlink()
    return PersistedArtifact(reference, local_size, local_hash, remote_hash, True)


def _parse_aspect(value: str) -> float:
    try:
        width_text, height_text = value.split(":", maxsplit=1)
        width, height = float(width_text), float(height_text)
    except (ValueError, TypeError) as exc:
        raise ValueError("aspect ratio must have WIDTH:HEIGHT form") from exc
    if width <= 0 or height <= 0:
        raise ValueError("aspect ratio dimensions must be positive")
    return width / height

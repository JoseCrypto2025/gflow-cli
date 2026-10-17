from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from gflow_cli.v2_runtime.assets import AssetReference
from gflow_cli.v2_runtime.errors import AssetVerificationError
from gflow_cli.v2_runtime.phase_g_artifacts import (
    persist_and_verify,
    validate_video,
)
from gflow_cli.v2_runtime.storage import AssetMetadata


def _synthetic_mp4(path: Path, *, frames: int = 80, width: int = 180, height: int = 320) -> Path:
    av = pytest.importorskip("av")
    np = pytest.importorskip("numpy")
    container = av.open(str(path), mode="w")
    try:
        stream = container.add_stream("mpeg4", rate=10)
        stream.width = width
        stream.height = height
        stream.pix_fmt = "yuv420p"
        for index in range(frames):
            pixels = np.full((height, width, 3), index % 255, dtype="uint8")
            frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()
    return path


class FakeStorage:
    def __init__(self, root: Path, *, corrupt: bool = False) -> None:
        self.root = root
        self.corrupt = corrupt
        self.remote = root / "remote.mp4"

    def upload_asset(self, source: Path, logical_key: str) -> AssetReference:
        self.root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, self.remote)
        if self.corrupt:
            content = bytearray(self.remote.read_bytes())
            content[-1] ^= 0x01
            self.remote.write_bytes(content)
        return AssetReference("google-drive", "drive-id-1", logical_key, "drive-id-1")

    def get_metadata(self, reference: AssetReference) -> AssetMetadata:
        if not self.remote.is_file():
            return AssetMetadata(False)
        return AssetMetadata(True, self.remote.stat().st_size, None, "video/mp4")

    def download_asset(self, reference: AssetReference, destination: Path) -> Path:
        shutil.copyfile(self.remote, destination)
        return destination


def test_validate_video_fully_decodes_and_reports_expected_contract(tmp_path: Path) -> None:
    path = _synthetic_mp4(tmp_path / "clip.mp4")
    result = validate_video(path, expected_duration_sec=8, expected_aspect_ratio="9:16")
    assert result.size_bytes > 0
    assert len(result.sha256) == 64
    assert result.duration_sec == pytest.approx(8, abs=0.05)
    assert (result.width, result.height) == (180, 320)
    assert result.audio_present is False
    assert result.decoded_video_frames == 80


def test_validate_video_rejects_wrong_duration_and_non_mp4(tmp_path: Path) -> None:
    path = _synthetic_mp4(tmp_path / "clip.mp4")
    with pytest.raises(AssetVerificationError, match="duration"):
        validate_video(path, expected_duration_sec=7, expected_aspect_ratio="9:16")
    corrupt = tmp_path / "broken.mp4"
    corrupt.write_bytes(b"not an mp4 file")
    with pytest.raises(AssetVerificationError, match="ftyp"):
        validate_video(corrupt, expected_duration_sec=8, expected_aspect_ratio="9:16")


def test_persistence_requires_remote_redownload_hash_before_local_cleanup(
    tmp_path: Path,
) -> None:
    local = _synthetic_mp4(tmp_path / "local.mp4")
    persisted = persist_and_verify(
        FakeStorage(tmp_path / "drive"),
        local,
        "d2_run__SCN_000001__SPV_000002__media.mp4",
        verification_temp_dir=tmp_path / "verify",
    )
    assert persisted.remote_verified is True
    assert persisted.sha256 == persisted.remote_sha256
    assert not local.exists()
    assert list((tmp_path / "verify").iterdir()) == []


def test_failed_remote_hash_verification_retains_primary_local_artifact(
    tmp_path: Path,
) -> None:
    local = _synthetic_mp4(tmp_path / "local.mp4")
    with pytest.raises(AssetVerificationError, match="SHA-256"):
        persist_and_verify(
            FakeStorage(tmp_path / "drive", corrupt=True),
            local,
            "d2_run__SCN_000001__SPV_000002__media.mp4",
            verification_temp_dir=tmp_path / "verify",
        )
    assert local.is_file()

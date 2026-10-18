from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from gflow_cli.v2_runtime.errors import AdapterUnavailableError, AssetVerificationError
from gflow_cli.v2_runtime.google_drive_storage import GoogleDriveStorageAdapter
from gflow_cli.v2_runtime.phase_g_artifacts import persist_and_verify


class Token:
    def access_token(self) -> str:
        return "test-token"


def _adapter(tmp_path: Path, *, remote_body: bytes = b"clip") -> GoogleDriveStorageAdapter:
    file_id = "drive-file-1"
    folder_id = "approved-folder"
    stable_key: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal stable_key
        if request.url.path.endswith(f"/files/{folder_id}"):
            return httpx.Response(
                200,
                json={
                    "id": folder_id,
                    "name": "Job_Artifacts",
                    "mimeType": "application/vnd.google-apps.folder",
                    "capabilities": {"canAddChildren": True},
                },
            )
        if request.url.path.endswith("/files"):
            if request.method == "POST":
                match = re.search(rb"gflowLogicalKey[^0-9a-f]+([0-9a-f]{64})", request.content)
                stable_key = match.group(1).decode() if match else None
                if not stable_key:
                    return httpx.Response(400)
                return httpx.Response(
                    200,
                    json={
                        "id": file_id,
                        "name": "clip.mp4",
                        "mimeType": "video/mp4",
                        "size": str(len(b"clip")),
                        "parents": [folder_id],
                        "appProperties": {"gflowLogicalKey": stable_key},
                    },
                )
            found = []
            if stable_key:
                found = [
                    {
                        "id": file_id,
                        "name": "clip.mp4",
                        "mimeType": "video/mp4",
                        "size": str(len(b"clip")),
                        "parents": [folder_id],
                        "appProperties": {"gflowLogicalKey": stable_key},
                    }
                ]
            return httpx.Response(200, json={"files": found})
        if request.url.path.endswith(f"/files/{file_id}"):
            if request.url.params.get("alt") == "media":
                return httpx.Response(200, content=remote_body)
            return httpx.Response(
                200,
                json={
                    "id": file_id,
                    "name": "clip.mp4",
                    "mimeType": "video/mp4",
                    "size": str(len(b"clip")),
                    "parents": [folder_id],
                    "md5Checksum": "unused",
                },
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return GoogleDriveStorageAdapter(Token(), approved_folder_id=folder_id, client=client)


def test_upload_verify_and_cleanup_requires_exact_redownload_hash(tmp_path: Path) -> None:
    artifact = tmp_path / "clip.mp4"
    artifact.write_bytes(b"clip")
    adapter = _adapter(tmp_path)
    result = persist_and_verify(
        adapter,
        artifact,
        "scene-job:SJB_000001",
        verification_temp_dir=tmp_path / "verify",
        cleanup_local=True,
    )

    assert result.verification.remote_verified
    assert result.verification.size_bytes == 4
    assert result.verification.local_sha256 == result.verification.remote_sha256
    assert not artifact.exists()


def test_mismatched_remote_download_keeps_local_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "clip.mp4"
    artifact.write_bytes(b"clip")
    adapter = _adapter(tmp_path, remote_body=b"evil")

    with pytest.raises(AssetVerificationError, match="SHA-256"):
        persist_and_verify(
            adapter,
            artifact,
            "scene-job:SJB_000002",
            verification_temp_dir=tmp_path / "verify",
            cleanup_local=True,
        )

    assert artifact.is_file()


def test_upload_rejects_changed_folder_capability(tmp_path: Path) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"clip")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "approved-folder",
                "name": "Job_Artifacts",
                "mimeType": "application/vnd.google-apps.folder",
                "capabilities": {"canAddChildren": False},
            },
        )

    adapter = GoogleDriveStorageAdapter(
        Token(),
        approved_folder_id="approved-folder",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(AdapterUnavailableError, match="not writable"):
        adapter.upload_asset(source, "job-1")

"""Local and remote asset verification helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .assets import AssetReference
from .errors import AssetVerificationError
from .storage import AssetMetadata


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_local_file(path: Path, reference: AssetReference) -> AssetMetadata:
    if not path.is_file():
        raise AssetVerificationError(f"asset is missing or not a file: {path}")
    size = path.stat().st_size
    digest = sha256_file(path)
    if reference.size_bytes is not None and size != reference.size_bytes:
        raise AssetVerificationError(f"size mismatch: expected {reference.size_bytes}, got {size}")
    if reference.checksum_sha256 and digest.lower() != reference.checksum_sha256.lower():
        raise AssetVerificationError("SHA-256 checksum mismatch")
    return AssetMetadata(True, size, digest, reference.mime_type)


def verify_remote_metadata(reference: AssetReference, metadata: AssetMetadata) -> None:
    if not metadata.exists:
        raise AssetVerificationError("remote asset does not exist")
    if reference.size_bytes is not None and metadata.size_bytes != reference.size_bytes:
        raise AssetVerificationError("remote asset size does not match expected size")
    if reference.checksum_sha256 and metadata.checksum_sha256:
        if metadata.checksum_sha256.lower() != reference.checksum_sha256.lower():
            raise AssetVerificationError("remote asset checksum does not match expected checksum")

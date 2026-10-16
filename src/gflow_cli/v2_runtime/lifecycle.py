"""Remote verification gates deletion of a local temporary file."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .assets import AssetReference
from .storage import AssetMetadata
from .verification import verify_remote_metadata


class UploadVerificationStorage(Protocol):
    """The minimal storage surface needed before deleting local media."""

    def upload_asset(self, source: Path, logical_key: str) -> AssetReference: ...
    def get_metadata(self, reference: AssetReference) -> AssetMetadata: ...


def upload_verify_then_cleanup(
    storage: UploadVerificationStorage, local_file: Path, logical_key: str
) -> AssetReference:
    """Upload, read remote metadata, verify, then and only then delete the local file."""
    reference = storage.upload_asset(local_file, logical_key)
    remote_metadata = storage.get_metadata(reference)
    verify_remote_metadata(reference, remote_metadata)
    local_file.unlink()
    return reference

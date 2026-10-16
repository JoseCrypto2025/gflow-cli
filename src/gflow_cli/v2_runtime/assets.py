"""Logical, provider-neutral references to stored assets."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AssetReference:
    storage_provider: str
    asset_id: str
    logical_key: str
    remote_file_id: str | None = None
    checksum_sha256: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None

    def __post_init__(self) -> None:
        if (
            not self.storage_provider.strip()
            or not self.asset_id.strip()
            or not self.logical_key.strip()
        ):
            raise ValueError("storage_provider, asset_id, and logical_key are required")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")

"""Storage adapter contracts. No real Drive credentials or network calls are implemented."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol

from .assets import AssetReference
from .errors import AdapterUnavailableError


@dataclass(frozen=True, slots=True)
class AssetMetadata:
    exists: bool
    size_bytes: int | None = None
    checksum_sha256: str | None = None
    mime_type: str | None = None


class StorageProvider(Protocol):
    def resolve_asset(self, reference: AssetReference) -> AssetMetadata: ...
    def download_asset(self, reference: AssetReference, destination: Path) -> Path: ...
    def upload_asset(self, source: Path, logical_key: str) -> AssetReference: ...
    def verify_remote_asset(self, reference: AssetReference) -> AssetMetadata: ...
    def exists(self, reference: AssetReference) -> bool: ...
    def get_metadata(self, reference: AssetReference) -> AssetMetadata: ...


class GoogleDriveStorageProvider:
    """Documented adapter boundary only; auth and upload remain unimplemented."""

    def _unavailable(self) -> NoReturn:
        raise AdapterUnavailableError(
            "Google Drive adapter is a contract placeholder; no I/O is enabled"
        )

    def resolve_asset(self, reference: AssetReference) -> AssetMetadata:
        self._unavailable()

    def download_asset(self, reference: AssetReference, destination: Path) -> Path:
        self._unavailable()

    def upload_asset(self, source: Path, logical_key: str) -> AssetReference:
        self._unavailable()

    def exists(self, reference: AssetReference) -> bool:
        self._unavailable()

    def get_metadata(self, reference: AssetReference) -> AssetMetadata:
        self._unavailable()

    def verify_remote_asset(self, reference: AssetReference) -> AssetMetadata:
        self._unavailable()

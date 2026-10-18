"""Injected, idempotency-aware Google Drive storage adapter for V2 artifacts."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from pathlib import Path
from typing import cast
from urllib.parse import quote

import httpx

from .assets import AssetReference
from .errors import AdapterUnavailableError, AssetVerificationError
from .google_sheets import AccessTokenProvider
from .storage import AssetMetadata

_DRIVE_API = "https://www.googleapis.com/drive/v3"
_DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
_FOLDER_MIME = "application/vnd.google-apps.folder"


class GoogleDriveStorageAdapter:
    """Upload/retrieve exact artifacts in one previously verified output folder.

    The caller must re-resolve the current approved folder ID from trusted
    configuration before construction. The adapter rechecks name, type and
    write capability before every upload. It never deletes remote files.
    """

    def __init__(
        self,
        token_provider: AccessTokenProvider,
        *,
        approved_folder_id: str,
        expected_folder_name: str = "Job_Artifacts",
        client: httpx.Client | None = None,
    ) -> None:
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", approved_folder_id)
            or not expected_folder_name.strip()
        ):
            raise ValueError("an approved folder identity is required")
        self._token_provider = token_provider
        self._folder_id = approved_folder_id
        self._folder_name = expected_folder_name
        self._client = client or httpx.Client(timeout=60.0, follow_redirects=False)
        self._owns_client = client is None

    def upload_asset(self, source: Path, logical_key: str) -> AssetReference:
        if not source.is_file() or source.is_symlink() or source.stat().st_size <= 0:
            raise AssetVerificationError("local artifact is missing or unsafe")
        if not logical_key or len(logical_key) > 256:
            raise ValueError("logical artifact key is invalid")
        folder = self._folder()
        stable_key = hashlib.sha256(logical_key.encode("utf-8")).hexdigest()
        existing = self._find_by_key(stable_key)
        if len(existing) > 1:
            raise AdapterUnavailableError("multiple remote artifacts match the stable key")
        if existing:
            candidate = existing[0]
            if candidate.get("size") != str(source.stat().st_size):
                raise AdapterUnavailableError("existing remote artifact size conflicts")
            return self._reference(candidate, logical_key)

        mime_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        metadata = {
            "name": source.name,
            "mimeType": mime_type,
            "parents": [folder["id"]],
            "appProperties": {"gflowLogicalKey": stable_key},
        }
        try:
            with source.open("rb") as media:
                response = self._client.post(
                    _DRIVE_UPLOAD,
                    params={
                        "uploadType": "multipart",
                        "fields": "id,name,mimeType,size,parents,appProperties",
                    },
                    headers={"Authorization": f"Bearer {self._token_provider.access_token()}"},
                    files={
                        "metadata": (
                            None,
                            json.dumps(metadata, separators=(",", ":")),
                            "application/json; charset=UTF-8",
                        ),
                        "media": (source.name, media, mime_type),
                    },
                )
                response.raise_for_status()
                payload: object = response.json()
        except Exception as exc:  # noqa: BLE001 — no OAuth or response detail is safe to echo
            reconciled = self._find_by_key(stable_key)
            if len(reconciled) == 1 and reconciled[0].get("size") == str(source.stat().st_size):
                return self._reference(reconciled[0], logical_key)
            raise AdapterUnavailableError("Drive upload acknowledgement is unresolved") from exc
        if not isinstance(payload, dict):
            raise AdapterUnavailableError("Drive upload response is invalid")
        remote = cast(dict[str, object], payload)
        app_properties = remote.get("appProperties")
        if (
            remote.get("parents") != [folder["id"]]
            or not isinstance(app_properties, dict)
            or cast(dict[str, object], app_properties).get("gflowLogicalKey") != stable_key
        ):
            raise AdapterUnavailableError("Drive upload identity did not match the approved folder")
        return self._reference(remote, logical_key)

    def get_metadata(self, reference: AssetReference) -> AssetMetadata:
        payload = self._get_file(
            reference.asset_id, fields="id,name,mimeType,size,parents,md5Checksum"
        )
        if payload.get("parents") is None or self._folder_id not in cast(
            list[str], payload["parents"]
        ):
            raise AssetVerificationError("remote artifact is outside the approved folder")
        size = payload.get("size")
        parsed_size = int(cast(str, size)) if size is not None else None
        return AssetMetadata(
            exists=True,
            size_bytes=parsed_size,
            checksum_sha256=None,
            mime_type=cast(str | None, payload.get("mimeType")),
        )

    def verify_remote_asset(self, reference: AssetReference) -> AssetMetadata:
        metadata = self.get_metadata(reference)
        if not metadata.exists or metadata.size_bytes != reference.size_bytes:
            raise AssetVerificationError("remote artifact metadata differs from upload")
        if reference.mime_type and metadata.mime_type != reference.mime_type:
            raise AssetVerificationError("remote artifact media type differs from upload")
        return metadata

    def download_asset(self, reference: AssetReference, destination: Path) -> Path:
        if destination.exists() or destination.is_symlink():
            raise AssetVerificationError("remote verification destination must be unused")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.download")
        if temporary.exists() or temporary.is_symlink():
            raise AssetVerificationError("remote verification temporary path is occupied")
        headers = {"Authorization": f"Bearer {self._token_provider.access_token()}"}
        try:
            with self._client.stream(
                "GET",
                f"{_DRIVE_API}/files/{quote(reference.asset_id, safe='')}",
                params={"alt": "media"},
                headers=headers,
            ) as response:
                response.raise_for_status()
                with temporary.open("xb") as target:
                    for chunk in response.iter_bytes(1024 * 1024):
                        target.write(chunk)
                    target.flush()
            temporary.replace(destination)
        except Exception as exc:  # noqa: BLE001 — suppress HTTP/auth detail
            temporary.unlink(missing_ok=True)
            raise AdapterUnavailableError("Drive artifact download failed") from exc
        return destination

    def resolve_asset(self, reference: AssetReference) -> AssetMetadata:
        return self.get_metadata(reference)

    def exists(self, reference: AssetReference) -> bool:
        try:
            return self.get_metadata(reference).exists
        except (AdapterUnavailableError, AssetVerificationError):
            return False

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _folder(self) -> dict[str, object]:
        payload = self._get_file(
            self._folder_id, fields="id,name,mimeType,capabilities(canAddChildren)"
        )
        capabilities = payload.get("capabilities")
        if (
            payload.get("id") != self._folder_id
            or payload.get("name") != self._folder_name
            or payload.get("mimeType") != _FOLDER_MIME
            or not isinstance(capabilities, dict)
            or cast(dict[str, object], capabilities).get("canAddChildren") is not True
        ):
            raise AdapterUnavailableError("approved Drive folder is not writable or has changed")
        return payload

    def _find_by_key(self, stable_key: str) -> list[dict[str, object]]:
        query = (
            f"'{self._folder_id}' in parents and trashed = false and "
            "appProperties has { key='gflowLogicalKey' and "
            f"value='{stable_key}' }}"
        )
        try:
            response = self._client.get(
                f"{_DRIVE_API}/files",
                params={
                    "q": query,
                    "fields": "files(id,name,mimeType,size,parents,appProperties)",
                    "pageSize": 10,
                },
                headers={"Authorization": f"Bearer {self._token_provider.access_token()}"},
            )
            response.raise_for_status()
            value: object = response.json()
        except Exception as exc:  # noqa: BLE001
            raise AdapterUnavailableError("Drive idempotency lookup failed") from exc
        if not isinstance(value, dict):
            raise AdapterUnavailableError("Drive idempotency lookup response is invalid")
        payload = cast(dict[str, object], value)
        files = payload.get("files", [])
        if not isinstance(files, list):
            raise AdapterUnavailableError("Drive idempotency lookup response is invalid")
        return cast(list[dict[str, object]], files)

    def _get_file(self, file_id: str, *, fields: str) -> dict[str, object]:
        try:
            response = self._client.get(
                f"{_DRIVE_API}/files/{quote(file_id, safe='')}",
                params={"fields": fields},
                headers={"Authorization": f"Bearer {self._token_provider.access_token()}"},
            )
            response.raise_for_status()
            value: object = response.json()
        except Exception as exc:  # noqa: BLE001
            raise AdapterUnavailableError("Drive metadata read failed") from exc
        if not isinstance(value, dict):
            raise AdapterUnavailableError("Drive metadata response is invalid")
        return cast(dict[str, object], value)

    def _reference(self, value: dict[str, object], logical_key: str) -> AssetReference:
        file_id = value.get("id")
        name = value.get("name")
        size = value.get("size")
        mime_type = value.get("mimeType")
        if not isinstance(file_id, str) or not file_id or not isinstance(name, str):
            raise AdapterUnavailableError("Drive upload did not return a stable file identity")
        if not isinstance(size, str) or not size.isdecimal():
            raise AdapterUnavailableError("Drive upload did not return an exact size")
        return AssetReference(
            "GOOGLE_DRIVE",
            file_id,
            logical_key,
            remote_file_id=file_id,
            mime_type=mime_type if isinstance(mime_type, str) else None,
            size_bytes=int(size),
        )

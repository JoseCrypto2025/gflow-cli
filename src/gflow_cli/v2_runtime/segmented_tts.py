"""Authenticated adapter for the existing segmented-v2 TTS service."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import httpx

from .media_audio import AudioInfo, inspect_audio
from .phase_g_artifacts import sha256_file
from .tts import TTSJobRequest, build_segmented_tts_payload


class TTSProviderError(ValueError):
    """Sanitized provider failure; never includes response bodies or credentials."""


class TTSProviderAuthError(TTSProviderError):
    """The provider definitively rejected the request credentials."""


@dataclass(frozen=True, slots=True)
class SegmentedTTSConfig:
    base_url: str
    api_key: str = field(repr=False)
    output_root: Path
    timeout_sec: float = 20.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("TTS base URL must be an origin")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("TTS base URL port is invalid") from exc
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or port != 8011:
            raise ValueError("TTS API credentials may be sent only to the loopback service")
        if (
            not self.api_key.strip()
            or not self.output_root.is_absolute()
            or self.output_root.is_symlink()
        ):
            raise ValueError("TTS API key and absolute output root are required")
        if not 1 <= self.timeout_sec <= 120:
            raise ValueError("TTS timeout is outside supported bounds")

    @classmethod
    def from_environment(cls) -> SegmentedTTSConfig:
        return cls(
            base_url=os.environ.get("GFLOW_TTS_BASE_URL", "http://127.0.0.1:8011"),
            api_key=os.environ.get("TTS_API_KEY", ""),
            output_root=Path(os.environ.get("TTS_OUTPUT_ROOT", "")),
            timeout_sec=float(os.environ.get("GFLOW_TTS_TIMEOUT_SEC", "20")),
        )


@dataclass(frozen=True, slots=True)
class TTSProviderResult:
    provider_execution_id: str
    audio_path: Path
    audio: AudioInfo
    provider_status: str


class SegmentedTTSProvider:
    """Single-submit TTS client with local, contained artifact retrieval."""

    def __init__(self, config: SegmentedTTSConfig, *, client: httpx.Client | None = None):
        self._config = config
        self._client = client or httpx.Client(
            timeout=config.timeout_sec,
            follow_redirects=False,
            trust_env=False,
        )
        self._owns_client = client is None

    def health(self) -> bool:
        try:
            response = self._client.get(f"{self._config.base_url}/health")
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise TTSProviderError("TTS provider health check failed") from exc
        if not isinstance(payload, dict) or cast(dict[str, object], payload).get("status") != "ok":
            raise TTSProviderError("TTS provider health response is invalid")
        return True

    def synthesize(
        self,
        request: TTSJobRequest,
        *,
        tts_job_id: str,
        destination: Path,
        rate: str = "+0%",
        pitch: str = "+0st",
        volume: str = "medium",
    ) -> TTSProviderResult:
        if destination.exists() or destination.is_symlink():
            raise TTSProviderError("TTS destination must be unused")
        payload = build_segmented_tts_payload(
            content_id=tts_job_id,
            text=request.text,
            voice=request.voice_name,
            rate=rate,
            pitch=pitch,
            volume=volume,
        )
        try:
            response = self._client.post(
                f"{self._config.base_url}/tts/segmented-v2/create",
                headers={"x-api-key": self._config.api_key},
                json=payload,
            )
        except Exception as exc:  # noqa: BLE001 — do not expose URL/header/body details
            raise TTSProviderError(
                "TTS submit outcome is uncertain; do not resubmit this attempt"
            ) from exc
        if response.status_code in {401, 403}:
            raise TTSProviderAuthError("TTS provider rejected authentication")
        try:
            response.raise_for_status()
            result: object = response.json()
        except Exception as exc:  # noqa: BLE001 — do not expose URL/header/body details
            raise TTSProviderError(
                "TTS submit outcome is uncertain; do not resubmit this attempt"
            ) from exc
        if not isinstance(result, dict):
            raise TTSProviderError("TTS provider response shape is invalid")
        result = cast(dict[str, object], result)
        status = str(result.get("status", "")).strip().upper()
        execution_id = result.get("job_id")
        returned_content_id = result.get("content_id")
        path_value = result.get("audio_file_path")
        if status not in {"COMPLETED", "DONE", "SUCCESS"}:
            raise TTSProviderError("TTS provider did not return a completed result")
        if not isinstance(execution_id, str) or not execution_id.strip():
            raise TTSProviderError("TTS provider did not return a stable execution ID")
        safe_job_id = re.sub(r"[^A-Za-z0-9_-]+", "_", tts_job_id)[:120].strip("_")
        expected_prefix = f"SEG2_TTS_{safe_job_id}_"
        if (
            returned_content_id != tts_job_id
            or not execution_id.startswith(expected_prefix)
            or not execution_id[len(expected_prefix) :]
        ):
            raise TTSProviderError("TTS result is not bound to the submitted job ID")
        if not isinstance(path_value, str) or not path_value.strip():
            raise TTSProviderError("TTS provider did not return a local audio artifact")
        source = Path(path_value)
        try:
            root = self._config.output_root.resolve(strict=True)
            if not source.is_absolute():
                raise ValueError("provider artifact path must be absolute")
            resolved = source.resolve(strict=True)
            resolved.relative_to(root)
            if resolved.parent != root:
                raise ValueError("provider artifact must be a direct child of the output root")
        except (OSError, RuntimeError, ValueError) as exc:
            raise TTSProviderError(
                "TTS audio artifact is outside the approved output root"
            ) from exc
        if (
            source.is_symlink()
            or not resolved.is_file()
            or not source.name.startswith(expected_prefix)
            or source.suffix.lower() != ".mp3"
        ):
            raise TTSProviderError("TTS audio artifact is missing, unsafe, or unbound")
        try:
            audio = inspect_audio(resolved, max_duration_sec=request.target_duration_sec)
            source_size = resolved.stat().st_size
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(resolved, destination)
            if (
                destination.stat().st_size != source_size
                or sha256_file(destination) != audio.sha256
            ):
                raise TTSProviderError("copied TTS artifact differs from provider output")
        except Exception as exc:  # noqa: BLE001
            destination.unlink(missing_ok=True)
            raise TTSProviderError("TTS output failed audio validation") from exc
        return TTSProviderResult(execution_id, destination, audio, status)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

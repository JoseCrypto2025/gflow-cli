"""Provider-neutral TTS job values and exact request construction."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TTSJobRequest:
    production_job_id: str
    dialogue_line_id: str
    attempt_no: int
    text: str = field(repr=False)
    voice_id: str
    voice_name: str
    locale: str
    provider_account_id: str
    model_id: str
    target_duration_sec: float
    rate: str = "+0%"
    pitch: str = "+0st"
    volume: str = "medium"

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.production_job_id,
                self.dialogue_line_id,
                self.voice_id,
                self.voice_name,
                self.locale,
                self.provider_account_id,
                self.model_id,
                self.rate,
                self.pitch,
                self.volume,
            )
        ):
            raise ValueError("TTS job identity and resolved provider/voice are required")
        if (
            isinstance(self.attempt_no, bool)
            or self.attempt_no < 1
            or not self.text.strip()
            or not math.isfinite(self.target_duration_sec)
            or self.target_duration_sec <= 0
        ):
            raise ValueError("TTS attempt, exact text, and target duration are required")

    @property
    def input_text_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def synthesis_config_sha256(self) -> str:
        payload = json.dumps(
            [
                "TTS_SYNTHESIS_CONFIG_V1",
                self.voice_id,
                self.voice_name,
                self.locale,
                self.provider_account_id,
                self.model_id,
                self.target_duration_sec,
                self.rate,
                self.pitch,
                self.volume,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TTSJobClaim:
    tts_job_id: str
    request: TTSJobRequest
    job_status: str


@dataclass(frozen=True, slots=True)
class TTSArtifact:
    provider_execution_id: str
    remote_audio_uri: str
    remote_file_id: str
    audio_sha256: str
    audio_size_bytes: int
    audio_duration_ms: int


class TTSJobStore(Protocol):
    def claim(self, request: TTSJobRequest, occurred_at: str) -> TTSJobClaim: ...

    def completed_artifact(self, tts_job_id: str) -> TTSArtifact | None: ...


def build_segmented_tts_payload(
    *,
    content_id: str,
    text: str,
    voice: str,
    rate: str,
    pitch: str,
    volume: str,
) -> dict[str, object]:
    """Build the legacy service's narrow V2 payload without rewriting text."""
    if not content_id.strip() or not text.strip() or not voice.strip():
        raise ValueError("content ID, approved text, and explicit voice are required")
    return {
        "content_id": content_id,
        "text": text,
        "voice": voice,
        "rate": rate,
        "pitch": pitch,
        "volume": volume,
    }

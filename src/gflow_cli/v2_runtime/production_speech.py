"""TTS-to-merge adapter used by the production composition layer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .google_drive_storage import GoogleDriveStorageAdapter
from .media_merge import MergeResult
from .production_jobs import SceneJob
from .production_orchestrator import GeneratedSceneAsset
from .segmented_tts import SegmentedTTSProvider
from .tts import TTSJobRequest
from .tts_pipeline import (
    TTSAudioSegment,
    TTSDrivePort,
    TTSExecutionResult,
    TTSLifecycleStore,
    execute_tts_once,
    merge_verified_tts_segments,
)
from .workspace import JobWorkspace


@dataclass(frozen=True, slots=True)
class ResolvedTTSVoice:
    voice_id: str
    provider_voice_id: str
    locale: str
    provider_account_id: str
    model_id: str
    rate: str = "+0%"
    pitch: str = "+0st"
    volume: str = "medium"


class TTSVoiceResolver(Protocol):
    """Resolve a line from the caller's fresh approved voice/provider snapshot."""

    def resolve(
        self, line: Mapping[str, object], *, scene: Mapping[str, object]
    ) -> ResolvedTTSVoice: ...


class TTSMergeSpeechFinalizer:
    """Run existing single-submit TTS jobs and the verified segment merger."""

    def __init__(
        self,
        store: TTSLifecycleStore,
        provider: SegmentedTTSProvider,
        drive: GoogleDriveStorageAdapter | TTSDrivePort,
        voice_resolver: TTSVoiceResolver,
        *,
        temp_root: Path,
    ) -> None:
        self._store = store
        self._provider = provider
        self._drive = drive
        self._voice_resolver = voice_resolver
        self._temp_root = temp_root

    def merge_approved_dialogue(
        self,
        scene_job: SceneJob,
        generated_asset: GeneratedSceneAsset,
        scene: Mapping[str, object],
        dialogue_rows: tuple[Mapping[str, object], ...],
        workspace: JobWorkspace,
        *,
        expected_duration_sec: float,
        expected_aspect_ratio: str,
        occurred_at: str,
    ) -> MergeResult:
        scene_id = scene.get("Scene_ID")
        lines = [
            line
            for line in dialogue_rows
            if line.get("Scene_ID") == scene_id and str(line.get("Status", "")).upper() == "ACTIVE"
        ]
        numbered: list[tuple[int, Mapping[str, object]]] = []
        for line in lines:
            raw_number = line.get("Line_No")
            if isinstance(raw_number, bool) or not isinstance(raw_number, (int, str)):
                raise ValueError("dialogue Line_No must be a positive integer")
            try:
                line_number = int(raw_number)
            except ValueError as exc:
                raise ValueError("dialogue Line_No must be a positive integer") from exc
            if line_number <= 0:
                raise ValueError("dialogue Line_No must be a positive integer")
            numbered.append((line_number, line))
        numbered.sort(key=lambda item: item[0])
        if not numbered or len({number for number, _ in numbered}) != len(numbered):
            raise ValueError("speech finalization requires unique active dialogue lines")

        segments: list[TTSAudioSegment] = []
        for _, line in numbered:
            line_id = line.get("Dialogue_Line_ID")
            text = line.get("Dialogue_Text")
            if not isinstance(line_id, str) or not line_id.strip():
                raise ValueError("dialogue line identity is required")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("approved dialogue text is required")
            voice = self._voice_resolver.resolve(line, scene=scene)
            override = line.get("Voice_ID_Override")
            if override and override != voice.voice_id:
                raise ValueError("resolved voice does not match the approved line override")
            request = TTSJobRequest(
                production_job_id=scene_job.production_job_id,
                dialogue_line_id=line_id,
                attempt_no=scene_job.attempt_no,
                text=text,
                voice_id=voice.voice_id,
                voice_name=voice.provider_voice_id,
                locale=voice.locale,
                provider_account_id=voice.provider_account_id,
                model_id=voice.model_id,
                target_duration_sec=expected_duration_sec,
                rate=voice.rate,
                pitch=voice.pitch,
                volume=voice.volume,
            )
            result: TTSExecutionResult = execute_tts_once(
                self._store,
                self._provider,
                self._drive,
                request,
                workspace=workspace,
                temp_root=self._temp_root,
                occurred_at=occurred_at,
            )
            raw_pause = line.get("Pause_After_Ms")
            if raw_pause in (None, ""):
                pause_after_ms = 0
            elif isinstance(raw_pause, bool) or not isinstance(raw_pause, (int, str)):
                raise ValueError("Pause_After_Ms must be a nonnegative integer")
            else:
                pause_after_ms = int(raw_pause)
            segments.append(TTSAudioSegment(result, pause_after_ms))

        merged_path = workspace.directories["validated"] / (
            f"{scene_job.scene_job_id}-attempt-{scene_job.attempt_no}-merged.mp4"
        )
        return merge_verified_tts_segments(
            self._store,
            self._drive,
            tuple(segments),
            generated_asset.video_path,
            merged_path,
            workspace=workspace,
            temp_root=self._temp_root,
            expected_duration_sec=expected_duration_sec,
            expected_aspect_ratio=expected_aspect_ratio,
        )

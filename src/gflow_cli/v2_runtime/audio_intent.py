"""Fail-closed audio intent resolution from approved scene evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum


class AudioIntentError(ValueError):
    """Approved scene evidence does not determine a safe audio path."""


class AudioIntent(StrEnum):
    NO_AUDIO = "NO_AUDIO"
    TTS_REQUIRED = "TTS_REQUIRED"


_SPEECH_MODES = frozenset({"DIALOGUE", "NARRATION", "MIXED"})


def resolve_audio_intent(
    scene: Mapping[str, object],
    dialogue_rows: Sequence[Mapping[str, object]],
    *,
    approved_script_audio_required: bool | None = None,
    voice_mapping_resolved: bool = False,
) -> AudioIntent:
    """Resolve audio only from locked scene/script/dialogue/voice evidence.

    Prompt text and visual scene metadata are intentionally not inspected.
    An absent or contradictory signal holds rather than defaulting to silence.
    """
    scene_id = scene.get("Scene_ID")
    if not isinstance(scene_id, str) or not scene_id.strip():
        raise AudioIntentError("scene identity is required for audio resolution")
    if scene.get("Scene_Definition_Status") != "LOCKED":
        raise AudioIntentError("scene definition must be LOCKED before audio resolution")

    active_lines = [
        row
        for row in dialogue_rows
        if row.get("Scene_ID") == scene_id and str(row.get("Status", "")).upper() == "ACTIVE"
    ]
    raw_mode = scene.get("Dialogue_Mode")
    mode = raw_mode.strip().upper() if isinstance(raw_mode, str) else ""
    if mode == "NONE":
        if active_lines:
            raise AudioIntentError("Dialogue_Mode NONE conflicts with active dialogue lines")
        if approved_script_audio_required is True or voice_mapping_resolved:
            raise AudioIntentError("Dialogue_Mode NONE conflicts with approved speech evidence")
        return AudioIntent.NO_AUDIO
    if mode in _SPEECH_MODES:
        if not active_lines:
            raise AudioIntentError("speech Dialogue_Mode requires active scene dialogue lines")
        return AudioIntent.TTS_REQUIRED
    if mode:
        raise AudioIntentError("Dialogue_Mode is not supported")

    if active_lines:
        return AudioIntent.TTS_REQUIRED
    if approved_script_audio_required is True or voice_mapping_resolved:
        return AudioIntent.TTS_REQUIRED
    raise AudioIntentError("no authoritative audio intent is present")

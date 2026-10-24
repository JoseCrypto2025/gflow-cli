from __future__ import annotations

import pytest

from gflow_cli.v2_runtime.audio_intent import AudioIntent, AudioIntentError, resolve_audio_intent


def _scene(mode: str | None = "NONE") -> dict[str, object]:
    return {
        "Scene_ID": "SCN_000001",
        "Scene_Definition_Status": "LOCKED",
        "Dialogue_Mode": mode,
        "Scene_Summary": "A character gives a long spoken explanation.",
    }


def test_locked_none_scene_routes_to_no_audio_without_inventing_tts() -> None:
    assert resolve_audio_intent(_scene(), ()) is AudioIntent.NO_AUDIO


def test_no_audio_does_not_infer_speech_from_visual_or_narrative_text() -> None:
    assert resolve_audio_intent(_scene(), ()) is AudioIntent.NO_AUDIO


def test_none_mode_conflicts_with_active_dialogue_rows() -> None:
    line = {"Scene_ID": "SCN_000001", "Status": "ACTIVE", "Dialogue_Text": "Hello."}
    with pytest.raises(AudioIntentError, match="conflicts with active dialogue"):
        resolve_audio_intent(_scene(), (line,))


@pytest.mark.parametrize("mode", ["DIALOGUE", "NARRATION", "MIXED"])
def test_speech_mode_requires_active_dialogue_lines(mode: str) -> None:
    with pytest.raises(AudioIntentError, match="requires active scene dialogue"):
        resolve_audio_intent(_scene(mode), ())


def test_active_scene_dialogue_requires_tts_when_mode_is_unset() -> None:
    line = {"Scene_ID": "SCN_000001", "Status": "ACTIVE", "Dialogue_Text": "Hello."}
    assert resolve_audio_intent(_scene(None), (line,)) is AudioIntent.TTS_REQUIRED


def test_inactive_or_other_scene_dialogue_does_not_route_to_tts() -> None:
    rows = (
        {"Scene_ID": "SCN_000001", "Status": "INACTIVE"},
        {"Scene_ID": "SCN_000002", "Status": "ACTIVE"},
    )
    with pytest.raises(AudioIntentError, match="no authoritative audio intent"):
        resolve_audio_intent(_scene(None), rows)


def test_approved_script_relationship_can_resolve_audio_without_prompt_inference() -> None:
    assert (
        resolve_audio_intent(_scene(None), (), approved_script_audio_required=True)
        is AudioIntent.TTS_REQUIRED
    )


def test_voice_mapping_cannot_override_explicit_no_audio() -> None:
    with pytest.raises(AudioIntentError, match="conflicts with approved speech evidence"):
        resolve_audio_intent(_scene(), (), voice_mapping_resolved=True)

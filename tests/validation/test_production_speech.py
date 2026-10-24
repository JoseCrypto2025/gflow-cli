from __future__ import annotations

from pathlib import Path
from typing import cast

from gflow_cli.v2_runtime.production_jobs import SceneJob
from gflow_cli.v2_runtime.production_orchestrator import GeneratedSceneAsset
from gflow_cli.v2_runtime.production_speech import (
    ResolvedTTSVoice,
    TTSMergeSpeechFinalizer,
)
from gflow_cli.v2_runtime.tts import TTSJobClaim
from gflow_cli.v2_runtime.tts_pipeline import TTSExecutionResult
from gflow_cli.v2_runtime.workspace import create_workspace


class VoiceResolver:
    def resolve(self, line, *, scene):
        assert scene["Scene_ID"] == "SCN_000001"
        return ResolvedTTSVoice(
            str(line["Voice_ID_Override"]),
            "provider-voice-1",
            "th-TH",
            "PVA_TTS_0001",
            "MDL_TTS_0001",
            rate="-12%",
            pitch="+1st",
            volume="loud",
        )


def test_speech_finalizer_uses_approved_order_and_one_tts_attempt_per_line(
    tmp_path: Path, monkeypatch
) -> None:
    from gflow_cli.v2_runtime import production_speech as module

    workspace = create_workspace(tmp_path / "workspaces", "SJB_000001")
    scene_job = SceneJob("SJB_000001", "PRD_000001", "SPV_000001", 2, "RUNNING")
    asset = GeneratedSceneAsset(
        workspace.directories["generated"] / "video.mp4",
        "PVA_0001",
        "execution-1",
        "media-1",
        "D2_test-run-0001",
    )
    lines = (
        {
            "Scene_ID": "SCN_000001",
            "Dialogue_Line_ID": "SDL_000002",
            "Line_No": 2,
            "Dialogue_Text": "Second approved line.",
            "Voice_ID_Override": "VOI_0002",
            "Pause_After_Ms": 120,
            "Status": "ACTIVE",
        },
        {
            "Scene_ID": "SCN_000001",
            "Dialogue_Line_ID": "SDL_000001",
            "Line_No": 1,
            "Dialogue_Text": "First approved line.",
            "Voice_ID_Override": "VOI_0001",
            "Status": "ACTIVE",
        },
        {
            "Scene_ID": "SCN_000001",
            "Dialogue_Line_ID": "SDL_000003",
            "Line_No": 3,
            "Dialogue_Text": "Inactive line.",
            "Voice_ID_Override": "VOI_0003",
            "Status": "INACTIVE",
        },
    )
    tts_requests = []
    merged_call = {}

    def execute(_store, _provider, _drive, request, **kwargs):
        tts_requests.append((request, kwargs))
        claim = TTSJobClaim(f"TTS_{len(tts_requests):06d}", request, "COMPLETED")
        return TTSExecutionResult(claim, "tts-exec", "uri", "remote", "a" * 64, 512, 1000)

    def merge(_store, _drive, segments, video, output, **kwargs):
        merged_call.update(
            {
                "segments": segments,
                "video": video,
                "output": output,
                "kwargs": kwargs,
            }
        )
        return cast(module.MergeResult, object())

    monkeypatch.setattr(module, "execute_tts_once", execute)
    monkeypatch.setattr(module, "merge_verified_tts_segments", merge)
    finalizer = TTSMergeSpeechFinalizer(
        cast(module.TTSLifecycleStore, object()),
        cast(module.SegmentedTTSProvider, object()),
        cast(module.TTSDrivePort, object()),
        VoiceResolver(),
        temp_root=tmp_path / "workspaces",
    )

    result = finalizer.merge_approved_dialogue(
        scene_job,
        asset,
        {"Scene_ID": "SCN_000001"},
        lines,
        workspace,
        expected_duration_sec=8.0,
        expected_aspect_ratio="16:9",
        occurred_at="2026-10-24T00:00:00Z",
    )

    assert result is not None
    assert [item[0].dialogue_line_id for item in tts_requests] == [
        "SDL_000001",
        "SDL_000002",
    ]
    assert [item[0].attempt_no for item in tts_requests] == [2, 2]
    assert [item[0].provider_account_id for item in tts_requests] == [
        "PVA_TTS_0001",
        "PVA_TTS_0001",
    ]
    assert [(item[0].rate, item[0].pitch, item[0].volume) for item in tts_requests] == [
        ("-12%", "+1st", "loud"),
        ("-12%", "+1st", "loud"),
    ]
    assert all(item[0].synthesis_config_sha256 for item in tts_requests)
    assert [segment.pause_after_ms for segment in merged_call["segments"]] == [0, 120]
    assert merged_call["video"] == asset.video_path
    assert merged_call["kwargs"]["expected_duration_sec"] == 8.0

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import wave
from dataclasses import replace
from pathlib import Path

import httpx
import numpy as np
import pytest

from gflow_cli.v2_runtime.assets import AssetReference
from gflow_cli.v2_runtime.errors import UnsafePathError
from gflow_cli.v2_runtime.finalize_scene import (
    NoAudioFinalizationError,
    finalize_merged_output,
    finalize_no_audio,
)
from gflow_cli.v2_runtime.media_audio import (
    TTS_DURATION_MISMATCH,
    canonicalize_audio,
    inspect_audio,
    plan_audio_timeline,
)
from gflow_cli.v2_runtime.media_merge import merge_audio_video
from gflow_cli.v2_runtime.segmented_tts import (
    SegmentedTTSConfig,
    SegmentedTTSProvider,
    TTSProviderError,
)
from gflow_cli.v2_runtime.storage import AssetMetadata
from gflow_cli.v2_runtime.tts import (
    TTSArtifact,
    TTSJobClaim,
    TTSJobRequest,
    build_segmented_tts_payload,
)
from gflow_cli.v2_runtime.tts_pipeline import (
    TTSAudioSegment,
    TTSExecutionResult,
    execute_tts_once,
    merge_verified_tts_audio,
    merge_verified_tts_segments,
    recover_tts_cleanup,
)
from gflow_cli.v2_runtime.workspace import create_workspace, validate_workspace


def _wav(path: Path, duration: float, *, frequency: float = 440.0) -> Path:
    rate = 24_000
    samples = np.arange(round(rate * duration), dtype=np.float64)
    signal = 0.2 * np.sin(2 * math.pi * frequency * samples / rate)
    pcm = (signal * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(pcm.tobytes())
    return path


def _video(path: Path, *, duration: float = 2.0, size: tuple[int, int] = (160, 90)) -> Path:
    av = pytest.importorskip("av")
    width, height = size
    container = av.open(str(path), mode="w", format="mp4")
    stream = container.add_stream("mpeg4", rate=10)
    stream.width = width
    stream.height = height
    stream.pix_fmt = "yuv420p"
    frame_count = round(duration * 10)
    for index in range(frame_count):
        pixels = np.full((height, width, 3), index % 255, dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()
    return path


def _video_with_audio(path: Path, *, duration: float = 2.0) -> Path:
    video = _video(path.with_name(f"silent-{path.name}"), duration=duration)
    audio = _wav(path.with_name(f"audio-{path.stem}.wav"), duration)
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            str(path),
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    return path


class MemoryTTSJobStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, int], TTSJobClaim] = {}
        self.states: dict[str, dict[str, object]] = {}

    def claim(self, request: TTSJobRequest, occurred_at: str) -> TTSJobClaim:
        del occurred_at
        key = (request.production_job_id, request.dialogue_line_id, request.attempt_no)
        existing = self.rows.get(key)
        if existing is not None:
            if existing.request.input_text_sha256 != request.input_text_sha256:
                raise ValueError("claim identity conflicts with existing input")
            return replace(existing, job_status=str(self.states[existing.tts_job_id]["status"]))
        claim = TTSJobClaim(f"TTS_{len(self.rows) + 1:06d}", request, "PENDING")
        self.rows[key] = claim
        self.states[claim.tts_job_id] = {"status": "PENDING", "remote_verified": False}
        return claim

    def transition(
        self,
        tts_job_id: str,
        *,
        expected_status: str,
        next_status: str,
        occurred_at: str,
        error_code: str | None = None,
    ) -> object:
        del occurred_at, error_code
        state = self.states[tts_job_id]
        if state["status"] != expected_status:
            raise ValueError("state conflict")
        state["status"] = next_status
        return type("Row", (), {"job_status": next_status})()

    def record_remote_evidence(self, tts_job_id: str, **evidence: object) -> object:
        self.states[tts_job_id]["remote_verified"] = True
        self.states[tts_job_id]["artifact"] = evidence
        self.states[tts_job_id]["status"] = "REMOTE_VERIFYING"
        self.states[tts_job_id]["Remote_Verified"] = True
        self.states[tts_job_id]["Remote_File_ID"] = evidence["remote_file_id"]
        self.states[tts_job_id]["Local_Cleanup_Status"] = "RETAINED"
        return self.states[tts_job_id]

    def record_provider_receipt(
        self, tts_job_id: str, provider_execution_id: str, occurred_at: str
    ) -> object:
        del occurred_at
        self.states[tts_job_id]["provider_execution_id"] = provider_execution_id
        return self.states[tts_job_id]

    def completed_artifact(self, tts_job_id: str) -> TTSArtifact | None:
        raw = self.states[tts_job_id].get("artifact")
        if not isinstance(raw, dict):
            return None
        return TTSArtifact(
            str(raw["provider_execution_id"]),
            str(raw["remote_audio_uri"]),
            str(raw["remote_file_id"]),
            str(raw["audio_sha256"]),
            int(raw["audio_size_bytes"]),
            int(raw["audio_duration_ms"]),
        )

    def validate_completed_claim(self, claim: TTSJobClaim) -> TTSArtifact:
        key = (
            claim.request.production_job_id,
            claim.request.dialogue_line_id,
            claim.request.attempt_no,
        )
        persisted = self.rows.get(key)
        if (
            persisted is None
            or persisted.tts_job_id != claim.tts_job_id
            or persisted.request != claim.request
            or self.states[claim.tts_job_id].get("status") != "COMPLETED"
            or claim.job_status != "COMPLETED"
        ):
            raise ValueError("TTS claim is not the persisted completed attempt")
        artifact = self.completed_artifact(claim.tts_job_id)
        if artifact is None:
            raise ValueError("completed TTS artifact is missing")
        return artifact

    def mark_local_cleaned(self, tts_job_id: str, occurred_at: str) -> object:
        del occurred_at
        self.states[tts_job_id]["cleaned"] = True
        self.states[tts_job_id]["Local_Cleanup_Status"] = "CLEANED"
        return self.states[tts_job_id]

    def cleanup_state(self, tts_job_id: str) -> dict[str, object]:
        state = self.states[tts_job_id]
        return {**state, "Job_Status": state["status"]}

    def record_cleanup_failure(self, tts_job_id: str, reason_code: str, occurred_at: str) -> object:
        del occurred_at
        self.states[tts_job_id]["Local_Cleanup_Status"] = "CLEANUP_FAILED"
        self.states[tts_job_id]["cleanup_error"] = reason_code
        return self.states[tts_job_id]

    def complete(self, tts_job_id: str, occurred_at: str) -> object:
        del occurred_at
        self.states[tts_job_id]["status"] = "COMPLETED"
        return self.states[tts_job_id]


class FakeTTSProvider:
    def __init__(self, source: Path) -> None:
        self.source = source
        self.calls = 0

    def health(self) -> bool:
        return True

    def synthesize(
        self,
        request: TTSJobRequest,
        *,
        tts_job_id: str,
        destination: Path,
        rate: str,
        pitch: str,
        volume: str,
    ) -> object:
        from gflow_cli.v2_runtime.segmented_tts import TTSProviderResult

        del request, rate, pitch, volume
        self.calls += 1
        shutil.copyfile(self.source, destination)
        return TTSProviderResult(
            "provider-execution-1", destination, inspect_audio(destination), "COMPLETED"
        )


class FakeTTSDrive:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.remote = root / "remote.wav"

    def upload_asset(self, source: Path, logical_key: str) -> AssetReference:
        self.root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, self.remote)
        return AssetReference(
            "GOOGLE_DRIVE",
            "drive-audio-1",
            logical_key,
            remote_file_id="drive-audio-1",
            mime_type="audio/wav",
            size_bytes=self.remote.stat().st_size,
        )

    def verify_remote_asset(self, _reference: AssetReference) -> AssetMetadata:
        return AssetMetadata(True, self.remote.stat().st_size, None, "audio/wav")

    def download_asset(self, reference: AssetReference, destination: Path) -> Path:
        del reference
        shutil.copyfile(self.remote, destination)
        return destination


class MemoryFinalOutputStore:
    def __init__(self) -> None:
        import json

        schema = json.loads(
            (Path(__file__).parents[2] / "schemas/transaction/FINAL_OUTPUT.schema.json").read_text()
        )
        self.headers = tuple(field["field_name"] for field in schema["fields"])
        self.rows: list[dict[str, object]] = []

    def read_rows(self, tab: str) -> tuple[dict[str, object], ...]:
        assert tab == "FINAL_OUTPUT"
        return tuple(dict(row) for row in self.rows)

    def schema_headers(self, tab: str) -> tuple[str, ...]:
        assert tab == "FINAL_OUTPUT"
        return self.headers

    def append_if_unique(
        self,
        tab: str,
        row: dict[str, object],
        *,
        unique_constraints: tuple[tuple[str, ...], ...],
        identity_fields: tuple[str, ...],
    ) -> dict[str, object]:
        assert tab == "FINAL_OUTPUT"
        for existing in self.rows:
            if any(
                all(existing.get(field) == row.get(field) for field in fields)
                for fields in unique_constraints
            ):
                if any(existing.get(field) != row.get(field) for field in identity_fields):
                    raise ValueError("final output key conflicts")
                return dict(existing)
        self.rows.append(dict(row))
        return dict(row)


class FakeFinalOutputDrive(FakeTTSDrive):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.files: dict[str, Path] = {}

    def upload_asset(self, source: Path, logical_key: str) -> AssetReference:
        self.root.mkdir(parents=True, exist_ok=True)
        is_manifest = logical_key.startswith("final-output-manifest:")
        file_id = "drive-manifest-1" if is_manifest else "drive-final-1"
        self.remote = self.root / ("manifest.json" if is_manifest else "final.mp4")
        shutil.copyfile(source, self.remote)
        self.files[file_id] = self.remote
        return AssetReference(
            "GOOGLE_DRIVE",
            file_id,
            logical_key,
            remote_file_id=file_id,
            mime_type="application/json" if is_manifest else "video/mp4",
            size_bytes=self.remote.stat().st_size,
        )

    def verify_remote_asset(self, reference: AssetReference) -> AssetMetadata:
        remote = self.files[reference.asset_id]
        return AssetMetadata(True, remote.stat().st_size, None, reference.mime_type)

    def download_asset(self, reference: AssetReference, destination: Path) -> Path:
        shutil.copyfile(self.files[reference.asset_id], destination)
        return destination


def test_tts_request_builder_preserves_exact_text_and_explicit_voice() -> None:
    payload = build_segmented_tts_payload(
        content_id="TTS_000001",
        text="สวัสดีค่ะ — exact wording.",
        voice="th-TH-NiwatNeural",
        rate="+0%",
        pitch="+0st",
        volume="medium",
    )

    assert payload["content_id"] == "TTS_000001"
    assert payload["text"] == "สวัสดีค่ะ — exact wording."
    assert payload["voice"] == "th-TH-NiwatNeural"
    assert payload["rate"] == "+0%"


def test_tts_synthesis_config_fingerprint_binds_provider_voice_and_controls() -> None:
    request = TTSJobRequest(
        "PRD_000001",
        "SDL_000001",
        1,
        "Approved line.",
        "VOI_000001",
        "th-TH-NiwatNeural",
        "th-TH",
        "PVA_0001",
        "MDL_TTS_0001",
        2.0,
        rate="-12%",
    )

    assert (
        request.synthesis_config_sha256
        == TTSJobRequest(
            "PRD_000001",
            "SDL_000001",
            1,
            "Approved line.",
            "VOI_000001",
            "th-TH-NiwatNeural",
            "th-TH",
            "PVA_0001",
            "MDL_TTS_0001",
            2.0,
            rate="-12%",
        ).synthesis_config_sha256
    )
    assert (
        request.synthesis_config_sha256
        != replace(request, voice_name="th-TH-OtherNeural").synthesis_config_sha256
    )
    assert (
        request.synthesis_config_sha256 != replace(request, locale="en-US").synthesis_config_sha256
    )
    assert request.synthesis_config_sha256 != replace(request, rate="+0%").synthesis_config_sha256
    assert request.synthesis_config_sha256 != replace(request, pitch="+1st").synthesis_config_sha256
    assert (
        request.synthesis_config_sha256 != replace(request, volume="loud").synthesis_config_sha256
    )


def test_secret_and_dialogue_text_are_excluded_from_default_representations(
    tmp_path: Path,
) -> None:
    request = TTSJobRequest(
        "PRD_000001",
        "SDL_000001",
        1,
        "private dialogue phrase",
        "VOI_000001",
        "th-TH-NiwatNeural",
        "th-TH",
        "PVA_0001",
        "MDL_TTS_0001",
        2.0,
    )
    config = SegmentedTTSConfig("http://127.0.0.1:8011", "test-secret-key", tmp_path)

    assert "private dialogue phrase" not in repr(request)
    assert "test-secret-key" not in repr(config)


@pytest.mark.parametrize(
    "base_url",
    (
        "https://attacker.example",
        "https://localhost",
        "http://127.0.0.1",
        "http://user:password@127.0.0.1:8011",  # pragma: allowlist secret -- synthetic test fixture
        "http://127.0.0.1:8012",
    ),
)
def test_tts_key_origin_is_pinned_to_loopback_service(base_url: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="origin|credentials may be sent"):
        SegmentedTTSConfig(base_url, "key", tmp_path)


def test_workspace_validation_rejects_outside_and_symlink_roots(tmp_path: Path) -> None:
    temp_root = tmp_path / "runtime"
    workspace = create_workspace(temp_root, "PRD_000001")
    assert validate_workspace(temp_root, workspace)["audio"] == workspace.directories["audio"]

    outside_root = create_workspace(tmp_path / "outside", "PRD_000002")
    with pytest.raises(UnsafePathError, match="outside"):
        validate_workspace(temp_root, outside_root)

    symlink = temp_root / "linked-job"
    try:
        symlink.symlink_to(workspace.root, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    from gflow_cli.v2_runtime.workspace import JobWorkspace

    linked = JobWorkspace(
        "linked-job", symlink, {name: symlink / name for name in workspace.directories}
    )
    with pytest.raises(UnsafePathError, match="nonsymlink"):
        validate_workspace(temp_root, linked)


def test_tts_job_store_reuses_same_attempt_without_duplicate_claim() -> None:
    store = MemoryTTSJobStore()
    request = TTSJobRequest(
        production_job_id="PRD_000001",
        dialogue_line_id="SDL_000001",
        attempt_no=1,
        text="Exact approved line.",
        voice_id="VOI_000001",
        voice_name="th-TH-NiwatNeural",
        locale="th-TH",
        provider_account_id="PVA_0001",
        model_id="MDL_TTS_0001",
        target_duration_sec=3.0,
    )

    first = store.claim(request, "2026-10-24T00:00:00Z")
    replay = store.claim(request, "2026-10-24T00:01:00Z")

    assert replay == first
    assert len(store.rows) == 1
    assert (
        first.request.input_text_sha256 == hashlib.sha256(request.text.encode("utf-8")).hexdigest()
    )


def test_segmented_provider_uses_key_and_validates_completed_artifact(tmp_path: Path) -> None:
    source = _wav(tmp_path / "SEG2_TTS_TTS_000001_abcdef1234.mp3", 0.75)
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["api_key"] = request.headers.get("x-api-key")
        seen["payload"] = request.read().decode("utf-8")
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "job_id": "SEG2_TTS_TTS_000001_abcdef1234",
                "content_id": "TTS_000001",
                "audio_file_path": str(source),
            },
        )

    config = SegmentedTTSConfig("http://127.0.0.1:8011", "secret-value", tmp_path)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = SegmentedTTSProvider(config, client=client)
    request = TTSJobRequest(
        "PRD_000001",
        "SDL_000001",
        1,
        "Exact approved text.",
        "VOI_000001",
        "th-TH-NiwatNeural",
        "th-TH",
        "PVA_0001",
        "MDL_TTS_0001",
        2.0,
    )

    result = provider.synthesize(
        request,
        tts_job_id="TTS_000001",
        destination=tmp_path / "workspace" / "output.wav",
    )

    assert seen["path"] == "/tts/segmented-v2/create"
    assert seen["api_key"] == "secret-value"  # pragma: allowlist secret -- synthetic test fixture
    assert "Exact approved text." in str(seen["payload"])
    payload = json.loads(str(seen["payload"]))
    assert payload["rate"] == "+0%"
    assert payload["pitch"] == "+0Hz"
    assert payload["volume"] == "+10%"
    assert result.provider_execution_id == "SEG2_TTS_TTS_000001_abcdef1234"
    assert result.audio.duration_sec == pytest.approx(0.75, abs=0.05)
    assert result.audio_path.is_file()


@pytest.mark.parametrize(("control", "value"), [("pitch", "+1st"), ("volume", "loud")])
def test_segmented_provider_rejects_unmapped_controls_before_submit(
    tmp_path: Path, control: str, value: str
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    provider = SegmentedTTSProvider(
        SegmentedTTSConfig("http://127.0.0.1:8011", "key", tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    request = TTSJobRequest(
        "PRD_000001",
        "SDL_000001",
        1,
        "Exact approved text.",
        "VOI_000001",
        "th-TH-NiwatNeural",
        "th-TH",
        "PVA_0001",
        "MDL_TTS_0001",
        2.0,
    )

    with pytest.raises(TTSProviderError, match="outside the segmented service contract"):
        provider.synthesize(
            request,
            tts_job_id="TTS_000001",
            destination=tmp_path / "output.wav",
            **{control: value},
        )

    assert calls == 0


def test_segmented_provider_owned_client_ignores_proxy_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("NO_PROXY", "")
    provider = SegmentedTTSProvider(SegmentedTTSConfig("http://127.0.0.1:8011", "key", tmp_path))
    try:
        assert provider._client._trust_env is False
    finally:
        provider.close()


def test_segmented_provider_fails_closed_on_ambiguous_submit(tmp_path: Path) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"detail": "service unavailable"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = SegmentedTTSProvider(
        SegmentedTTSConfig("http://127.0.0.1:8011", "key", tmp_path),
        client=client,
    )
    request = TTSJobRequest(
        "PRD_000001",
        "SDL_000001",
        1,
        "Exact text",
        "VOI_000001",
        "voice",
        "th-TH",
        "PVA_0001",
        "MDL_TTS_0001",
        2.0,
    )

    with pytest.raises(TTSProviderError, match="do not resubmit"):
        provider.synthesize(request, tts_job_id="TTS_000001", destination=tmp_path / "out.wav")

    assert calls == 1


def test_segmented_provider_rejects_valid_audio_from_another_attempt(tmp_path: Path) -> None:
    source = _wav(tmp_path / "SEG2_TTS_TTS_999999_abcdef1234.mp3", 0.75)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "status": "completed",
                    "job_id": "SEG2_TTS_TTS_999999_abcdef1234",
                    "content_id": "TTS_999999",
                    "audio_file_path": str(source),
                },
            )
        )
    )
    provider = SegmentedTTSProvider(
        SegmentedTTSConfig("http://127.0.0.1:8011", "key", tmp_path), client=client
    )
    request = TTSJobRequest(
        "PRD_000001",
        "SDL_000001",
        1,
        "Approved text",
        "VOI_000001",
        "voice",
        "th-TH",
        "PVA_0001",
        "MDL_TTS_0001",
        2.0,
    )

    with pytest.raises(TTSProviderError, match="not bound to the submitted job ID"):
        provider.synthesize(request, tts_job_id="TTS_000001", destination=tmp_path / "out.wav")


def test_segmented_provider_rejects_unauthorized_operation_without_leaking_key(
    tmp_path: Path,
) -> None:
    from gflow_cli.v2_runtime.segmented_tts import TTSProviderAuthError

    secret = "test-only-secret"  # pragma: allowlist secret -- synthetic test fixture
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(401, json={"detail": secret}))
    )
    provider = SegmentedTTSProvider(
        SegmentedTTSConfig("http://127.0.0.1:8011", secret, tmp_path), client=client
    )
    request = TTSJobRequest(
        "PRD_000001",
        "SDL_000001",
        1,
        "Exact approved text.",
        "VOI_000001",
        "th-TH-NiwatNeural",
        "th-TH",
        "PVA_0001",
        "MDL_TTS_0001",
        2.0,
    )

    with pytest.raises(TTSProviderAuthError) as captured:
        provider.synthesize(request, tts_job_id="TTS_000001", destination=tmp_path / "out.wav")

    assert secret not in str(captured.value)
    assert not (tmp_path / "out.wav").exists()


def test_explicit_tts_auth_rejection_fails_attempt_without_resubmit(tmp_path: Path) -> None:
    from gflow_cli.v2_runtime.segmented_tts import TTSProviderAuthError

    class RejectedProvider:
        calls = 0

        def health(self) -> bool:
            return True

        def synthesize(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            raise TTSProviderAuthError("TTS provider rejected authentication")

    store = MemoryTTSJobStore()
    provider = RejectedProvider()
    temp_root = tmp_path / "workspaces"
    workspace = create_workspace(temp_root, "PRD_000001")
    request = TTSJobRequest(
        "PRD_000001",
        "SDL_000001",
        1,
        "Approved line.",
        "VOI_000001",
        "th-TH-NiwatNeural",
        "th-TH",
        "PVA_0001",
        "MDL_TTS_0001",
        2.0,
    )

    with pytest.raises(TTSProviderAuthError):
        execute_tts_once(
            store,  # type: ignore[arg-type]
            provider,  # type: ignore[arg-type]
            FakeTTSDrive(tmp_path / "drive"),  # type: ignore[arg-type]
            request,
            workspace=workspace,
            temp_root=temp_root,
            occurred_at="2026-10-24T00:00:00Z",
        )

    assert store.states["TTS_000001"]["status"] == "FAILED"
    assert provider.calls == 1
    with pytest.raises(ValueError, match="unresolved"):
        execute_tts_once(
            store,  # type: ignore[arg-type]
            provider,  # type: ignore[arg-type]
            FakeTTSDrive(tmp_path / "drive"),  # type: ignore[arg-type]
            request,
            workspace=workspace,
            temp_root=temp_root,
            occurred_at="2026-10-24T00:01:00Z",
        )
    assert provider.calls == 1


def test_segmented_provider_rejects_artifact_outside_approved_root(tmp_path: Path) -> None:
    outside = _wav(tmp_path / "outside.wav", 0.75)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "status": "completed",
                    "job_id": "SEG2_TTS_TTS_000001_abcdef1234",
                    "content_id": "TTS_000001",
                    "audio_file_path": str(outside),
                },
            )
        )
    )
    provider = SegmentedTTSProvider(
        SegmentedTTSConfig("http://127.0.0.1:8011", "key", allowed), client=client
    )
    request = TTSJobRequest(
        "PRD_000001",
        "SDL_000001",
        1,
        "Exact text",
        "VOI_000001",
        "voice",
        "th-TH",
        "PVA_0001",
        "MDL_TTS_0001",
        2.0,
    )

    with pytest.raises(TTSProviderError, match="outside"):
        provider.synthesize(request, tts_job_id="TTS_000001", destination=allowed / "out.wav")


def test_tts_lifecycle_verifies_drive_before_cleanup_and_never_resubmits(
    tmp_path: Path,
) -> None:
    provider_audio = _wav(tmp_path / "provider.wav", 0.75)
    provider = FakeTTSProvider(provider_audio)
    store = MemoryTTSJobStore()
    drive = FakeTTSDrive(tmp_path / "drive")
    temp_root = tmp_path / "workspaces"
    workspace = create_workspace(temp_root, "PRD_000001")
    request = TTSJobRequest(
        "PRD_000001",
        "SDL_000001",
        1,
        "Approved line.",
        "VOI_000001",
        "th-TH-NiwatNeural",
        "th-TH",
        "PVA_0001",
        "MDL_TTS_0001",
        2.0,
    )

    result = execute_tts_once(
        store,
        provider,  # type: ignore[arg-type]
        drive,  # type: ignore[arg-type]
        request,
        workspace=workspace,
        temp_root=temp_root,
        occurred_at="2026-10-24T00:00:00Z",
    )

    assert result.claim.tts_job_id == "TTS_000001"
    assert result.audio_duration_ms == 750
    assert provider.calls == 1
    assert store.states["TTS_000001"]["status"] == "COMPLETED"
    assert store.states["TTS_000001"]["remote_verified"] is True
    assert store.states["TTS_000001"]["cleaned"] is True
    assert not (workspace.directories["audio"] / "TTS_000001-provider.mp3").exists()
    assert not (workspace.directories["audio"] / "TTS_000001-canonical.wav").exists()
    assert list(workspace.directories["validated"].iterdir()) == []

    replay = execute_tts_once(
        store,
        provider,  # type: ignore[arg-type]
        drive,  # type: ignore[arg-type]
        request,
        workspace=workspace,
        temp_root=temp_root,
        occurred_at="2026-10-24T00:01:00Z",
    )
    assert replay == result
    assert provider.calls == 1

    video = _video(tmp_path / "silent-video.mp4")
    merged = merge_verified_tts_audio(
        store,  # type: ignore[arg-type]
        drive,  # type: ignore[arg-type]
        replay,
        video,
        workspace.directories["validated"] / "merged.mp4",
        workspace=workspace,
        temp_root=temp_root,
        expected_duration_sec=2.0,
        expected_aspect_ratio="16:9",
    )
    assert merged.video_strategy == "VIDEO_COPY"
    assert merged.audio_validation.codec == "aac"
    assert merged.video_validation.decoded_video_frames == 20
    assert not (workspace.directories["audio"] / "TTS_000001-merge-source.wav").exists()


def test_verified_tts_cleanup_recovery_completes_without_provider_resubmit(
    tmp_path: Path,
) -> None:
    store = MemoryTTSJobStore()
    request = TTSJobRequest(
        "PRD_000001",
        "SDL_000001",
        1,
        "Approved line.",
        "VOI_000001",
        "th-TH-NiwatNeural",
        "th-TH",
        "PVA_0001",
        "MDL_TTS_0001",
        2.0,
    )
    claim = store.claim(request, "2026-10-24T00:00:00Z")
    workspace = create_workspace(tmp_path / "workspaces", "PRD_000001")
    canonical = workspace.directories["audio"] / f"{claim.tts_job_id}-canonical.wav"
    raw = workspace.directories["audio"] / f"{claim.tts_job_id}-provider.mp3"
    canonical.write_bytes(b"verified-output-local-remnant")
    raw.write_bytes(b"provider-output-local-remnant")
    digest = "a" * 64
    store.states[claim.tts_job_id].update(
        {
            "status": "REMOTE_VERIFYING",
            "Remote_Verified": True,
            "Remote_File_ID": "drive-audio-1",
            "Local_Cleanup_Status": "CLEANUP_FAILED",
            "artifact": {
                "provider_execution_id": "provider-execution-1",
                "remote_audio_uri": "https://drive.google.com/file/d/drive-audio-1/view",
                "remote_file_id": "drive-audio-1",
                "audio_sha256": digest,
                "audio_size_bytes": 512,
                "audio_duration_ms": 750,
            },
        }
    )

    result = recover_tts_cleanup(
        store,
        claim,
        workspace=workspace,
        temp_root=tmp_path / "workspaces",
        occurred_at="2026-10-24T00:01:00Z",
    )

    assert result.claim.job_status == "COMPLETED"
    assert result.provider_execution_id == "provider-execution-1"
    assert store.states[claim.tts_job_id]["status"] == "COMPLETED"
    assert not canonical.exists()
    assert not raw.exists()


def test_merge_rejects_malformed_tts_id_before_workspace_path_use(tmp_path: Path) -> None:
    request = TTSJobRequest(
        "PRD_000001",
        "SDL_000001",
        1,
        "Approved line.",
        "VOI_000001",
        "th-TH-NiwatNeural",
        "th-TH",
        "PVA_0001",
        "MDL_TTS_0001",
        2.0,
    )
    malformed = TTSExecutionResult(
        TTSJobClaim("../../outside", request, "COMPLETED"),
        "provider-execution-1",
        "https://drive.google.com/file/d/drive-audio-1/view",
        "drive-audio-1",
        "a" * 64,
        512,
        500,
    )
    temp_root = tmp_path / "workspaces"
    workspace = create_workspace(temp_root, "PRD_000001")
    video = _video(tmp_path / "silent.mp4")

    with pytest.raises(ValueError, match="canonical format"):
        merge_verified_tts_segments(
            MemoryTTSJobStore(),  # type: ignore[arg-type]
            FakeTTSDrive(tmp_path / "drive"),  # type: ignore[arg-type]
            (TTSAudioSegment(malformed),),
            video,
            workspace.directories["validated"] / "output.mp4",
            workspace=workspace,
            temp_root=temp_root,
            expected_duration_sec=2.0,
            expected_aspect_ratio="16:9",
        )

    assert list(workspace.directories["audio"].iterdir()) == []


def test_ordered_tts_segments_include_lineage_and_pause_in_idempotent_merge(
    tmp_path: Path,
) -> None:
    import json

    source = _wav(tmp_path / "remote.wav", 0.5)
    drive = FakeTTSDrive(tmp_path / "drive")
    drive.remote.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, drive.remote)
    sha = hashlib.sha256(drive.remote.read_bytes()).hexdigest()
    size = drive.remote.stat().st_size
    store = MemoryTTSJobStore()
    segments = []
    for index in (1, 2):
        request = TTSJobRequest(
            "PRD_000001",
            f"SDL_{index:06d}",
            1,
            f"Approved line {index}.",
            "VOI_000001",
            "th-TH-NiwatNeural",
            "th-TH",
            "PVA_0001",
            "MDL_TTS_0001",
            2.0,
        )
        claim = TTSJobClaim(f"TTS_{index:06d}", request, "COMPLETED")
        result = TTSExecutionResult(
            claim,
            f"provider-{index}",
            "https://drive.google.com/file/d/drive-audio-1/view",
            "drive-audio-1",
            sha,
            size,
            500,
        )
        key = (request.production_job_id, request.dialogue_line_id, request.attempt_no)
        store.rows[key] = claim
        store.states[claim.tts_job_id] = {
            "status": "COMPLETED",
            "artifact": {
                "provider_execution_id": result.provider_execution_id,
                "remote_audio_uri": result.remote_audio_uri,
                "remote_file_id": result.remote_file_id,
                "audio_sha256": result.audio_sha256,
                "audio_size_bytes": result.audio_size_bytes,
                "audio_duration_ms": result.audio_duration_ms,
            },
        }
        segments.append(TTSAudioSegment(result, 250 if index == 1 else 0))

    temp_root = tmp_path / "workspaces"
    workspace = create_workspace(temp_root, "PRD_000001")
    video = _video(tmp_path / "silent.mp4")
    output = workspace.directories["validated"] / "final.mp4"
    merged = merge_verified_tts_segments(
        store,  # type: ignore[arg-type]
        drive,  # type: ignore[arg-type]
        tuple(segments),
        video,
        output,
        workspace=workspace,
        temp_root=temp_root,
        expected_duration_sec=2.0,
        expected_aspect_ratio="16:9",
    )

    assert merged.source_tts_job_ids == ("TTS_000001", "TTS_000002")
    assert merged.source_dialogue_line_ids == ("SDL_000001", "SDL_000002")
    manifest = json.loads(output.with_name(f"{output.name}.gflow-merge.json").read_text())
    assert manifest["source_tts_job_ids"] == ["TTS_000001", "TTS_000002"]


def test_no_audio_finalization_persists_verified_final_video_without_tts(tmp_path: Path) -> None:
    source = _video(tmp_path / "silent.mp4")
    store = MemoryFinalOutputStore()
    drive = FakeFinalOutputDrive(tmp_path / "drive")

    result = finalize_no_audio(
        store,
        drive,
        production_job_id="PRD_000001",
        scene_prompt_id="SPV_000001",
        generation_profile_id="GPR_0001",
        d2_run_id="D2_test-run-0001",
        provider_account_id="PVA_0001",
        provider_execution_id="execution-1",
        provider_media_id="media-1",
        scene={"Scene_ID": "SCN_000001", "Dialogue_Mode": "NONE"},
        dialogue_rows=(),
        video_path=source,
        expected_duration_sec=2.0,
        expected_aspect_ratio="16:9",
        verification_temp_dir=tmp_path / "verify",
        occurred_at="2026-10-24T00:00:00Z",
    )

    assert result.final_output_id == "FOUT_000001"
    assert result.output_sha256
    assert len(store.rows) == 1
    assert store.rows[0]["Output_Type"] == "FINAL_VIDEO"
    assert store.rows[0]["Validation_Status"] == "PASS"
    assert store.rows[0]["Remote_Verified"] is True
    assert "drive-manifest-1" in str(store.rows[0]["Validation_Message"])
    assert len(drive.files) == 2
    assert source.is_file()
    assert list((tmp_path / "verify").iterdir()) == []

    replay = finalize_no_audio(
        store,
        drive,
        production_job_id="PRD_000001",
        scene_prompt_id="SPV_000001",
        generation_profile_id="GPR_0001",
        d2_run_id="D2_test-run-0001",
        provider_account_id="PVA_0001",
        provider_execution_id="execution-1",
        provider_media_id="media-1",
        scene={"Scene_ID": "SCN_000001", "Dialogue_Mode": "NONE"},
        dialogue_rows=(),
        video_path=source,
        expected_duration_sec=2.0,
        expected_aspect_ratio="16:9",
        verification_temp_dir=tmp_path / "verify",
        occurred_at="2026-10-24T00:01:00Z",
    )
    assert replay == result
    assert len(store.rows) == 1


def test_no_audio_finalization_rejects_dialogue_before_remote_work(tmp_path: Path) -> None:
    source = _video(tmp_path / "silent.mp4")
    store = MemoryFinalOutputStore()
    drive = FakeFinalOutputDrive(tmp_path / "drive")

    with pytest.raises(NoAudioFinalizationError, match="no linked dialogue"):
        finalize_no_audio(
            store,
            drive,
            production_job_id="PRD_000001",
            scene_prompt_id="SPV_000001",
            generation_profile_id="GPR_0001",
            d2_run_id="D2_test-run-0001",
            provider_account_id="PVA_0001",
            provider_execution_id="execution-1",
            provider_media_id="media-1",
            scene={"Scene_ID": "SCN_000001", "Dialogue_Mode": "NONE"},
            dialogue_rows=({"Scene_ID": "SCN_000001", "Dialogue_Line_ID": "SDL_000001"},),
            video_path=source,
            expected_duration_sec=2.0,
            expected_aspect_ratio="16:9",
            verification_temp_dir=tmp_path / "verify",
            occurred_at="2026-10-24T00:00:00Z",
        )

    assert store.rows == []
    assert not drive.root.exists()


def test_no_audio_finalization_rejects_video_that_contains_audio(tmp_path: Path) -> None:
    source = _video_with_audio(tmp_path / "with-audio.mp4")
    store = MemoryFinalOutputStore()
    drive = FakeFinalOutputDrive(tmp_path / "drive")

    with pytest.raises(NoAudioFinalizationError, match="unexpectedly contains"):
        finalize_no_audio(
            store,
            drive,
            production_job_id="PRD_000001",
            scene_prompt_id="SPV_000001",
            generation_profile_id="GPR_0001",
            d2_run_id="D2_test-run-0001",
            provider_account_id="PVA_0001",
            provider_execution_id="execution-1",
            provider_media_id="media-1",
            scene={"Scene_ID": "SCN_000001", "Dialogue_Mode": "NONE"},
            dialogue_rows=(),
            video_path=source,
            expected_duration_sec=2.0,
            expected_aspect_ratio="16:9",
            verification_temp_dir=tmp_path / "verify",
            occurred_at="2026-10-24T00:00:00Z",
        )

    assert store.rows == []
    assert not drive.root.exists()


def test_audio_probe_and_canonical_pcm_conversion(tmp_path: Path) -> None:
    source = _wav(tmp_path / "source.wav", 1.0)
    info = inspect_audio(source)

    assert info.duration_sec == pytest.approx(1.0, abs=0.05)
    assert info.sample_rate_hz == 24_000
    assert info.channels == 1
    assert len(info.sha256) == 64

    canonical = canonicalize_audio(source, tmp_path / "canonical.wav")
    assert canonical.codec == "pcm_s16le"
    assert canonical.sample_rate_hz == 48_000
    assert canonical.channels == 1
    assert canonical.duration_sec == pytest.approx(1.0, abs=0.05)


def test_audio_probe_rejects_empty_and_corrupt_audio(tmp_path: Path) -> None:
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    corrupt = tmp_path / "corrupt.wav"
    corrupt.write_bytes(b"not audio")

    with pytest.raises(ValueError, match="empty"):
        inspect_audio(empty)
    with pytest.raises(ValueError, match="probe|decode|audio"):
        inspect_audio(corrupt)


def test_audio_probe_stops_decoding_when_supplied_duration_limit_is_exceeded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    import gflow_cli.v2_runtime.media_audio as media_audio

    source = tmp_path / "missing-duration.wav"
    source.write_bytes(b"x" * 1024)

    class FakeFrame:
        sample_rate = 48_000
        samples = 96_000

        def to_ndarray(self) -> np.ndarray:
            raise AssertionError("duration overflow must stop before sample decode")

    class FakeContainer:
        streams = (SimpleNamespace(type="audio"),)

        def __enter__(self) -> FakeContainer:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def decode(self, *, audio: int):
            assert audio == 0
            yield FakeFrame()
            raise AssertionError("duration overflow must stop the decode iterator")

    monkeypatch.setattr(
        media_audio.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            stdout=(
                b'{"streams":[{"codec_name":"pcm_s16le","sample_rate":"48000",'
                b'"channels":1,"bit_rate":"768000"}]}'
            )
        ),
    )
    monkeypatch.setattr(media_audio.av, "open", lambda *_args, **_kwargs: FakeContainer())

    with pytest.raises(ValueError, match=TTS_DURATION_MISMATCH):
        inspect_audio(source, max_duration_sec=1.0)


def test_audio_probe_rejects_clipped_audio_regression(tmp_path: Path) -> None:
    clipped = tmp_path / "clipped.wav"
    with wave.open(str(clipped), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(np.full(24_000, 32767, dtype=np.int16).tobytes())

    with pytest.raises(ValueError, match="clipped"):
        inspect_audio(clipped)


@pytest.mark.parametrize(
    ("audio_duration", "expected_pad"),
    [(1.0, 1.0), (2.0, 0.0)],
)
def test_timeline_policy_preserves_video_and_pads_only_short_audio(
    audio_duration: float, expected_pad: float
) -> None:
    plan = plan_audio_timeline(video_duration_sec=2.0, audio_duration_sec=audio_duration)

    assert plan.video_duration_sec == 2.0
    assert plan.audio_pad_duration_sec == pytest.approx(expected_pad)


@pytest.mark.parametrize("audio_duration", [2.01, 20.0])
def test_timeline_policy_rejects_audio_longer_than_video(audio_duration: float) -> None:
    with pytest.raises(ValueError, match=TTS_DURATION_MISMATCH):
        plan_audio_timeline(video_duration_sec=2.0, audio_duration_sec=audio_duration)


def test_merge_uses_video_copy_and_preserves_video_with_short_audio(tmp_path: Path) -> None:
    video = _video(tmp_path / "video.mp4")
    audio = _wav(tmp_path / "audio.wav", 1.0)
    output = tmp_path / "merged.mp4"

    commands: list[list[str]] = []

    def run_and_capture(command: list[str]) -> None:
        from subprocess import run

        commands.append(command)
        completed = run(command, capture_output=True, check=False, timeout=30)
        if completed.returncode:
            raise RuntimeError("ffmpeg failed")

    result = merge_audio_video(
        video,
        audio,
        output,
        expected_aspect_ratio="16:9",
        expected_duration_sec=2.0,
        command_runner=run_and_capture,
        source_tts_job_ids=("TTS_000001",),
        source_dialogue_line_ids=("SDL_000001",),
    )

    assert result.video_strategy == "VIDEO_COPY"
    assert result.duration_sec == pytest.approx(2.0, abs=0.15)
    assert result.video_validation.decoded_video_frames == 20
    assert result.audio_validation.duration_sec == pytest.approx(2.0, abs=0.15)
    assert result.audio_validation.clipped_sample_ratio < 0.01
    assert len(result.sha256) == 64
    assert all("-shortest" not in command for command in commands)
    command_count = len(commands)
    replay = merge_audio_video(
        video,
        audio,
        output,
        expected_aspect_ratio="16:9",
        expected_duration_sec=2.0,
        command_runner=run_and_capture,
        source_tts_job_ids=("TTS_000001",),
        source_dialogue_line_ids=("SDL_000001",),
    )
    assert replay.sha256 == result.sha256
    assert replay.merge_job_sha256 == result.merge_job_sha256
    assert replay.video_strategy == result.video_strategy
    assert len(commands) == command_count

    store = MemoryFinalOutputStore()
    drive = FakeFinalOutputDrive(tmp_path / "final-drive")
    final = finalize_merged_output(
        store,
        drive,
        production_job_id="PRD_000001",
        scene_prompt_id="SPV_000001",
        generation_profile_id="GPR_0001",
        d2_run_id="D2_test-run-0001",
        provider_account_id="PVA_0001",
        provider_execution_id="execution-1",
        provider_media_id="media-1",
        scene={"Scene_ID": "SCN_000001", "Dialogue_Mode": "DIALOGUE"},
        dialogue_rows=({"Scene_ID": "SCN_000001", "Dialogue_Line_ID": "SDL_000001", "Line_No": 1},),
        merge_result=result,
        expected_duration_sec=2.0,
        expected_aspect_ratio="16:9",
        verification_temp_dir=tmp_path / "final-verify",
        occurred_at="2026-10-24T00:00:00Z",
    )
    assert final.final_output_id == "FOUT_000001"
    assert store.rows[0]["Assembly_Manifest_SHA256"]
    assert "VIDEO_COPY" in str(store.rows[0]["Validation_Message"])


def test_merged_finalization_rejects_missing_or_duplicate_line_numbers(
    tmp_path: Path,
) -> None:
    source = _video(tmp_path / "silent.mp4")
    result = merge_audio_video(
        source,
        _wav(tmp_path / "audio.wav", 2.0),
        tmp_path / "merged.mp4",
        expected_aspect_ratio="16:9",
        expected_duration_sec=2.0,
        source_tts_job_ids=("TTS_000001", "TTS_000002"),
        source_dialogue_line_ids=("SDL_000001", "SDL_000002"),
    )
    store = MemoryFinalOutputStore()
    drive = FakeFinalOutputDrive(tmp_path / "drive")

    with pytest.raises(NoAudioFinalizationError, match="unique positive"):
        finalize_merged_output(
            store,
            drive,
            production_job_id="PRD_000001",
            scene_prompt_id="SPV_000001",
            generation_profile_id="GPR_0001",
            d2_run_id="D2_test-run-0001",
            provider_account_id="PVA_0001",
            provider_execution_id="execution-1",
            provider_media_id="media-1",
            scene={"Scene_ID": "SCN_000001", "Dialogue_Mode": "DIALOGUE"},
            dialogue_rows=(
                {
                    "Scene_ID": "SCN_000001",
                    "Dialogue_Line_ID": "SDL_000001",
                    "Line_No": 1,
                },
                {
                    "Scene_ID": "SCN_000001",
                    "Dialogue_Line_ID": "SDL_000002",
                    "Line_No": 1,
                },
            ),
            merge_result=result,
            expected_duration_sec=2.0,
            expected_aspect_ratio="16:9",
            verification_temp_dir=tmp_path / "verify",
            occurred_at="2026-10-24T00:00:00Z",
        )

    assert store.rows == []


def test_merge_uses_fallback_when_stream_copy_fails(tmp_path: Path) -> None:
    video = _video(tmp_path / "video.mp4")
    audio = _wav(tmp_path / "audio.wav", 2.0)
    output = tmp_path / "merged.mp4"
    real_run = shutil.which("ffmpeg")
    assert real_run is not None
    calls: list[list[str]] = []

    def fail_copy_once(command: list[str]) -> None:
        calls.append(command)
        if len(calls) == 1:
            raise RuntimeError("forced stream-copy incompatibility")
        from subprocess import run

        completed = run(command, capture_output=True, check=False, timeout=30)
        if completed.returncode:
            raise RuntimeError("ffmpeg fallback failed")

    result = merge_audio_video(
        video,
        audio,
        output,
        expected_aspect_ratio="16:9",
        expected_duration_sec=2.0,
        command_runner=fail_copy_once,
    )

    assert result.video_strategy == "VIDEO_REENCODE"
    assert len(calls) == 2
    assert result.audio_validation.codec == "aac"
    assert result.video_validation.decoded_video_frames == 20


def test_merge_rejects_long_speech_without_invoking_ffmpeg(tmp_path: Path) -> None:
    video = _video(tmp_path / "video.mp4")
    audio = _wav(tmp_path / "audio.wav", 5.0)
    output = tmp_path / "merged.mp4"
    calls: list[list[str]] = []

    with pytest.raises(ValueError, match=TTS_DURATION_MISMATCH):
        merge_audio_video(
            video,
            audio,
            output,
            expected_aspect_ratio="16:9",
            expected_duration_sec=2.0,
            command_runner=lambda command: calls.append(command),
        )

    assert calls == []
    assert not output.exists()

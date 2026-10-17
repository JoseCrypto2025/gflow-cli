from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import gflow_cli.v2_runtime.google_flow as flow
from gflow_cli.v2_runtime.errors import FlowPresubmitError, FlowSubmissionDisabledError
from gflow_cli.v2_runtime.google_flow import (
    FlowPresubmitRequest,
    FlowProjectState,
    FlowReadback,
    FlowSessionState,
    GoogleFlowProviderAdapter,
    PersistedPromptVersion,
    ProviderReferenceBinding,
    classify_flow_surface,
    diagnostic_path,
    prompt_sha256,
)
from gflow_cli.v2_runtime.phase_e import PreflightResult, ReferenceDescriptor


class FakeLease:
    async def __aenter__(self) -> FakeLease:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakePort:
    def __init__(self) -> None:
        self.session = FlowSessionState("FLOW_SESSION_READY", "PVA_0001")
        self.project = FlowProjectState("flow-project-1")
        self.surface = "FLOW_MIGRATED"
        self.applied: dict[str, object] = {}
        self.bound: tuple[ProviderReferenceBinding, ...] = ()
        self.prompt: str | None = None
        self.negative_prompt: str | None = None
        self.readback_settings: dict[str, object] | None = None
        self.readback_setting_override: tuple[str, object] | None = None
        self.readback_references: tuple[ProviderReferenceBinding, ...] | None = None
        self.readback_prompt: str | None = None
        self.readback_account_id: str | None = None
        self.readback_project_id: str | None = None
        self.readback_session_status: str | None = None
        self.negative_supported = True
        self.generate_present = True
        self.generate_enabled = True
        self.calls: list[str] = []

    async def inspect_session(self) -> FlowSessionState:
        self.calls.append("inspect_session")
        return self.session

    async def select_project(self, project_id: str) -> FlowProjectState:
        self.calls.append("select_project")
        assert project_id == "flow-project-1"
        return self.project

    async def inspect_surface(self) -> str | None:
        self.calls.append("inspect_surface")
        return self.surface

    async def apply_settings(self, settings: dict[str, object]) -> dict[str, object]:
        self.calls.append("apply_settings")
        self.applied = dict(settings)
        return self.applied

    async def bind_references(
        self, references: tuple[ProviderReferenceBinding, ...]
    ) -> tuple[ProviderReferenceBinding, ...]:
        self.calls.append("bind_references")
        self.bound = references
        return references

    async def fill_prompt(self, prompt: str) -> None:
        self.calls.append("fill_prompt")
        self.prompt = prompt

    async def supports_negative_prompt(self) -> bool:
        return self.negative_supported

    async def fill_negative_prompt(self, prompt: str) -> None:
        self.negative_prompt = prompt

    async def readback(self) -> FlowReadback:
        self.calls.append("readback")
        final_settings = dict(
            self.readback_settings if self.readback_settings is not None else self.applied
        )
        if self.readback_setting_override:
            key, value = self.readback_setting_override
            final_settings[key] = value
        return FlowReadback(
            self.readback_session_status or self.session.status,
            self.readback_account_id or "PVA_0001",
            self.readback_project_id or "flow-project-1",
            self.surface,
            final_settings,
            self.readback_references if self.readback_references is not None else self.bound,
            self.readback_prompt if self.readback_prompt is not None else self.prompt,
            self.negative_prompt,
            self.negative_supported,
            self.generate_present,
            self.generate_enabled,
        )


def request_for(
    tmp_path: Path, *, references: tuple[ProviderReferenceBinding, ...] = ()
) -> FlowPresubmitRequest:
    mode = "FIRST_FRAME_TO_VIDEO" if references else "TEXT_TO_VIDEO"
    positive = "A red kite over the ocean."
    negative = "blurry"
    digest = prompt_sha256(positive, negative)
    reference_descriptors = (
        (ReferenceDescriptor("REF_0001", "START_FRAME", "drive-file-1"),) if references else ()
    )
    result = PreflightResult(
        "PREFLIGHT_PASS",
        True,
        "SCN_0001",
        "GPR_0001",
        resolved={
            "provider_id": "PRV_0001",
            "provider_account_id": "PVA_0001",
            "model_id": "MDL_0001",
            "generation_mode": mode,
        },
        technical_configuration={
            "generation_mode": mode,
            "duration_sec": 8,
            "aspect_ratio": "16:9",
            "resolution": "720p",
            "output_count": 1,
            "audio_mode": "NATIVE",
            "quality_tier": "STANDARD",
        },
        compiled_prompt=positive,
        compiled_negative_prompt=negative,
        source_context_sha256="a" * 64,
        prompt_sha256=digest,
        references=reference_descriptors,
    )
    prompt = PersistedPromptVersion(
        "SPV_000001",
        "SCN_0001",
        "GPR_0001",
        "APPROVED",
        positive,
        negative,
        "a" * 64,
        digest,
    )
    return FlowPresubmitRequest(
        result,
        prompt,
        "PRV_0001",
        "PVA_0001",
        "MDL_0001",
        mode,
        "flow-project-1",
        tmp_path / "state",
        tmp_path / "temp",
        references,
    )


@pytest.fixture(autouse=True)
def no_real_profile_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flow, "ProfileLease", lambda _path: FakeLease())


@pytest.mark.asyncio
async def test_valid_approved_request_reaches_presubmit_ready_without_submission(
    tmp_path: Path,
) -> None:
    port = FakePort()
    outcome = await GoogleFlowProviderAdapter("PRV_0001").prepare(request_for(tmp_path), port)
    assert outcome.state == "FLOW_PRESUBMIT_READY"
    assert outcome.generate_present and outcome.generate_enabled
    assert "FLOW_PRESUBMIT_READY" not in {"SUBMITTED", "RENDERING", "COMPLETED"}
    assert port.calls[-1] == "readback"
    with pytest.raises(FlowSubmissionDisabledError) as exc:
        GoogleFlowProviderAdapter.assert_submission_disabled()
    assert exc.value.code == "FLOW_SUBMISSION_DISABLED"


@pytest.mark.parametrize("status", ["VALIDATED", "SUPERSEDED", "HOLD"])
@pytest.mark.asyncio
async def test_non_approved_prompt_is_rejected_before_ui(tmp_path: Path, status: str) -> None:
    request = request_for(tmp_path)
    request = replace(request, prompt_version=replace(request.prompt_version, prompt_status=status))
    port = FakePort()
    with pytest.raises(FlowPresubmitError, match="persisted prompt") as exc:
        await GoogleFlowProviderAdapter("PRV_0001").prepare(request, port)
    assert exc.value.code == "SCENE_PROMPT_NOT_APPROVED"
    assert port.calls == []


@pytest.mark.asyncio
async def test_prompt_hash_or_text_mismatch_is_rejected(tmp_path: Path) -> None:
    request = request_for(tmp_path)
    request = replace(
        request, prompt_version=replace(request.prompt_version, compiled_prompt_text="changed")
    )
    with pytest.raises(FlowPresubmitError) as exc:
        await GoogleFlowProviderAdapter("PRV_0001").prepare(request, FakePort())
    assert exc.value.code == "SCENE_PROMPT_CONTRACT_MISMATCH"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("session", FlowSessionState("FLOW_AUTH_REQUIRED", None), "FLOW_AUTH_REQUIRED"),
        ("session", FlowSessionState("FLOW_AUTH_EXPIRED", "PVA_0001"), "FLOW_AUTH_EXPIRED"),
        ("session", FlowSessionState("FLOW_AUTH_CHALLENGE", "PVA_0001"), "FLOW_AUTH_CHALLENGE"),
        ("session", FlowSessionState("FLOW_SESSION_READY", "OTHER"), "FLOW_ACCOUNT_MISMATCH"),
        ("project", FlowProjectState(None), "FLOW_PROJECT_MISMATCH"),
        ("surface", "UNKNOWN", "FLOW_SURFACE_CHANGED"),
    ],
)
@pytest.mark.asyncio
async def test_session_project_and_surface_safety_holds(
    tmp_path: Path, field: str, value: Any, code: str
) -> None:
    port = FakePort()
    setattr(port, field, value)
    with pytest.raises(FlowPresubmitError) as exc:
        await GoogleFlowProviderAdapter("PRV_0001").prepare(request_for(tmp_path), port)
    assert exc.value.code == code


@pytest.mark.parametrize(
    ("key", "wrong"),
    [
        ("model_id", "MDL_OTHER"),
        ("generation_mode", "FIRST_LAST_FRAME_TO_VIDEO"),
        ("duration_sec", 6),
        ("aspect_ratio", "9:16"),
        ("resolution", "1080p"),
        ("output_count", 2),
        ("audio_mode", "NONE"),
        ("quality_tier", "PREMIUM"),
    ],
)
@pytest.mark.asyncio
async def test_each_material_setting_requires_exact_readback(
    tmp_path: Path, key: str, wrong: object
) -> None:
    port = FakePort()
    port.readback_setting_override = (key, wrong)
    with pytest.raises(FlowPresubmitError) as exc:
        await GoogleFlowProviderAdapter("PRV_0001").prepare(request_for(tmp_path), port)
    assert exc.value.code == "FLOW_SETTING_VALUE_MISMATCH"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("readback_settings", {"model_id": "wrong"}, "FLOW_SETTING_VALUE_MISMATCH"),
        ("readback_prompt", "A red kite over", "FLOW_PROMPT_VALUE_MISMATCH"),
        ("readback_project_id", "other-project", "FLOW_PROJECT_MISMATCH"),
        ("readback_account_id", "OTHER", "FLOW_ACCOUNT_MISMATCH"),
        ("readback_session_status", "FLOW_AUTH_EXPIRED", "FLOW_AUTH_EXPIRED"),
        ("negative_supported", False, "FLOW_NEGATIVE_PROMPT_UNSUPPORTED"),
        ("generate_present", False, "FLOW_GENERATE_CONTROL_MISSING"),
        ("generate_enabled", False, "FLOW_GENERATE_CONTROL_DISABLED"),
    ],
)
@pytest.mark.asyncio
async def test_final_readback_mismatches_hold(
    tmp_path: Path, field: str, value: Any, code: str
) -> None:
    port = FakePort()
    setattr(port, field, value)
    with pytest.raises(FlowPresubmitError) as exc:
        await GoogleFlowProviderAdapter("PRV_0001").prepare(request_for(tmp_path), port)
    assert exc.value.code == code


@pytest.mark.asyncio
async def test_required_reference_mapping_and_frame_slot_are_exact(tmp_path: Path) -> None:
    reference = ProviderReferenceBinding(
        "REF_0001", "PRV_0001", "provider-asset-1", "flow-project-1", "START_FRAME"
    )
    base = request_for(tmp_path, references=(reference,))
    descriptor = ReferenceDescriptor("REF_0001", "START_FRAME", "drive-file-1")
    base = replace(
        base,
        references=(),
        preflight=replace(base.preflight, references=(descriptor,)),
    )
    port = FakePort()
    with pytest.raises(FlowPresubmitError) as missing:
        await GoogleFlowProviderAdapter("PRV_0001").prepare(base, port)
    assert missing.value.code == "FLOW_REFERENCE_SYNC_REQUIRED"
    wrong = replace(reference, provider_project_id="other-project")
    with pytest.raises(FlowPresubmitError) as project:
        await GoogleFlowProviderAdapter("PRV_0001").prepare(
            replace(base, references=(wrong,)), FakePort()
        )
    assert project.value.code == "FLOW_PROJECT_MISMATCH"
    wrong_slot = replace(reference, frame_slot="END_FRAME")
    with pytest.raises(FlowPresubmitError) as slot:
        await GoogleFlowProviderAdapter("PRV_0001").prepare(
            replace(base, references=(wrong_slot,)), FakePort()
        )
    assert slot.value.code == "FLOW_REFERENCE_SLOT_MISMATCH"
    wrong_provider = replace(reference, provider_id="PRV_OTHER")
    with pytest.raises(FlowPresubmitError) as provider:
        await GoogleFlowProviderAdapter("PRV_0001").prepare(
            replace(base, references=(wrong_provider,)), FakePort()
        )
    assert provider.value.code == "FLOW_REFERENCE_MAP_INVALID"


@pytest.mark.asyncio
async def test_project_missing_holds_before_any_ui_call(tmp_path: Path) -> None:
    request = replace(request_for(tmp_path), provider_project_id="")
    port = FakePort()
    with pytest.raises(FlowPresubmitError) as exc:
        await GoogleFlowProviderAdapter("PRV_0001").prepare(request, port)
    assert exc.value.code == "FLOW_PROJECT_NOT_CONFIGURED"
    assert port.calls == []


@pytest.mark.asyncio
async def test_extend_and_video_to_video_are_held(tmp_path: Path) -> None:
    request = request_for(tmp_path)
    result = replace(
        request.preflight,
        resolved={**request.preflight.resolved, "generation_mode": "EXTEND_VIDEO"},
        technical_configuration={
            **request.preflight.technical_configuration,
            "generation_mode": "EXTEND_VIDEO",
        },
    )
    unsupported = replace(request, generation_mode="EXTEND_VIDEO", preflight=result)
    with pytest.raises(FlowPresubmitError) as exc:
        await GoogleFlowProviderAdapter("PRV_0001").prepare(unsupported, FakePort())
    assert exc.value.code == "FLOW_MODE_NOT_SUPPORTED"


@pytest.mark.asyncio
async def test_exact_reference_binding_passes(tmp_path: Path) -> None:
    ref = ProviderReferenceBinding(
        "REF_0001", "PRV_0001", "provider-asset-1", "flow-project-1", "START_FRAME"
    )
    outcome = await GoogleFlowProviderAdapter("PRV_0001").prepare(
        request_for(tmp_path, references=(ref,)), FakePort()
    )
    assert outcome.reference_count == 1


@pytest.mark.asyncio
async def test_reference_readback_requires_exact_provider_identity_and_slot(tmp_path: Path) -> None:
    reference = ProviderReferenceBinding(
        "REF_0001", "PRV_0001", "provider-asset-1", "flow-project-1", "START_FRAME"
    )
    port = FakePort()
    port.readback_references = (replace(reference, provider_asset_id="different-asset"),)
    with pytest.raises(FlowPresubmitError) as exc:
        await GoogleFlowProviderAdapter("PRV_0001").prepare(
            request_for(tmp_path, references=(reference,)), port
        )
    assert exc.value.code == "FLOW_REFERENCE_BINDING_MISMATCH"


def test_submission_always_fails_closed_even_if_flag_is_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert flow.FLOW_SUBMISSION_ENABLED is False
    with pytest.raises(FlowSubmissionDisabledError) as disabled:
        GoogleFlowProviderAdapter.assert_submission_disabled()
    assert disabled.value.code == "FLOW_SUBMISSION_DISABLED"
    monkeypatch.setattr(flow, "FLOW_SUBMISSION_ENABLED", True)
    with pytest.raises(FlowSubmissionDisabledError) as unavailable:
        GoogleFlowProviderAdapter.assert_submission_disabled()
    assert unavailable.value.code == "FLOW_SUBMISSION_DISABLED"
    with pytest.raises(FlowSubmissionDisabledError) as router_entry:
        GoogleFlowProviderAdapter("PRV_0001").prepare_request("GPR_0001", {})
    assert router_entry.value.code == "FLOW_SUBMISSION_DISABLED"


@pytest.mark.asyncio
async def test_same_account_project_concurrent_lane_is_blocked(tmp_path: Path) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingPort(FakePort):
        async def inspect_session(self) -> FlowSessionState:
            entered.set()
            await release.wait()
            return await super().inspect_session()

    adapter = GoogleFlowProviderAdapter("PRV_0001")
    first = asyncio.create_task(adapter.prepare(request_for(tmp_path), BlockingPort()))
    await entered.wait()
    with pytest.raises(FlowPresubmitError) as busy:
        await adapter.prepare(request_for(tmp_path), FakePort())
    assert busy.value.code == "FLOW_LANE_BUSY"
    release.set()
    assert (await first).state == "FLOW_PRESUBMIT_READY"


def test_diagnostic_root_must_be_absolute_non_root(tmp_path: Path) -> None:
    request = replace(request_for(tmp_path), temp_root=Path("relative"))
    with pytest.raises(FlowPresubmitError) as exc:
        GoogleFlowProviderAdapter("PRV_0001")._validate_request(request)
    assert exc.value.code == "FLOW_DIAGNOSTIC_PATH_INVALID"


def test_diagnostic_path_stays_beneath_temp_root(tmp_path: Path) -> None:
    root = tmp_path / "temp"
    path = diagnostic_path(root, "PRESUBMIT_001")
    assert path == root.resolve() / "PRESUBMIT_001"
    with pytest.raises(FlowPresubmitError) as exc:
        diagnostic_path(root, "../outside")
    assert exc.value.code == "FLOW_DIAGNOSTIC_PATH_INVALID"


@pytest.mark.parametrize(
    ("url", "surface"),
    [
        ("https://labs.google/fx/tools/flow", "LABS_CLASSIC"),
        ("https://flow.google.com/project/123", "FLOW_MIGRATED"),
        ("https://not-flow.example/?next=flow.google.com", None),
        ("http://flow.google.com/project/123", None),
    ],
)
def test_surface_classification_reuses_exact_flow_host_classifier(
    url: str, surface: str | None
) -> None:
    assert classify_flow_surface(url) == surface

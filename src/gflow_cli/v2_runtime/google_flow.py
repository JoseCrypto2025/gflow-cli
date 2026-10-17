"""Google Flow V2 preparation contract; deliberately has no submission API."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gflow_cli.api.transports._common import flow_host_kind
from gflow_cli.errors import ProfileLockedError
from gflow_cli.profile_lease import ProfileLease

from .errors import FlowPresubmitError, FlowSubmissionDisabledError
from .phase_e import PreflightResult, sha256_json

FLOW_SUBMISSION_ENABLED = False


@dataclass(frozen=True, slots=True)
class PersistedPromptVersion:
    scene_prompt_id: str
    scene_id: str
    generation_profile_id: str
    prompt_status: str
    compiled_prompt_text: str
    compiled_negative_prompt: str
    source_context_sha256: str
    prompt_sha256: str


@dataclass(frozen=True, slots=True)
class ProviderReferenceBinding:
    reference_asset_id: str
    provider_id: str
    provider_asset_id: str
    provider_project_id: str
    frame_slot: str | None = None


@dataclass(frozen=True, slots=True)
class FlowPresubmitRequest:
    preflight: PreflightResult
    prompt_version: PersistedPromptVersion
    provider_id: str
    provider_account_id: str
    model_id: str
    generation_mode: str
    provider_project_id: str
    runtime_state_root: Path
    temp_root: Path
    references: tuple[ProviderReferenceBinding, ...] = ()


@dataclass(frozen=True, slots=True)
class FlowSessionState:
    status: str
    account_id: str | None


@dataclass(frozen=True, slots=True)
class FlowProjectState:
    project_id: str | None


@dataclass(frozen=True, slots=True)
class FlowReadback:
    session_status: str
    account_id: str | None
    project_id: str | None
    surface: str | None
    settings: Mapping[str, object]
    references: tuple[ProviderReferenceBinding, ...]
    prompt: str | None
    negative_prompt: str | None
    negative_prompt_supported: bool
    generate_present: bool
    generate_enabled: bool


class FlowPreparationPort(Protocol):
    """Submission-free facade implemented using centralized Flow components.

    No click, upload, keyboard-submit, network-submit, or generation method is
    intentionally part of this interface.
    """

    async def inspect_session(self) -> FlowSessionState: ...
    async def select_project(self, project_id: str) -> FlowProjectState: ...
    async def inspect_surface(self) -> str | None: ...
    async def apply_settings(self, settings: Mapping[str, object]) -> Mapping[str, object]: ...
    async def bind_references(
        self, references: Sequence[ProviderReferenceBinding]
    ) -> tuple[ProviderReferenceBinding, ...]: ...
    async def fill_prompt(self, prompt: str) -> None: ...
    async def supports_negative_prompt(self) -> bool: ...
    async def fill_negative_prompt(self, prompt: str) -> None: ...
    async def readback(self) -> FlowReadback: ...


@dataclass(frozen=True, slots=True)
class FlowPresubmitResult:
    state: str
    provider_id: str
    provider_account_id: str
    provider_project_id: str
    settings: Mapping[str, object]
    reference_count: int
    prompt_sha256: str
    generate_present: bool
    generate_enabled: bool


_SUPPORTED_SURFACES = frozenset({"LABS_CLASSIC", "FLOW_MIGRATED"})
_EXPECTED_SETTINGS = (
    "model_id",
    "generation_mode",
    "duration_sec",
    "aspect_ratio",
    "resolution",
    "output_count",
    "audio_mode",
    "quality_tier",
)


def classify_flow_surface(url: object) -> str | None:
    """Use the repository's exact-host classifier instead of V2 URL selectors."""
    host = flow_host_kind(url)
    return None if host is None else {"labs": "LABS_CLASSIC", "migrated": "FLOW_MIGRATED"}.get(host)


def diagnostic_path(temp_root: Path, diagnostic_id: str) -> Path:
    """Resolve a temporary diagnostic location without allowing path traversal."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", diagnostic_id):
        raise FlowPresubmitError("FLOW_DIAGNOSTIC_PATH_INVALID", "diagnostic ID is invalid")
    root = temp_root.resolve()
    path = (root / diagnostic_id).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise FlowPresubmitError(
            "FLOW_DIAGNOSTIC_PATH_INVALID", "diagnostic path escapes TEMP_ROOT"
        ) from exc
    return path


class GoogleFlowProviderAdapter:
    """Prepare a dedicated Flow project to a verified, non-submitted state."""

    provider_type = "VIDEO_GENERATION"
    _lanes: dict[str, asyncio.Lock] = {}

    def __init__(self, provider_id: str) -> None:
        if not provider_id.strip():
            raise ValueError("Google Flow Provider_ID must come from validated master data")
        self.provider_id = provider_id

    def prepare_request(self, _profile_id: str, _payload: dict[str, object]) -> dict[str, object]:
        """Router compatibility only; use `prepare` for presubmit, never execution."""
        self.assert_submission_disabled()
        raise AssertionError("unreachable")

    @classmethod
    def assert_submission_disabled(cls) -> None:
        # This phase never includes a submission implementation, even if an
        # environment variable or UI state claims generation is enabled.
        if not FLOW_SUBMISSION_ENABLED:
            raise FlowSubmissionDisabledError("Google Flow generation is disabled in Phase F")
        raise FlowSubmissionDisabledError("Google Flow submission is not implemented in Phase F")

    async def prepare(
        self, request: FlowPresubmitRequest, port: FlowPreparationPort
    ) -> FlowPresubmitResult:
        settings = self._validate_request(request)
        expected_refs = self._validate_references(request)
        lane_key = f"{request.provider_account_id}:{request.provider_project_id}"
        lane = self._lanes.setdefault(lane_key, asyncio.Lock())
        if lane.locked():
            raise FlowPresubmitError("FLOW_LANE_BUSY", "Flow account/project lane is occupied")
        async with lane:
            profile_dir = request.runtime_state_root / "browser-state" / request.provider_account_id
            try:
                async with ProfileLease(profile_dir):
                    return await self._prepare_locked(request, port, settings, expected_refs)
            except ProfileLockedError as exc:
                raise FlowPresubmitError(
                    "FLOW_LANE_BUSY", "Flow account browser lane is occupied"
                ) from exc
            except FlowPresubmitError:
                raise
            except Exception as exc:
                # Do not include exception text: browser libraries can include
                # URLs, profile paths, or authentication details.
                raise FlowPresubmitError(
                    "FLOW_PRESUBMIT_FAILED", "Flow preparation failed safely"
                ) from exc

    async def _prepare_locked(
        self,
        request: FlowPresubmitRequest,
        port: FlowPreparationPort,
        settings: Mapping[str, object],
        references: tuple[ProviderReferenceBinding, ...],
    ) -> FlowPresubmitResult:
        session = await port.inspect_session()
        if session.status != "FLOW_SESSION_READY":
            raise FlowPresubmitError(session.status, "Flow session is not verified and ready")
        if session.account_id != request.provider_account_id:
            raise FlowPresubmitError(
                "FLOW_ACCOUNT_MISMATCH", "authenticated account identity differs"
            )

        if not request.provider_project_id:
            raise FlowPresubmitError("FLOW_PROJECT_NOT_CONFIGURED", "dedicated project is required")
        project = await port.select_project(request.provider_project_id)
        if project.project_id != request.provider_project_id:
            raise FlowPresubmitError("FLOW_PROJECT_MISMATCH", "dedicated project identity differs")

        surface = await port.inspect_surface()
        if surface not in _SUPPORTED_SURFACES:
            raise FlowPresubmitError("FLOW_SURFACE_CHANGED", "Flow editor surface is unknown")

        negative_supported = await port.supports_negative_prompt()
        if request.prompt_version.compiled_negative_prompt and not negative_supported:
            raise FlowPresubmitError(
                "FLOW_NEGATIVE_PROMPT_UNSUPPORTED",
                "selected Flow mode has no verified negative prompt contract",
            )

        configured = await port.apply_settings(settings)
        self._compare_mapping(settings, configured, "FLOW_SETTING_VALUE_MISMATCH")
        bound = await port.bind_references(references)
        self._compare_references(references, bound)
        await port.fill_prompt(request.prompt_version.compiled_prompt_text)
        if request.prompt_version.compiled_negative_prompt:
            await port.fill_negative_prompt(request.prompt_version.compiled_negative_prompt)
        readback = await port.readback()

        if readback.account_id != request.provider_account_id:
            raise FlowPresubmitError(
                "FLOW_ACCOUNT_MISMATCH", "final authenticated account identity differs"
            )
        if readback.project_id != request.provider_project_id:
            raise FlowPresubmitError(
                "FLOW_PROJECT_MISMATCH", "final selected project identity differs"
            )
        if readback.session_status != "FLOW_SESSION_READY":
            raise FlowPresubmitError(
                readback.session_status, "final Flow session state is not ready"
            )
        if readback.surface != surface:
            raise FlowPresubmitError(
                "FLOW_SURFACE_CHANGED", "Flow editor surface changed during preparation"
            )
        self._compare_mapping(settings, readback.settings, "FLOW_SETTING_VALUE_MISMATCH")
        self._compare_references(references, readback.references)
        if readback.prompt != request.prompt_version.compiled_prompt_text:
            raise FlowPresubmitError(
                "FLOW_PROMPT_VALUE_MISMATCH", "Flow prompt readback differs or is truncated"
            )
        if (
            request.prompt_version.compiled_negative_prompt
            and not readback.negative_prompt_supported
        ):
            raise FlowPresubmitError(
                "FLOW_NEGATIVE_PROMPT_UNSUPPORTED",
                "selected Flow mode has no verified negative prompt contract",
            )
        if readback.negative_prompt != request.prompt_version.compiled_negative_prompt:
            raise FlowPresubmitError(
                "FLOW_NEGATIVE_PROMPT_MISMATCH", "Flow negative prompt readback differs"
            )
        if not readback.generate_present:
            raise FlowPresubmitError(
                "FLOW_GENERATE_CONTROL_MISSING", "Generate control is not present"
            )
        if not readback.generate_enabled:
            raise FlowPresubmitError(
                "FLOW_GENERATE_CONTROL_DISABLED", "Generate control is not enabled"
            )
        return FlowPresubmitResult(
            "FLOW_PRESUBMIT_READY",
            request.provider_id,
            request.provider_account_id,
            request.provider_project_id,
            dict(settings),
            len(references),
            request.prompt_version.prompt_sha256,
            True,
            True,
        )

    def _validate_request(self, request: FlowPresubmitRequest) -> dict[str, object]:
        result = request.preflight
        prompt = request.prompt_version
        if not result.ready_for_provider_adapter or result.status != "PREFLIGHT_PASS":
            raise FlowPresubmitError(
                "PHASE_E_PREFLIGHT_REQUIRED", "successful Phase E preflight is required"
            )
        if request.provider_id != self.provider_id:
            raise FlowPresubmitError("FLOW_PROVIDER_ID_MISMATCH", "request is not for Google Flow")
        if not request.provider_project_id:
            raise FlowPresubmitError("FLOW_PROJECT_NOT_CONFIGURED", "dedicated project is required")
        if prompt.prompt_status.upper() != "APPROVED":
            raise FlowPresubmitError(
                "SCENE_PROMPT_NOT_APPROVED", "persisted prompt version must be APPROVED"
            )
        if (prompt.scene_id, prompt.generation_profile_id) != (
            result.scene_id,
            result.generation_profile_id,
        ):
            raise FlowPresubmitError(
                "SCENE_PROMPT_CONTRACT_MISMATCH", "approved prompt identity differs from Phase E"
            )
        if (
            not re.fullmatch(r"SPV_[0-9]{6}", prompt.scene_prompt_id)
            or not re.fullmatch(r"[a-f0-9]{64}", prompt.source_context_sha256)
            or prompt.source_context_sha256 != result.source_context_sha256
        ):
            raise FlowPresubmitError(
                "SCENE_PROMPT_CONTRACT_MISMATCH", "approved prompt source context differs"
            )
        expected_hash = sha256_json(
            {
                "prompt": prompt.compiled_prompt_text,
                "negative_prompt": prompt.compiled_negative_prompt,
            }
        )
        if (
            not re.fullmatch(r"[a-f0-9]{64}", prompt.prompt_sha256)
            or prompt.prompt_sha256 != expected_hash
            or prompt.prompt_sha256 != result.prompt_sha256
        ):
            raise FlowPresubmitError(
                "SCENE_PROMPT_CONTRACT_MISMATCH", "approved prompt hash differs"
            )
        if (
            prompt.compiled_prompt_text != result.compiled_prompt
            or prompt.compiled_negative_prompt != (result.compiled_negative_prompt or "")
        ):
            raise FlowPresubmitError(
                "SCENE_PROMPT_CONTRACT_MISMATCH", "approved prompt text differs from Phase E"
            )
        self._ensure_temp_root(request.temp_root)
        resolved = result.resolved
        if resolved.get("provider_id") != request.provider_id:
            raise FlowPresubmitError("FLOW_PROVIDER_ID_MISMATCH", "Phase E provider differs")
        if resolved.get("provider_account_id") != request.provider_account_id:
            raise FlowPresubmitError("FLOW_ACCOUNT_MISMATCH", "Phase E account differs")
        if resolved.get("model_id") != request.model_id:
            raise FlowPresubmitError("FLOW_SETTING_VALUE_MISMATCH", "Phase E model differs")
        if resolved.get("generation_mode") != request.generation_mode:
            raise FlowPresubmitError(
                "FLOW_SETTING_VALUE_MISMATCH", "Phase E resolved generation mode differs"
            )
        technical = result.technical_configuration
        settings = {key: technical.get(key) for key in _EXPECTED_SETTINGS}
        settings["model_id"] = request.model_id
        settings["generation_mode"] = request.generation_mode
        if any(settings[key] is None for key in _EXPECTED_SETTINGS):
            raise FlowPresubmitError("FLOW_SETTING_UNRESOLVED", "Phase E settings are incomplete")
        if technical.get("generation_mode") != request.generation_mode:
            raise FlowPresubmitError(
                "FLOW_SETTING_VALUE_MISMATCH", "Phase E generation mode differs"
            )
        if request.generation_mode not in {
            "TEXT_TO_VIDEO",
            "FIRST_FRAME_TO_VIDEO",
            "FIRST_LAST_FRAME_TO_VIDEO",
            "INGREDIENTS_TO_VIDEO",
        }:
            raise FlowPresubmitError(
                "FLOW_MODE_NOT_SUPPORTED",
                "this Flow presubmit foundation does not bind this generation mode",
            )
        return settings

    @staticmethod
    def _ensure_temp_root(temp_root: Path) -> None:
        if not temp_root.is_absolute() or temp_root == Path(temp_root.anchor):
            raise FlowPresubmitError(
                "FLOW_DIAGNOSTIC_PATH_INVALID", "TEMP_ROOT must be a non-root absolute path"
            )

    @staticmethod
    def _validate_references(request: FlowPresubmitRequest) -> tuple[ProviderReferenceBinding, ...]:
        expected_assets = {ref.reference_asset_id: ref for ref in request.preflight.references}
        bindings = {item.reference_asset_id: item for item in request.references}
        if len(bindings) != len(request.references):
            raise FlowPresubmitError(
                "FLOW_REFERENCE_MAP_INVALID", "duplicate provider reference mapping"
            )
        if set(bindings) != set(expected_assets):
            raise FlowPresubmitError(
                "FLOW_REFERENCE_SYNC_REQUIRED", "reference provider mapping is incomplete"
            )
        roles = [ref.reference_role.upper() for ref in request.preflight.references]
        if request.generation_mode == "TEXT_TO_VIDEO" and roles:
            raise FlowPresubmitError(
                "FLOW_REFERENCE_MODE_MISMATCH", "TEXT_TO_VIDEO cannot invent reference assets"
            )
        if request.generation_mode == "FIRST_FRAME_TO_VIDEO" and roles.count("START_FRAME") != 1:
            raise FlowPresubmitError(
                "FLOW_REFERENCE_SLOT_MISMATCH", "first-frame mode requires exactly one START_FRAME"
            )
        if request.generation_mode == "FIRST_LAST_FRAME_TO_VIDEO" and (
            roles.count("START_FRAME") != 1 or roles.count("END_FRAME") != 1
        ):
            raise FlowPresubmitError(
                "FLOW_REFERENCE_SLOT_MISMATCH",
                "first/last-frame mode requires exact START_FRAME and END_FRAME",
            )
        for ref_id, binding in bindings.items():
            if binding.provider_id != request.provider_id:
                raise FlowPresubmitError(
                    "FLOW_REFERENCE_MAP_INVALID", "reference mapping belongs to another provider"
                )
            if not binding.provider_asset_id:
                raise FlowPresubmitError(
                    "FLOW_REFERENCE_SYNC_REQUIRED", "provider reference identity is missing"
                )
            if binding.provider_project_id != request.provider_project_id:
                raise FlowPresubmitError(
                    "FLOW_PROJECT_MISMATCH", "reference belongs to another Flow project"
                )
            role = expected_assets[ref_id].reference_role.upper()
            required_slot = {"START_FRAME": "START_FRAME", "END_FRAME": "END_FRAME"}.get(role)
            if required_slot and binding.frame_slot != required_slot:
                raise FlowPresubmitError(
                    "FLOW_REFERENCE_SLOT_MISMATCH", "frame reference is bound to the wrong slot"
                )
            if not required_slot and binding.frame_slot is not None:
                raise FlowPresubmitError(
                    "FLOW_REFERENCE_SLOT_MISMATCH", "non-frame reference has a frame slot"
                )
        return request.references

    @staticmethod
    def _compare_mapping(
        expected: Mapping[str, object], actual: Mapping[str, object], code: str
    ) -> None:
        if any(actual.get(key) != value for key, value in expected.items()):
            raise FlowPresubmitError(
                code, "Flow settings readback differs from requested canonical values"
            )

    @staticmethod
    def _compare_references(
        expected: Sequence[ProviderReferenceBinding], actual: Sequence[ProviderReferenceBinding]
    ) -> None:
        if tuple(expected) != tuple(actual):
            raise FlowPresubmitError(
                "FLOW_REFERENCE_BINDING_MISMATCH", "Flow reference identities or slots differ"
            )


def prompt_sha256(prompt: str, negative_prompt: str) -> str:
    """Expose the Phase E canonical prompt digest for persisted prompt row checks."""
    return sha256_json({"prompt": prompt, "negative_prompt": negative_prompt})

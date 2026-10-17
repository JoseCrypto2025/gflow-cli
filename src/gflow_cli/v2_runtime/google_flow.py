"""Google Flow V2 preparation contract; deliberately has no submission API."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from gflow_cli.api.transports._common import flow_host_kind
from gflow_cli.errors import ProfileLockedError
from gflow_cli.profile_lease import ProfileLease

from .agent_recovery import AgentSettingsRecoveryJournal, RecoveryJournalError
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


class AgentSettingsTransactionPort(FlowPreparationPort, Protocol):
    """Additional non-submitting surface needed for shared Agent defaults."""

    async def snapshot_settings(self) -> Mapping[str, object]: ...
    async def restore_settings(self, settings: Mapping[str, object]) -> None: ...


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


_SUPPORTED_SURFACES = frozenset({"LABS_CLASSIC", "FLOW_MIGRATED", "CLASSIC", "AGENT_ONLY"})
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
_AGENT_UI_SETTINGS = ("model_id", "generation_mode", "aspect_ratio", "output_count")
_AGENT_DURATION_PARAMETER_KEY = "google_flow.agent.video.duration_instruction"


def build_agent_only_directive(
    prompt_text: str, technical_configuration: Mapping[str, object]
) -> str:
    """Translate Phase E's structured duration into the Agent's text-only control."""
    canonical = technical_configuration.get("canonical_parameters")
    adapter = technical_configuration.get("adapter_parameters")
    duration = technical_configuration.get("duration_sec")
    canonical_values = cast(Mapping[str, object], canonical)
    adapter_values = cast(Mapping[str, object], adapter)
    if (
        not isinstance(canonical, Mapping)
        or not isinstance(adapter, Mapping)
        or isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or duration <= 0
        or canonical_values.get("DURATION_SEC") != duration
        or adapter_values.get(_AGENT_DURATION_PARAMETER_KEY) != duration
    ):
        raise FlowPresubmitError(
            "FLOW_DURATION_UNRESOLVED",
            "Phase E must provide a mapped, positive structured duration for Agent-only T2V",
        )
    seconds = format(float(duration), "g")
    return (
        f"{prompt_text}\n\n"
        f"Follow the selected video settings and target a duration of {seconds} seconds. "
        "Create video only; do not generate images first or ask follow-up questions."
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
            # Live ports expose their repository-resolved auth profile so the
            # one-lane lease protects the actual persistent browser state.
            profile_dir = getattr(port, "profile_dir", None)
            if not isinstance(profile_dir, Path):
                profile_dir = (
                    request.runtime_state_root / "browser-state" / request.provider_account_id
                )
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

        if surface == "AGENT_ONLY":
            agent_settings = self._agent_only_settings(request, settings)
            return await self._prepare_agent_only_transaction(
                request, port, agent_settings, references
            )

        if any(settings[key] is None for key in _EXPECTED_SETTINGS):
            raise FlowPresubmitError("FLOW_SETTING_UNRESOLVED", "Phase E settings are incomplete")

        return await self._prepare_after_surface(request, port, settings, references, surface)

    async def _prepare_agent_only_transaction(
        self,
        request: FlowPresubmitRequest,
        port: FlowPreparationPort,
        settings: Mapping[str, object],
        references: tuple[ProviderReferenceBinding, ...],
    ) -> FlowPresubmitResult:
        snapshot = getattr(port, "snapshot_settings", None)
        restore = getattr(port, "restore_settings", None)
        if not callable(snapshot) or not callable(restore):
            raise FlowPresubmitError(
                "FLOW_RECOVERY_UNAVAILABLE",
                "Agent-only preparation requires a settings snapshot and restore port",
            )
        transaction_port = cast(AgentSettingsTransactionPort, port)

        journal = AgentSettingsRecoveryJournal(
            request.runtime_state_root,
            request.provider_account_id,
            request.provider_project_id,
        )
        recovered_previous = False
        try:
            journal.require_clean_or_missing()
        except RecoveryJournalError:
            await self._recover_agent_settings(request, port, journal)
            try:
                journal.require_clean_or_missing()
            except RecoveryJournalError as still_dirty:
                raise FlowPresubmitError(
                    "FLOW_ACCOUNT_STATE_RECOVERY_REQUIRED",
                    "previous Agent settings could not be verified clean",
                ) from still_dirty
            recovered_previous = True

        if recovered_previous:
            # Recovery may have navigated to a prior project's URL. Re-select and
            # classify this request's explicitly configured project before work.
            project = await port.select_project(request.provider_project_id)
            if project.project_id != request.provider_project_id:
                raise FlowPresubmitError(
                    "FLOW_PROJECT_MISMATCH", "request project differs after recovery"
                )
            if await port.inspect_surface() != "AGENT_ONLY":
                raise FlowPresubmitError(
                    "FLOW_SURFACE_CHANGED", "request project is not on the Agent-only surface"
                )

        try:
            original: dict[str, object] = dict(await transaction_port.snapshot_settings())
            correlation_id = request.prompt_version.scene_prompt_id
            journal.begin(
                correlation_id=correlation_id,
                original_settings=original,
                intended_settings=dict(settings),
            )
        except RecoveryJournalError as exc:
            raise FlowPresubmitError(
                "FLOW_ACCOUNT_STATE_RECOVERY_REQUIRED",
                "Agent settings recovery journal could not be safely started",
            ) from exc

        result: FlowPresubmitResult | None = None
        operation_error: BaseException | None = None
        try:
            result = await self._prepare_after_surface(
                request, port, settings, references, "AGENT_ONLY"
            )
        except BaseException as exc:
            operation_error = exc

        try:
            journal.set_state("RESTORING")
            await asyncio.shield(transaction_port.restore_settings(original))
            final = await asyncio.shield(port.readback())
            if final.account_id != request.provider_account_id:
                raise FlowPresubmitError(
                    "FLOW_ACCOUNT_MISMATCH", "account changed during Agent settings restore"
                )
            if final.project_id != request.provider_project_id:
                raise FlowPresubmitError(
                    "FLOW_PROJECT_MISMATCH", "project changed during Agent settings restore"
                )
            if final.session_status != "FLOW_SESSION_READY":
                raise FlowPresubmitError(
                    final.session_status, "Flow session changed during Agent settings restore"
                )
            self._compare_mapping(original, final.settings, "FLOW_SETTING_RESTORE_MISMATCH")
            journal.set_state("CLEAN")
        except BaseException as restore_error:
            raise FlowPresubmitError(
                "FLOW_ACCOUNT_STATE_RECOVERY_REQUIRED",
                "Agent defaults could not be restored and verified",
            ) from restore_error

        if operation_error is not None:
            raise operation_error
        if result is None:  # defensive: the operation must either return or raise
            raise FlowPresubmitError(
                "FLOW_PRESUBMIT_FAILED", "Agent-only preparation returned no result"
            )
        return result

    async def _recover_agent_settings(
        self,
        request: FlowPresubmitRequest,
        port: FlowPreparationPort,
        journal: AgentSettingsRecoveryJournal,
    ) -> None:
        """Recover the exact prior project before accepting new account work."""
        restore = getattr(port, "restore_settings", None)
        if not callable(restore):
            raise FlowPresubmitError(
                "FLOW_ACCOUNT_STATE_RECOVERY_REQUIRED",
                "dirty Agent settings journal has no recovery-capable port",
            )
        transaction_port = cast(AgentSettingsTransactionPort, port)
        try:
            record = journal.read()
            if record is None or record.state == "CLEAN":
                return
            if record.provider_project_id != request.provider_project_id:
                raise FlowPresubmitError(
                    "FLOW_ACCOUNT_STATE_RECOVERY_REQUIRED",
                    "dirty Agent journal belongs to a different pinned project",
                )
            session = await port.inspect_session()
            if session.status != "FLOW_SESSION_READY":
                raise FlowPresubmitError(session.status, "Flow session is not ready for recovery")
            if session.account_id != request.provider_account_id:
                raise FlowPresubmitError("FLOW_ACCOUNT_MISMATCH", "recovery account differs")
            project = await port.select_project(record.provider_project_id)
            if project.project_id != record.provider_project_id:
                raise FlowPresubmitError("FLOW_PROJECT_MISMATCH", "recovery project differs")
            if await port.inspect_surface() != "AGENT_ONLY":
                raise FlowPresubmitError(
                    "FLOW_SURFACE_CHANGED", "recovery surface is not Agent-only"
                )
            journal.set_state("RESTORING")
            await asyncio.shield(transaction_port.restore_settings(record.original_settings))
            readback = await asyncio.shield(port.readback())
            if (
                readback.session_status != "FLOW_SESSION_READY"
                or readback.account_id != request.provider_account_id
                or readback.project_id != record.provider_project_id
            ):
                raise FlowPresubmitError(
                    "FLOW_ACCOUNT_STATE_RECOVERY_REQUIRED",
                    "Flow identity did not verify after settings recovery",
                )
            self._compare_mapping(
                record.original_settings,
                readback.settings,
                "FLOW_SETTING_RESTORE_MISMATCH",
            )
            journal.set_state("CLEAN")
        except (RecoveryJournalError, FlowPresubmitError) as exc:
            raise FlowPresubmitError(
                "FLOW_ACCOUNT_STATE_RECOVERY_REQUIRED",
                "prior Agent defaults remain unverified",
            ) from exc
        except Exception as exc:  # noqa: BLE001 — credentials/browser detail is not surfaced
            raise FlowPresubmitError(
                "FLOW_ACCOUNT_STATE_RECOVERY_REQUIRED",
                "prior Agent defaults remain unverified",
            ) from exc

    async def _prepare_after_surface(
        self,
        request: FlowPresubmitRequest,
        port: FlowPreparationPort,
        settings: Mapping[str, object],
        references: tuple[ProviderReferenceBinding, ...],
        surface: str,
    ) -> FlowPresubmitResult:

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
        provider_prompt = request.prompt_version.compiled_prompt_text
        if surface == "AGENT_ONLY":
            provider_prompt = build_agent_only_directive(
                provider_prompt, request.preflight.technical_configuration
            )
        await port.fill_prompt(provider_prompt)
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
        if readback.prompt != provider_prompt:
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
        expected_negative_prompt = request.prompt_version.compiled_negative_prompt or None
        if readback.negative_prompt != expected_negative_prompt:
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

    @staticmethod
    def _agent_only_settings(
        request: FlowPresubmitRequest, settings: Mapping[str, object]
    ) -> dict[str, object]:
        """Require known UI controls, keeping duration as a separate directive value."""
        if request.generation_mode != "TEXT_TO_VIDEO":
            raise FlowPresubmitError(
                "FLOW_AGENT_MODE_UNSUPPORTED", "Agent-only F5 supports text-to-video only"
            )
        for key in ("resolution", "audio_mode", "quality_tier"):
            # EXTERNAL_TTS is a downstream post-processing choice, not a Flow
            # generation setting; F5 does not execute or map that stage.
            if key == "audio_mode" and settings.get(key) == "EXTERNAL_TTS":
                continue
            if settings.get(key) is not None:
                raise FlowPresubmitError(
                    "FLOW_AGENT_SETTING_UNSUPPORTED",
                    f"Agent-only Flow has no verified {key} contract",
                )
        duration = settings.get("duration_sec")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise FlowPresubmitError(
                "FLOW_DURATION_UNRESOLVED",
                "Agent-only T2V requires a structured duration from Phase E",
            )
        build_agent_only_directive(
            request.prompt_version.compiled_prompt_text,
            request.preflight.technical_configuration,
        )
        selected = {key: settings.get(key) for key in _AGENT_UI_SETTINGS}
        if any(selected[key] is None for key in _AGENT_UI_SETTINGS):
            raise FlowPresubmitError(
                "FLOW_SETTING_UNRESOLVED", "required Agent video UI settings are incomplete"
            )
        return selected

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
        if any(settings[key] is None for key in ("duration_sec", "aspect_ratio", "output_count")):
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

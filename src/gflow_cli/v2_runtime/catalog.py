"""Master-data-backed profile resolution without embedding provider catalogs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from .errors import CatalogResolutionError


@dataclass(frozen=True, slots=True)
class ProviderRecord:
    provider_id: str
    provider_type: str
    status: str


@dataclass(frozen=True, slots=True)
class AccountRecord:
    provider_account_id: str
    provider_id: str
    environment: str
    status: str


@dataclass(frozen=True, slots=True)
class ModelRecord:
    model_id: str
    provider_id: str
    status: str


@dataclass(frozen=True, slots=True)
class CapabilityRecord:
    capability_type: str
    capability_value: str
    generation_mode: str | None = None
    status: str = "ACTIVE"


@dataclass(frozen=True, slots=True)
class PromptTemplateRecord:
    prompt_template_id: str
    status: str
    provider_id: str | None = None
    model_id: str | None = None
    generation_mode: str | None = None


@dataclass(frozen=True, slots=True)
class ProfileRecord:
    generation_profile_id: str
    provider_account_id: str
    model_id: str
    generation_mode: str
    environment: str
    purpose: str
    status: str
    prompt_template_id: str | None = None
    default_aspect_ratio: str | None = None
    default_duration_sec: float | None = None
    default_resolution: str | None = None
    default_output_count: int | None = None
    audio_mode: str | None = None
    quality_tier: str | None = None


class MasterCatalog(Protocol):
    """Read-only adapter over the approved Master tabs or a validated snapshot."""

    def provider(self, provider_id: str) -> ProviderRecord | None: ...
    def account(self, account_id: str) -> AccountRecord | None: ...
    def model(self, model_id: str) -> ModelRecord | None: ...
    def profile(self, profile_id: str) -> ProfileRecord | None: ...
    def prompt_template(self, prompt_template_id: str) -> PromptTemplateRecord | None: ...
    def capabilities(self, model_id: str) -> list[CapabilityRecord]: ...


def _values(capabilities: list[CapabilityRecord], kind: str, mode: str) -> set[str]:
    """Extract enumerated values from scalar rows or verified JSON capability bundles."""
    values: set[str] = set()
    bundle_keys = {
        "DURATION_SEC": "duration_sec",
        "ASPECT_RATIO": "aspect_ratios",
        "RESOLUTION": "resolutions",
        "OUTPUT_COUNT": "output_counts",
    }
    for cap in capabilities:
        if cap.status != "ACTIVE" or (cap.generation_mode and cap.generation_mode != mode):
            continue
        if cap.capability_type == kind:
            try:
                parsed = cast(object, json.loads(cap.capability_value))
            except (json.JSONDecodeError, TypeError):
                parsed = cap.capability_value
            if kind == "GENERATION_MODE" and isinstance(parsed, dict):
                parsed_map = cast(Mapping[str, object], parsed)
                if (
                    parsed_map.get("supported") is not False
                    and str(parsed_map.get("mode", "")).upper() == mode
                ):
                    values.add(mode)
                continue
            if isinstance(parsed, list):
                values.update(str(value) for value in cast(list[object], parsed))
            elif isinstance(parsed, dict):
                parsed_map = cast(Mapping[str, object], parsed)
                listed = parsed_map.get("values", parsed_map.get("supported_values"))
                if isinstance(listed, list):
                    values.update(str(value) for value in cast(list[object], listed))
            elif isinstance(parsed, str) and parsed:
                values.add(parsed)
            elif isinstance(parsed, (int, float, bool)):
                values.add(str(parsed))
        if cap.capability_type == "GENERATION_MODE":
            try:
                bundle = cast(object, json.loads(cap.capability_value))
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(bundle, dict):
                continue
            bundle_map = cast(Mapping[str, object], bundle)
            if (
                str(bundle_map.get("mode", "")).upper() not in {"", mode}
                or bundle_map.get("supported") is False
            ):
                continue
            key = bundle_keys.get(kind)
            candidate = bundle_map.get(key) if key else None
            if isinstance(candidate, list):
                values.update(str(value) for value in cast(list[object], candidate))
            elif isinstance(candidate, dict):
                candidate_map = cast(Mapping[str, object], candidate)
                listed = candidate_map.get("values", candidate_map.get("supported_values"))
                if isinstance(listed, list):
                    values.update(str(value) for value in cast(list[object], listed))
    return values


def resolve_profile(
    catalog: MasterCatalog,
    profile_id: str,
    environment: str,
    *,
    allow_unenumerable_capabilities: bool = False,
) -> tuple[ProfileRecord, ProviderRecord, AccountRecord, ModelRecord]:
    profile = catalog.profile(profile_id)
    if profile is None or profile.status != "ACTIVE":
        raise CatalogResolutionError(f"active generation profile not found: {profile_id}")
    runtime_environment = {"development": "DEV", "staging": "TEST", "production": "PROD"}.get(
        environment.lower(), environment.upper()
    )
    if profile.environment.upper() != runtime_environment:
        raise CatalogResolutionError("profile environment does not match runtime environment")
    account = catalog.account(profile.provider_account_id)
    model = catalog.model(profile.model_id)
    if account is None or account.status != "ACTIVE":
        raise CatalogResolutionError("profile provider account is missing or inactive")
    if model is None or model.status != "ACTIVE":
        raise CatalogResolutionError("profile model is missing or inactive")
    if account.environment != profile.environment:
        raise CatalogResolutionError("profile and provider account environments differ")
    if account.provider_id != model.provider_id:
        raise CatalogResolutionError("provider account and model belong to different providers")
    provider = catalog.provider(model.provider_id)
    if provider is None or provider.status != "ACTIVE":
        raise CatalogResolutionError("profile provider is missing or inactive")
    if profile.prompt_template_id:
        prompt = catalog.prompt_template(profile.prompt_template_id)
        if prompt is None or prompt.status != "ACTIVE":
            raise CatalogResolutionError("profile prompt template is missing or inactive")
        if prompt.provider_id and prompt.provider_id != provider.provider_id:
            raise CatalogResolutionError("profile prompt template belongs to another provider")
        if prompt.model_id and prompt.model_id != model.model_id:
            raise CatalogResolutionError("profile prompt template belongs to another model")
        if prompt.generation_mode and prompt.generation_mode != profile.generation_mode:
            raise CatalogResolutionError("profile prompt template uses a different generation mode")
    capabilities = catalog.capabilities(model.model_id)
    modes = _values(capabilities, "GENERATION_MODE", profile.generation_mode)
    if profile.generation_mode not in modes:
        raise CatalogResolutionError("generation mode is not supported by the model")
    checks = (
        ("ASPECT_RATIO", profile.default_aspect_ratio),
        (
            "DURATION_SEC",
            str(profile.default_duration_sec) if profile.default_duration_sec is not None else None,
        ),
        ("RESOLUTION", profile.default_resolution),
        (
            "OUTPUT_COUNT",
            str(profile.default_output_count) if profile.default_output_count is not None else None,
        ),
        ("AUDIO_MODE", profile.audio_mode),
        ("QUALITY_TIER", profile.quality_tier),
    )
    for kind, value in checks:
        if kind == "AUDIO_MODE" and profile.audio_mode == "EXTERNAL_TTS":
            # External TTS is a separate post-generation stage, not a model
            # capability that must be present in the video provider catalog.
            continue
        allowed = _values(capabilities, kind, profile.generation_mode)
        if value is not None and allowed:
            matches = value in allowed
            if kind in {"DURATION_SEC", "OUTPUT_COUNT"}:
                matches = any(float(value) == float(candidate) for candidate in allowed)
            if not matches:
                raise CatalogResolutionError(
                    f"profile {kind.lower()} is not supported by the model"
                )
        if value is not None and not allowed and not allow_unenumerable_capabilities:
            raise CatalogResolutionError(
                f"profile sets {kind.lower()} without verified capability data"
            )
    return profile, provider, account, model

"""Master-data-backed profile resolution without embedding provider catalogs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

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
class ProfileRecord:
    generation_profile_id: str
    provider_account_id: str
    model_id: str
    generation_mode: str
    environment: str
    purpose: str
    status: str
    default_aspect_ratio: str | None = None
    default_duration_sec: float | None = None
    default_resolution: str | None = None


class MasterCatalog(Protocol):
    """Read-only adapter over the approved Master tabs or a validated snapshot."""

    def provider(self, provider_id: str) -> ProviderRecord | None: ...
    def account(self, account_id: str) -> AccountRecord | None: ...
    def model(self, model_id: str) -> ModelRecord | None: ...
    def profile(self, profile_id: str) -> ProfileRecord | None: ...
    def capabilities(self, model_id: str) -> list[CapabilityRecord]: ...


def _values(capabilities: list[CapabilityRecord], kind: str, mode: str) -> set[str]:
    return {
        cap.capability_value
        for cap in capabilities
        if cap.status == "ACTIVE"
        and cap.capability_type == kind
        and (cap.generation_mode is None or cap.generation_mode == mode)
    }


def resolve_profile(
    catalog: MasterCatalog, profile_id: str, environment: str
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
    )
    for kind, value in checks:
        allowed = _values(capabilities, kind, profile.generation_mode)
        if value is not None and allowed:
            matches = value in allowed
            if kind == "DURATION_SEC":
                matches = any(float(value) == float(candidate) for candidate in allowed)
            if not matches:
                raise CatalogResolutionError(
                    f"profile {kind.lower()} is not supported by the model"
                )
        if value is not None and not allowed:
            raise CatalogResolutionError(
                f"profile sets {kind.lower()} without verified capability data"
            )
    return profile, provider, account, model

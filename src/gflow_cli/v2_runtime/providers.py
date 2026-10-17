"""Service-type routing through a small adapter registry."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .catalog import MasterCatalog, resolve_profile
from .errors import AdapterUnavailableError, CatalogResolutionError


class ServiceType(StrEnum):
    VIDEO_GENERATION = "VIDEO_GENERATION"
    IMAGE_GENERATION = "IMAGE_GENERATION"
    TTS = "TTS"


_SERVICE_TYPE_ALIASES = {"VIDEO_GENERATION_PLATFORM": ServiceType.VIDEO_GENERATION}


def _service_type(provider_type: str) -> ServiceType | None:
    """Normalize the existing Master label to the V2 service contract."""
    if provider_type in _SERVICE_TYPE_ALIASES:
        return _SERVICE_TYPE_ALIASES[provider_type]
    try:
        return ServiceType(provider_type)
    except ValueError:
        return None


class ProviderAdapter(Protocol):
    provider_id: str
    provider_type: str

    def prepare_request(self, profile_id: str, payload: dict[str, object]) -> dict[str, object]: ...


class ResultNormalizer(Protocol):
    """Provider-specific response conversion into the neutral output contract."""

    def normalize(self, provider_result: object) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class ResolvedRoute:
    profile_id: str
    provider_id: str
    provider_type: str
    account_id: str
    model_id: str
    generation_mode: str
    service_type: ServiceType


class ProviderRouter:
    """Resolve adapters by stable provider identity and verify service type."""

    def __init__(
        self, catalog: MasterCatalog, adapters: dict[str, ProviderAdapter], environment: str
    ) -> None:
        self.catalog = catalog
        self.adapters = adapters
        self.environment = environment

    def route(
        self, profile_id: str, service_type: ServiceType
    ) -> tuple[ResolvedRoute, ProviderAdapter]:
        profile, provider, account, model = resolve_profile(
            self.catalog, profile_id, self.environment
        )
        if _service_type(provider.provider_type) != service_type:
            raise CatalogResolutionError(
                "Provider_Master.Provider_Type does not match requested service type"
            )
        adapter = self.adapters.get(provider.provider_id)
        if adapter is None:
            raise AdapterUnavailableError(f"no adapter registered for {provider.provider_id}")
        if adapter.provider_id != provider.provider_id:
            raise CatalogResolutionError(
                "registered adapter identity does not match provider master data"
            )
        if _service_type(adapter.provider_type) != service_type:
            raise CatalogResolutionError(
                "registered adapter service type does not match requested service type"
            )
        return (
            ResolvedRoute(
                profile.generation_profile_id,
                provider.provider_id,
                provider.provider_type,
                account.provider_account_id,
                model.model_id,
                profile.generation_mode,
                service_type,
            ),
            adapter,
        )


class ContractOnlyAdapter:
    """Placeholder adapter that does not contact Google Flow or paid APIs."""

    def __init__(self, provider_id: str, provider_type: str | None = None) -> None:
        self.provider_id = provider_id
        self.provider_type = provider_type or provider_id

    def prepare_request(self, profile_id: str, payload: dict[str, object]) -> dict[str, object]:
        raise AdapterUnavailableError("provider execution is disabled in the V2 foundation")

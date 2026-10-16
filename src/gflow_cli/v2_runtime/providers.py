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


class ProviderAdapter(Protocol):
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
    """Resolve all routing from Master data; adapter keys remain semantic."""

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
        if provider.provider_type != service_type.value:
            raise CatalogResolutionError(
                "Provider_Master.Provider_Type does not match requested service type"
            )
        adapter = self.adapters.get(provider.provider_type)
        if adapter is None:
            raise AdapterUnavailableError(f"no adapter registered for {provider.provider_type}")
        if adapter.provider_type != provider.provider_type:
            raise CatalogResolutionError(
                "registered adapter type does not match provider master data"
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

    def __init__(self, provider_type: str) -> None:
        self.provider_type = provider_type

    def prepare_request(self, profile_id: str, payload: dict[str, object]) -> dict[str, object]:
        raise AdapterUnavailableError("provider execution is disabled in the V2 foundation")

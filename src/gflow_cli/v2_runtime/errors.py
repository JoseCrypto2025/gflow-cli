"""Stable machine-readable V2 runtime failures."""


class V2RuntimeError(Exception):
    """Base exception carrying a stable code for job/API boundaries."""

    code = "V2_RUNTIME_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"error_code": self.code, "message": self.message}


class ConfigurationError(V2RuntimeError):
    code = "CONFIGURATION_INVALID"


class UnsafePathError(V2RuntimeError):
    code = "UNSAFE_WORKSPACE_PATH"


class DiskSpaceError(V2RuntimeError):
    code = "INSUFFICIENT_DISK_SPACE"


class AssetVerificationError(V2RuntimeError):
    code = "ASSET_VERIFICATION_FAILED"


class CatalogResolutionError(V2RuntimeError):
    code = "CATALOG_RESOLUTION_FAILED"


class AdapterUnavailableError(V2RuntimeError):
    code = "PROVIDER_ADAPTER_UNAVAILABLE"


class FlowPresubmitError(V2RuntimeError):
    """Deterministic hold or failure from the non-submitting Flow presubmit path."""

    code = "FLOW_PRESUBMIT_HOLD"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FlowSubmissionDisabledError(FlowPresubmitError):
    code = "FLOW_SUBMISSION_DISABLED"

    def __init__(self, message: str) -> None:
        super().__init__(self.code, message)

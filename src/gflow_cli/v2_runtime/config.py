"""Environment-driven settings for the isolated Linux-oriented V2 worker."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    environment: str
    storage_provider: str
    runtime_state_root: Path
    temp_root: Path
    log_root: Path
    min_free_disk_gb: float = 10.0
    keep_failed_job_hours: int = 72
    success_temp_retention_minutes: int = 60
    log_level: str = "INFO"


def _validate_disjoint_roots(*roots: tuple[str, Path]) -> None:
    """Reject overlapping roots so temporary cleanup cannot reach durable state."""
    resolved = [(name, path.resolve(strict=False)) for name, path in roots]
    for index, (name, path) in enumerate(resolved):
        for other_name, other_path in resolved[index + 1 :]:
            if path == other_path or path in other_path.parents or other_path in path.parents:
                raise ConfigurationError(f"{name} and {other_name} roots must be disjoint")


def load_runtime_settings(env: Mapping[str, str] | None = None) -> RuntimeSettings:
    """Parse V2 settings without reading or resolving any secret values."""
    values = os.environ if env is None else env
    environment = values.get("RUNTIME_ENV", "development").strip().lower()
    if environment not in {"development", "staging", "production"}:
        raise ConfigurationError("RUNTIME_ENV must be development, staging, or production")
    storage_provider = values.get("STORAGE_PROVIDER", "").strip().lower()
    root_value = values.get("RUNTIME_STATE_ROOT", "")
    temp_value = values.get("TEMP_ROOT", "")
    log_value = values.get("LOG_ROOT", "")
    if environment == "production" and not storage_provider:
        raise ConfigurationError("production requires STORAGE_PROVIDER")
    if environment == "production" and not all((root_value, temp_value, log_value)):
        raise ConfigurationError("production requires explicit state, temp, and log roots")
    if (
        os.name == "nt"
        and environment == "development"
        and not any((root_value, temp_value, log_value))
    ):
        development_root = Path.cwd() / ".gflow-v2"
        root = development_root / "state"
        temp = development_root / "temp"
        logs = development_root / "logs"
    else:
        root = Path(root_value or "/var/lib/gflow").expanduser()
        temp = Path(temp_value or "/var/tmp/gflow").expanduser()
        logs = Path(log_value or "/var/log/gflow").expanduser()
    for label, path in (("state", root), ("temp", temp), ("log", logs)):
        if os.name == "nt" and environment != "development":
            if not path.is_absolute() or "\\" in str(path):
                raise ConfigurationError(f"{label} root must be an absolute POSIX path")
        elif not path.is_absolute():
            raise ConfigurationError(f"{label} root must be an absolute POSIX path")
        if path == Path("/"):
            raise ConfigurationError(f"{label} root cannot be filesystem root")
    _validate_disjoint_roots(("state", root), ("temp", temp), ("log", logs))
    try:
        min_free = float(values.get("MIN_FREE_DISK_GB", "10"))
        failed_hours = int(values.get("KEEP_FAILED_JOB_HOURS", "72"))
        success_minutes = int(values.get("SUCCESS_TEMP_RETENTION_MINUTES", "60"))
    except ValueError as exc:
        raise ConfigurationError("retention and disk threshold settings must be numeric") from exc
    if min_free < 0 or failed_hours < 0 or success_minutes < 0:
        raise ConfigurationError("disk threshold and retention settings cannot be negative")
    level = values.get("LOG_LEVEL", "INFO").upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError("LOG_LEVEL is invalid")
    if storage_provider and not storage_provider.replace("-", "").replace("_", "").isalnum():
        raise ConfigurationError("STORAGE_PROVIDER contains invalid characters")
    return RuntimeSettings(
        environment,
        storage_provider,
        root,
        temp,
        logs,
        min_free,
        failed_hours,
        success_minutes,
        level,
    )

"""Disk-space gate for job admission; it never terminates running work."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .errors import DiskSpaceError
from .timestamps import format_utc_timestamp


@dataclass(frozen=True, slots=True)
class DiskGuardResult:
    allowed: bool
    path: Path
    free_bytes: int
    total_bytes: int
    required_bytes: int
    job_id: str | None
    checked_at: str

    @property
    def status(self) -> str:
        return "DISK_GUARD_PASS" if self.allowed else "DISK_GUARD_HOLD"


def check_free_space(
    path: Path, minimum_bytes: int, estimated_job_bytes: int = 0, job_id: str | None = None
) -> DiskGuardResult:
    if minimum_bytes < 0 or estimated_job_bytes < 0:
        raise ValueError("disk-space requirements cannot be negative")
    usage = shutil.disk_usage(path)
    free = usage.free
    required = minimum_bytes + estimated_job_bytes
    return DiskGuardResult(
        free >= required,
        path,
        free,
        usage.total,
        required,
        job_id,
        format_utc_timestamp(datetime.now(UTC), timespec="auto"),
    )


def require_free_space(
    path: Path, minimum_bytes: int, estimated_job_bytes: int = 0, job_id: str | None = None
) -> DiskGuardResult:
    result = check_free_space(path, minimum_bytes, estimated_job_bytes, job_id)
    if not result.allowed:
        raise DiskSpaceError(
            f"free bytes {result.free_bytes} below required bytes {result.required_bytes}"
        )
    return result

"""Contained per-job temporary workspace and conservative cleanup policy."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .errors import UnsafePathError

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
LAYOUT = ("input", "generated", "audio", "ffmpeg", "validated", "logs")


@dataclass(frozen=True, slots=True)
class JobWorkspace:
    job_id: str
    root: Path
    directories: dict[str, Path]


def _safe_root(base: Path, job_id: str) -> tuple[Path, Path]:
    if not JOB_ID_RE.fullmatch(job_id):
        raise UnsafePathError("job ID contains unsafe path characters")
    base.mkdir(parents=True, exist_ok=True)
    if base.is_symlink():
        raise UnsafePathError("temporary root cannot be a symlink")
    canonical_base = base.resolve(strict=True)
    if canonical_base == Path(canonical_base.anchor):
        raise UnsafePathError("temporary root cannot be filesystem root")
    target = canonical_base / job_id
    if target.exists() and target.is_symlink():
        raise UnsafePathError("job workspace cannot be a symlink")
    if target.resolve(strict=False).parent != canonical_base:
        raise UnsafePathError("job workspace escaped the configured temp root")
    return canonical_base, target


def create_workspace(temp_root: Path, job_id: str) -> JobWorkspace:
    _, root = _safe_root(temp_root, job_id)
    root.mkdir(exist_ok=False)
    directories = {name: root / name for name in LAYOUT}
    for directory in directories.values():
        directory.mkdir()
    return JobWorkspace(job_id, root, directories)


def cleanup_workspace(
    temp_root: Path,
    job_id: str,
    job_status: str,
    created_at: datetime,
    now: datetime | None = None,
    failed_retention: timedelta = timedelta(hours=72),
    success_retention: timedelta = timedelta(minutes=60),
    remote_assets_verified: bool = False,
) -> bool:
    """Remove only an expired terminal job directory; active/unknown jobs are retained."""
    if job_status not in {"SUCCESS", "FAILED", "CANCELLED"}:
        return False
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("created_at must be timezone aware")
    if job_status == "SUCCESS" and not remote_assets_verified:
        return False
    age = (now or datetime.now(UTC)) - created_at.astimezone(UTC)
    retention = success_retention if job_status == "SUCCESS" else failed_retention
    if age < retention:
        return False
    canonical_base, target = _safe_root(temp_root, job_id)
    if not target.exists():
        return False
    resolved_target = target.resolve(strict=True)
    if resolved_target.parent != canonical_base or target.is_symlink():
        raise UnsafePathError("refusing cleanup outside the configured temp root")
    shutil.rmtree(resolved_target)
    return True

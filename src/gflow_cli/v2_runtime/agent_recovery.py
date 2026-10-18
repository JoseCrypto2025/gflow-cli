"""Crash-safe journal for temporary Agent-only Flow defaults.

The journal records semantic settings only. It deliberately rejects credential-
shaped keys and writes through an atomic replace so a worker restart cannot
mistake a partial file for a clean settings transaction.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from .errors import V2RuntimeError
from .timestamps import format_utc_timestamp

JournalState = Literal["CLEAN", "DIRTY", "RESTORING"]
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PROJECT_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CORRELATION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ALLOWED_SETTINGS = frozenset(
    {
        "model_id",
        "generation_mode",
        "aspect_ratio",
        "output_count",
        "duration_sec",
        "resolution",
        "audio_mode",
        "quality_tier",
        "confirm_before_generating",
    }
)


class RecoveryJournalError(V2RuntimeError):
    """A journal is missing, malformed, unsafe, or cannot be durably updated."""


@dataclass(frozen=True, slots=True)
class RecoveryRecord:
    """Credential-free snapshot required to restore one Flow project lane."""

    provider_account_id: str
    provider_project_id: str
    correlation_id: str
    surface: str
    original_settings: dict[str, object]
    intended_settings: dict[str, object]
    state: JournalState
    created_at: str
    updated_at: str


def _validate_settings(value: dict[str, object]) -> dict[str, object]:
    if any(key not in _ALLOWED_SETTINGS for key in value):
        raise RecoveryJournalError("recovery journal contains a non-semantic setting")
    try:
        # Round-trip through JSON to reject non-serializable objects and NaN/Infinity.
        result: object = json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise RecoveryJournalError("recovery journal settings are not valid JSON") from exc
    if not isinstance(result, dict):
        raise RecoveryJournalError("recovery journal settings must be an object")
    return cast(dict[str, object], result)


class AgentSettingsRecoveryJournal:
    """Atomic, portable recovery record under ``RUNTIME_STATE_ROOT/recovery``."""

    def __init__(self, runtime_state_root: Path, provider_account_id: str, project_id: str):
        if not runtime_state_root.is_absolute() or runtime_state_root == Path(
            runtime_state_root.anchor
        ):
            raise RecoveryJournalError("runtime state root must be absolute and non-root")
        if not _ACCOUNT_RE.fullmatch(provider_account_id):
            raise RecoveryJournalError("provider account ID is invalid")
        if not _PROJECT_RE.fullmatch(project_id):
            raise RecoveryJournalError("provider project ID is invalid")
        self.root = runtime_state_root / "recovery" / "agent-settings"
        # Agent defaults are account-level state. A project switch must still
        # discover and recover the prior transaction's record.
        self.path = self.root / f"{provider_account_id}.json"
        self.provider_account_id = provider_account_id
        self.project_id = project_id

    def read(self) -> RecoveryRecord | None:
        if not self.path.exists():
            return None
        if self.path.is_symlink():
            raise RecoveryJournalError("recovery journal cannot be a symlink")
        try:
            payload_value: object = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload_value, dict):
                raise ValueError("record is not an object")
            payload = cast(dict[str, object], payload_value)
            expected_keys = {
                "provider_account_id",
                "provider_project_id",
                "correlation_id",
                "surface",
                "original_settings",
                "intended_settings",
                "state",
                "created_at",
                "updated_at",
            }
            if set(payload) != expected_keys:
                raise ValueError("record contains missing or unexpected fields")
            string_fields = (
                "provider_account_id",
                "provider_project_id",
                "correlation_id",
                "surface",
                "created_at",
                "updated_at",
            )
            if any(not isinstance(payload[key], str) for key in string_fields):
                raise ValueError("record contains a non-string identity or timestamp")
            original_value = payload["original_settings"]
            intended_value = payload["intended_settings"]
            state_value = payload["state"]
            if not isinstance(original_value, dict) or not isinstance(intended_value, dict):
                raise ValueError("record settings must be objects")
            if state_value not in {"CLEAN", "DIRTY", "RESTORING"}:
                raise ValueError("record state is invalid")
            record = RecoveryRecord(
                provider_account_id=cast(str, payload["provider_account_id"]),
                provider_project_id=cast(str, payload["provider_project_id"]),
                correlation_id=cast(str, payload["correlation_id"]),
                surface=cast(str, payload["surface"]),
                original_settings=_validate_settings(cast(dict[str, object], original_value)),
                intended_settings=_validate_settings(cast(dict[str, object], intended_value)),
                state=cast(JournalState, state_value),
                created_at=cast(str, payload["created_at"]),
                updated_at=cast(str, payload["updated_at"]),
            )
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise RecoveryJournalError("recovery journal is unreadable or malformed") from exc
        if record.provider_account_id != self.provider_account_id:
            raise RecoveryJournalError("recovery journal account does not match its path")
        if not _PROJECT_RE.fullmatch(record.provider_project_id):
            raise RecoveryJournalError("recovery journal project ID is invalid")
        if record.state not in {"CLEAN", "DIRTY", "RESTORING"}:
            raise RecoveryJournalError("recovery journal state is invalid")
        if not _CORRELATION_RE.fullmatch(record.correlation_id):
            raise RecoveryJournalError("recovery journal correlation ID is invalid")
        return record

    def begin(
        self,
        *,
        correlation_id: str,
        original_settings: dict[str, object],
        intended_settings: dict[str, object],
    ) -> RecoveryRecord:
        """Persist DIRTY before any settings mutation is attempted."""
        if not _CORRELATION_RE.fullmatch(correlation_id):
            raise RecoveryJournalError("recovery journal correlation ID is invalid")
        previous = self.read()
        if previous is not None and previous.state != "CLEAN":
            raise RecoveryJournalError("unclean Agent settings journal blocks new work")
        now = _utc_now()
        record = RecoveryRecord(
            self.provider_account_id,
            self.project_id,
            correlation_id,
            "AGENT_ONLY",
            _validate_settings(original_settings),
            _validate_settings(intended_settings),
            "DIRTY",
            previous.created_at if previous else now,
            now,
        )
        self._write(record)
        return record

    def set_state(self, state: JournalState) -> RecoveryRecord:
        current = self.read()
        if current is None:
            raise RecoveryJournalError("recovery journal does not exist")
        updated = RecoveryRecord(
            current.provider_account_id,
            current.provider_project_id,
            current.correlation_id,
            current.surface,
            current.original_settings,
            current.intended_settings,
            state,
            current.created_at,
            _utc_now(),
        )
        self._write(updated)
        return updated

    def require_clean_or_missing(self) -> RecoveryRecord | None:
        """Refuse new work until a prior DIRTY/RESTORING transaction is recovered."""
        record = self.read()
        if record is not None and record.state != "CLEAN":
            raise RecoveryJournalError("Agent settings recovery is required before new work")
        return record

    def _write(self, record: RecoveryRecord) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            if self.root.is_symlink() or self.path.is_symlink():
                raise RecoveryJournalError("recovery journal path cannot be a symlink")
            root = self.root.resolve(strict=True)
            if root == Path(root.anchor) or self.path.resolve(strict=False).parent != root:
                raise RecoveryJournalError("recovery journal path escaped its root")
            payload = {
                "provider_account_id": record.provider_account_id,
                "provider_project_id": record.provider_project_id,
                "correlation_id": record.correlation_id,
                "surface": record.surface,
                "original_settings": record.original_settings,
                "intended_settings": record.intended_settings,
                "state": record.state,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
            }
            fd, temp_name = tempfile.mkstemp(prefix=".agent-settings-", suffix=".tmp", dir=root)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                    json.dump(
                        payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, self.path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        except RecoveryJournalError:
            raise
        except OSError as exc:
            raise RecoveryJournalError("recovery journal could not be written atomically") from exc


def _utc_now() -> str:
    return format_utc_timestamp(datetime.now(UTC), timespec="seconds")

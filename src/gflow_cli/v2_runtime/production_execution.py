"""Environment-pinned production execution identity and shared credit guard.

This module is offline-safe. It makes no Flow, Google, or browser calls; the
existing Phase G browser executor remains the sole provider interaction path.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from .timestamps import format_utc_timestamp

_BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")
_TERMINAL = frozenset({"SUCCESS", "PROVIDER_REJECTED", "FAILED_CONFIRMED", "WAF_REJECTION"})


class ProductionExecutionError(ValueError):
    """Safe, stable fail-closed production precondition or ledger error."""


@dataclass(frozen=True, slots=True)
class CreditBudgetContext:
    """One quote-time view of provider free quota and user authorization."""

    daily_user_ceiling: Decimal = Decimal("50")
    provider_remaining_free: Decimal | None = None
    quote_unit: str = "CREDIT"
    is_free_quota: bool = True
    paid_fallback: bool = False

    def __post_init__(self) -> None:
        try:
            ceiling = Decimal(str(self.daily_user_ceiling))
            remaining = (
                Decimal(str(self.provider_remaining_free))
                if self.provider_remaining_free is not None
                else None
            )
        except InvalidOperation as exc:
            raise ProductionExecutionError("credit budget value is invalid") from exc
        if not ceiling.is_finite() or ceiling != Decimal("50"):
            raise ProductionExecutionError("daily free credit ceiling must remain 50")
        if self.quote_unit != "CREDIT":
            raise ProductionExecutionError("provider quote must be expressed in credits")
        if remaining is not None and (not remaining.is_finite() or remaining < 0):
            raise ProductionExecutionError("provider free quota cannot be negative")


@dataclass(frozen=True, slots=True)
class ProductionExecutionContext:
    """Immutable identity tuple required before reusing the Phase G executor."""

    environment: str
    provider_id: str
    provider_account_id: str
    provider_project_id: str
    auth_profile: str
    scene_id: str
    generation_profile_id: str
    scene_prompt_id: str
    attempt_id: str
    credit_budget_context: CreditBudgetContext

    def validate(self, resolved: dict[str, str]) -> None:
        if self.environment not in {"DEV", "TEST", "PROD"}:
            raise ProductionExecutionError("execution environment is invalid")
        expected = {
            "environment": self.environment,
            "provider_id": self.provider_id,
            "provider_account_id": self.provider_account_id,
            "provider_project_id": self.provider_project_id,
            "auth_profile": self.auth_profile,
            "scene_id": self.scene_id,
            "generation_profile_id": self.generation_profile_id,
            "scene_prompt_id": self.scene_prompt_id,
        }
        if any(not value for value in expected.values()) or not self.attempt_id:
            raise ProductionExecutionError("execution identity is incomplete")
        if resolved != expected:
            raise ProductionExecutionError(
                "resolved provider identity differs from execution context"
            )


class ProductionCreditGuard:
    """One append-only provider/account/Bangkok-day budget shared by environments."""

    def __init__(self, runtime_root: Path) -> None:
        if not runtime_root.is_absolute() or runtime_root == Path(runtime_root.anchor):
            raise ProductionExecutionError("credit guard root must be absolute and non-root")
        self.root = runtime_root / "production-credit"
        self.path = self.root / "shared-ledger.json"
        self.lock_path = self.root / ".shared-ledger.lock"
        self.thread_lock = threading.RLock()

    def prepare(
        self, context: ProductionExecutionContext, *, now: datetime | None = None
    ) -> dict[str, object]:
        moment = now or datetime.now(UTC)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ProductionExecutionError("credit guard timestamp must be timezone-aware")
        day = moment.astimezone(_BANGKOK).date().isoformat()

        def update(payload: dict[str, object]) -> dict[str, object]:
            records = cast(list[dict[str, object]], payload["attempts"])
            existing = next(
                (row for row in records if row.get("attempt_id") == context.attempt_id), None
            )
            # Provider quota belongs to the authenticated user, not an
            # environment-specific Provider Account row.  Scope it to the
            # provider and Bangkok date so TEST/PROD cannot create two pools.
            identity = (context.provider_id, day)
            if existing is not None:
                if (
                    (existing.get("provider_id"), existing.get("date")) != identity
                    or existing.get("provider_account_id") != context.provider_account_id
                    or existing.get("provider_project_id") != context.provider_project_id
                    or existing.get("environment") != context.environment
                ):
                    raise ProductionExecutionError(
                        "attempt ID is already bound to another identity"
                    )
                return dict(existing)
            if any(
                (row.get("provider_id"), row.get("date")) == identity
                and row.get("state") not in _TERMINAL
                for row in records
            ):
                raise ProductionExecutionError("an unresolved provider attempt already exists")
            if any(
                (row.get("provider_id"), row.get("date")) == identity
                and row.get("state") in {"SUCCESS", "WAF_REJECTION"}
                for row in records
            ):
                raise ProductionExecutionError("provider generation stop condition is active")
            row: dict[str, object] = {
                "attempt_id": context.attempt_id,
                "environment": context.environment,
                "provider_id": context.provider_id,
                "provider_account_id": context.provider_account_id,
                "provider_project_id": context.provider_project_id,
                "date": day,
                "state": "PREPARED",
                "generate_click_count": 0,
                "quoted_credits": None,
                "reserved_credits": "0",
                "created_at": format_utc_timestamp(moment),
            }
            records.append(row)
            return dict(row)

        return self._mutate(update)

    def record_generate_click(self, attempt_id: str) -> None:
        def update(payload: dict[str, object]) -> dict[str, object]:
            row = self._find(payload, attempt_id)
            if row.get("state") != "PREPARED" or row.get("generate_click_count") != 0:
                raise ProductionExecutionError(
                    "attempt is not eligible for its single Generate click"
                )
            row["generate_click_count"] = 1
            row["state"] = "SUBMISSION_UNRESOLVED"
            return dict(row)

        self._mutate(update)

    def reserve_quote(
        self,
        attempt_id: str,
        *,
        video_count: int | None,
        quoted_credits: Decimal | int | float | str | None,
        budget: CreditBudgetContext,
    ) -> Decimal:
        quote: Decimal | None = None
        if quoted_credits is not None:
            try:
                quote = Decimal(str(quoted_credits))
            except InvalidOperation as exc:
                raise ProductionExecutionError("provider credit quote is invalid") from exc
        if video_count != 1 or quote is None or not quote.is_finite() or quote <= 0:
            raise ProductionExecutionError(
                "one video and a known positive credit quote are required"
            )
        if not budget.is_free_quota or budget.paid_fallback:
            raise ProductionExecutionError("paid fallback or non-free quota is not authorized")
        if budget.provider_remaining_free is None:
            raise ProductionExecutionError("current provider free quota is unavailable")
        if quote > budget.provider_remaining_free:
            raise ProductionExecutionError("quote exceeds current provider free quota")

        def update(payload: dict[str, object]) -> dict[str, object]:
            row = self._find(payload, attempt_id)
            if row.get("state") != "SUBMISSION_UNRESOLVED" or row.get("generate_click_count") != 1:
                raise ProductionExecutionError("attempt has no single unresolved Generate action")
            used = sum(
                (
                    Decimal(str(item.get("reserved_credits", "0")))
                    for item in cast(list[dict[str, object]], payload["attempts"])
                    if item.get("provider_id") == row.get("provider_id")
                    and item.get("date") == row.get("date")
                ),
                Decimal(0),
            )
            if used + quote > budget.daily_user_ceiling:
                raise ProductionExecutionError(
                    "quote exceeds remaining shared daily free credit ceiling"
                )
            row["quoted_credits"] = str(quote)
            row["reserved_credits"] = str(quote)
            row["state"] = "QUOTED_RESERVED"
            return dict(row)

        self._mutate(update)
        return quote

    def resolve(
        self,
        attempt_id: str,
        state: str,
        *,
        actual_credits: Decimal | int | float | str | None = None,
    ) -> None:
        if state not in _TERMINAL | {"UNKNOWN_SUBMISSION_STATE"}:
            raise ProductionExecutionError("attempt terminal state is invalid")
        actual: Decimal | None = None
        if actual_credits is not None:
            try:
                actual = Decimal(str(actual_credits))
            except InvalidOperation as exc:
                raise ProductionExecutionError("actual credit usage is invalid") from exc
            if not actual.is_finite() or actual < 0:
                raise ProductionExecutionError("actual credit usage is invalid")

        def update(payload: dict[str, object]) -> dict[str, object]:
            row = self._find(payload, attempt_id)
            if row.get("state") in _TERMINAL:
                raise ProductionExecutionError("terminal attempt cannot be changed")
            if row.get("generate_click_count") != 1:
                raise ProductionExecutionError("attempt has no recorded Generate click")
            if actual is not None:
                other_reserved = sum(
                    (
                        Decimal(str(item.get("reserved_credits", "0")))
                        for item in cast(list[dict[str, object]], payload["attempts"])
                        if item.get("provider_id") == row.get("provider_id")
                        and item.get("date") == row.get("date")
                        and item.get("attempt_id") != attempt_id
                    ),
                    Decimal(0),
                )
                if other_reserved + actual > Decimal("50"):
                    raise ProductionExecutionError(
                        "actual provider usage exceeds the shared daily free credit ceiling"
                    )
            if state == "UNKNOWN_SUBMISSION_STATE":
                row["reserved_credits"] = str(row.get("quoted_credits") or Decimal("50"))
            elif actual is not None:
                row["reserved_credits"] = str(actual)
            row["state"] = state
            return dict(row)

        self._mutate(update)

    def fail_before_submit(self, attempt_id: str) -> None:
        """Release a prepared identity when no Generate click was recorded."""

        def update(payload: dict[str, object]) -> dict[str, object]:
            row = self._find(payload, attempt_id)
            if row.get("state") != "PREPARED" or row.get("generate_click_count") != 0:
                raise ProductionExecutionError("only an unsubmitted attempt can be released")
            row["state"] = "FAILED_CONFIRMED"
            row["reserved_credits"] = "0"
            return dict(row)

        self._mutate(update)

    def _find(self, payload: dict[str, object], attempt_id: str) -> dict[str, object]:
        for row in cast(list[dict[str, object]], payload["attempts"]):
            if row.get("attempt_id") == attempt_id:
                return row
        raise ProductionExecutionError("production attempt does not exist")

    def _mutate(
        self, operation: Callable[[dict[str, object]], dict[str, object]]
    ) -> dict[str, object]:
        with self._file_lock():
            payload = self._read()
            result = operation(payload)
            self.root.mkdir(parents=True, exist_ok=True)
            root = self.root.resolve(strict=True)
            if self.root.is_symlink() or self.path.resolve(strict=False).parent != root:
                raise ProductionExecutionError("credit ledger path is unsafe")
            fd, temp_name = tempfile.mkstemp(prefix=".credit-ledger-", dir=root)
            try:
                if os.name == "posix":
                    os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                    json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, self.path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            return result

    def _read(self) -> dict[str, object]:
        if not self.path.exists():
            return {"schema_version": 1, "attempts": []}
        if self.path.is_symlink():
            raise ProductionExecutionError("credit ledger may not be a symlink")
        try:
            value: object = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProductionExecutionError("credit ledger is unreadable") from exc
        if not isinstance(value, dict):
            raise ProductionExecutionError("credit ledger has an invalid shape")
        payload = cast(dict[str, object], value)
        if payload.get("schema_version") != 1 or not isinstance(payload.get("attempts"), list):
            raise ProductionExecutionError("credit ledger has an invalid shape")
        return payload

    def _file_lock(self):
        return _CreditFileLock(self)


class _CreditFileLock:
    def __init__(self, guard: ProductionCreditGuard) -> None:
        self.guard = guard
        self.fd: int | None = None

    def __enter__(self):
        self.guard.root.mkdir(parents=True, exist_ok=True)
        if self.guard.root.is_symlink() or self.guard.lock_path.is_symlink():
            raise ProductionExecutionError("credit ledger lock path is unsafe")
        self.guard.thread_lock.acquire()
        try:
            self.fd = os.open(self.guard.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            if os.name == "nt":
                import msvcrt

                if os.fstat(self.fd).st_size == 0:
                    os.write(self.fd, b"0")
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_EX)
            return self
        except BaseException:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            self.guard.thread_lock.release()
            raise

    def __exit__(self, *_args: object) -> None:
        if self.fd is not None:
            if os.name == "nt":
                import msvcrt

                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
        self.guard.thread_lock.release()

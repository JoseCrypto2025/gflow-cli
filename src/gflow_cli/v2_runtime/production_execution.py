"""Environment-pinned production execution identity and shared credit guard.

This module is offline-safe. It makes no Flow, Google, or browser calls; the
existing Phase G browser executor remains the sole provider interaction path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from .timestamps import format_utc_timestamp

_BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")
_TERMINAL = frozenset(
    {
        "SUCCESS",
        "PROVIDER_REJECTED",
        "FAILED_CONFIRMED",
        "WAF_REJECTION",
        "UPSTREAM_PRE_SUBMIT_FAILED",
    }
)
_PROD_PROVIDER_ACCOUNT_ID = "PVA_0002"
_PROD_PROVIDER_PROJECT_ID = "aafe9282-071f-4333-baa4-92b9f76a9c27"
_PROD_AUTH_PROFILE = "GFLOW_PROD_AUTH"
_RUN3_CORRECTION_ATTEMPT_ID = "D2_5d54c068c457e7cf6fbc7a5319f312ce"
_RUN3_CORRECTION_PRODUCTION_JOB_ID = "PRD_000003"
_RUN3_CORRECTION_SCENE_JOB_ID = "SJB_000003"
_RUN3_CORRECTION_BEFORE_ROW_SHA256 = (
    "daa32f2e98254231ee28b6bdab02e210fe8a0f261ed1b894860e0e88873c5514"
)
_RUN3_CORRECTION_BEFORE_LEDGER_SHA256 = (
    "01f8b0c1fba8c6fef332389f623ecd942d0636e049e59f1da309122689eb6e2b"
)
_RUN3_CORRECTION_QUOTE_INPUT_DIGEST = (
    "8c07afd5d56aefc154eb367434003b513e5af8ffcd6fd71a997f807d9c79daf7"
)
_RUN3_CORRECTION_QUOTE_SOURCE_VERSION = (
    "PCT_000001:2026-10-28T15:09:16Z:"
    "ee1074ae02f4ed3a9fc49759d4502815605b9479f4ce311d945713bdc3b380cc"
)


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
    authorized_run_ceiling: Decimal | None = None

    def __post_init__(self) -> None:
        try:
            ceiling = Decimal(str(self.daily_user_ceiling))
            run_ceiling = (
                Decimal(str(self.authorized_run_ceiling))
                if self.authorized_run_ceiling is not None
                else None
            )
            remaining = (
                Decimal(str(self.provider_remaining_free))
                if self.provider_remaining_free is not None
                else None
            )
        except InvalidOperation as exc:
            raise ProductionExecutionError("credit budget value is invalid") from exc
        if not ceiling.is_finite() or ceiling != Decimal("50"):
            raise ProductionExecutionError("daily free credit ceiling must remain 50")
        if run_ceiling is not None and (
            not run_ceiling.is_finite() or run_ceiling <= 0 or run_ceiling > ceiling
        ):
            raise ProductionExecutionError("authorized per-run credit ceiling is invalid")
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
    prerequisite_d2_run_id: str | None = None
    production_job_id: str | None = None
    scene_job_id: str | None = None
    legacy_replacement_authorization_id: str | None = None
    legacy_replacement_parent_attempt_id: str | None = None
    legacy_replacement_quote_credits: Decimal | None = None
    legacy_replacement_quote_input_digest: str | None = None
    legacy_replacement_quote_source_version: str | None = None
    max_reservation_credits: Decimal | None = None
    reservation_quote_input_digest: str | None = None
    reservation_quote_source_version: str | None = None
    production_run_no: int | None = None
    request_idempotency_key: str | None = None

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
        if self.environment == "PROD" and (
            self.provider_account_id != _PROD_PROVIDER_ACCOUNT_ID
            or self.provider_project_id != _PROD_PROVIDER_PROJECT_ID
            or self.auth_profile != _PROD_AUTH_PROFILE
        ):
            raise ProductionExecutionError("PROD execution identity is not canonically pinned")
        if self.environment == "PROD" and (not self.production_job_id or not self.scene_job_id):
            raise ProductionExecutionError("PROD execution must bind production and scene jobs")
        if self.production_run_no is not None and (
            self.environment != "PROD"
            or type(self.production_run_no) is not int
            or self.production_run_no <= 0
        ):
            raise ProductionExecutionError("production run number is invalid")
        if self.request_idempotency_key is not None and (
            self.environment != "PROD"
            or type(self.request_idempotency_key) is not str
            or not self.request_idempotency_key.strip()
        ):
            raise ProductionExecutionError("production request idempotency key is invalid")
        if self.max_reservation_credits is not None and (
            not self.max_reservation_credits.is_finite() or self.max_reservation_credits <= 0
        ):
            raise ProductionExecutionError("per-run reservation quote cap is invalid")
        quote_cap_values = (
            self.max_reservation_credits,
            self.reservation_quote_input_digest,
            self.reservation_quote_source_version,
        )
        if any(value is not None for value in quote_cap_values) and (
            self.environment != "PROD"
            or self.max_reservation_credits is None
            or not isinstance(self.reservation_quote_input_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.reservation_quote_input_digest)
            or not isinstance(self.reservation_quote_source_version, str)
            or not self.reservation_quote_source_version.strip()
        ):
            raise ProductionExecutionError("per-run reservation quote capability is incomplete")
        replacement_values = (
            self.legacy_replacement_authorization_id,
            self.legacy_replacement_parent_attempt_id,
            self.legacy_replacement_quote_credits,
            self.legacy_replacement_quote_input_digest,
            self.legacy_replacement_quote_source_version,
        )
        if any(value is not None for value in replacement_values):
            if (
                self.environment != "PROD"
                or not self.production_job_id
                or not self.scene_job_id
                or not isinstance(self.legacy_replacement_authorization_id, str)
                or not re.fullmatch(r"LRA_[0-9A-F]{24}", self.legacy_replacement_authorization_id)
                or not isinstance(self.legacy_replacement_parent_attempt_id, str)
                or not re.fullmatch(
                    r"D2_[A-Za-z0-9_-]{8,64}", self.legacy_replacement_parent_attempt_id
                )
                or self.legacy_replacement_quote_credits is None
                or not self.legacy_replacement_quote_credits.is_finite()
                or self.legacy_replacement_quote_credits <= 0
                or not isinstance(self.legacy_replacement_quote_input_digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", self.legacy_replacement_quote_input_digest)
                or not isinstance(self.legacy_replacement_quote_source_version, str)
                or not self.legacy_replacement_quote_source_version.strip()
            ):
                raise ProductionExecutionError("legacy replacement capability is incomplete")
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
                    or existing.get("production_job_id") != context.production_job_id
                    or existing.get("scene_job_id") != context.scene_job_id
                ):
                    raise ProductionExecutionError(
                        "attempt ID is already bound to another identity"
                    )
                return dict(existing)
            if context.environment == "PROD" and context.max_reservation_credits is None:
                if context.legacy_replacement_quote_credits is None:
                    raise ProductionExecutionError(
                        "validated canonical per-run quote is required for PROD"
                    )
                quote_cap = context.legacy_replacement_quote_credits
                quote_digest = context.legacy_replacement_quote_input_digest
                quote_version = context.legacy_replacement_quote_source_version
            else:
                quote_cap = context.max_reservation_credits
                quote_digest = context.reservation_quote_input_digest
                quote_version = context.reservation_quote_source_version
            authorized_ceiling: Decimal | None = None
            if (
                context.environment == "PROD"
                and context.legacy_replacement_authorization_id is None
            ):
                authorized_ceiling = context.credit_budget_context.authorized_run_ceiling
                if authorized_ceiling is None:
                    raise ProductionExecutionError(
                        "authorized per-run credit ceiling is required for PROD"
                    )
                if quote_cap is not None and quote_cap > authorized_ceiling:
                    raise ProductionExecutionError(
                        "validated quote exceeds the authorized per-run credit ceiling"
                    )
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
            if (
                context.environment == "PROD"
                and context.legacy_replacement_authorization_id is None
                and quote_cap is not None
            ):
                prior_reservations = sum(
                    (
                        Decimal(str(row.get("reserved_credits", "0")))
                        for row in records
                        if (row.get("provider_id"), row.get("date")) == identity
                    ),
                    Decimal(0),
                )
                if (
                    prior_reservations + quote_cap
                    > context.credit_budget_context.daily_user_ceiling
                ):
                    raise ProductionExecutionError(
                        "validated quote exceeds remaining shared daily free credit ceiling"
                    )
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
            if quote_cap is not None:
                if (
                    not quote_cap.is_finite()
                    or quote_cap <= 0
                    or not quote_digest
                    or not quote_version
                ):
                    raise ProductionExecutionError(
                        "validated canonical per-run quote capability is incomplete"
                    )
                row["max_reservation_credits"] = str(quote_cap)
                row["reservation_quote_input_digest"] = quote_digest
                row["reservation_quote_source_version"] = quote_version
                if authorized_ceiling is not None:
                    row["validated_quote_credits"] = str(quote_cap)
                    row["authorized_run_ceiling"] = str(authorized_ceiling)
            if context.environment == "PROD":
                row["production_job_id"] = context.production_job_id
                row["scene_job_id"] = context.scene_job_id
            records.append(row)
            return dict(row)

        return self._mutate(update)

    def prepare_upstream_canary(
        self,
        context: ProductionExecutionContext,
        *,
        request_idempotency_key: str,
        quoted_credits: Decimal | int | float | str,
        budget: CreditBudgetContext,
        binding: dict[str, str | int],
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Atomically reserve an exact quote and persist a one-shot canary checkpoint."""
        try:
            quote = Decimal(str(quoted_credits))
        except InvalidOperation as exc:
            raise ProductionExecutionError("upstream canary quote is invalid") from exc
        if (
            context.environment != "PROD"
            or context.max_reservation_credits is None
            or quote != context.max_reservation_credits
            or not quote.is_finite()
            or quote <= 0
            or budget.authorized_run_ceiling is None
            or quote > budget.authorized_run_ceiling
            or not budget.is_free_quota
            or budget.paid_fallback
            or budget.provider_remaining_free is None
            or quote > budget.provider_remaining_free
            or not request_idempotency_key.strip()
            or context.request_idempotency_key != request_idempotency_key
            or not context.production_job_id
            or not context.scene_job_id
        ):
            raise ProductionExecutionError("upstream canary quote or identity is not authorized")
        required = {
            "canary_invocation_id",
            "authorization_id",
            "prompt_id",
            "prompt_sha256",
            "profile_name",
            "auth_profile",
            "account_email",
            "project_id",
            "model",
            "aspect_ratio",
            "duration_seconds",
            "output_count",
            "adapter_commit",
            "upstream_version",
            "upstream_commit",
            "output_path",
            "verification_temp_dir",
            "quote_read_at",
        }
        if not required.issubset(binding) or any(not str(binding[key]).strip() for key in required):
            raise ProductionExecutionError("upstream canary checkpoint binding is incomplete")
        if binding["canary_invocation_id"] != context.attempt_id:
            raise ProductionExecutionError("upstream canary invocation identity differs")
        duration_seconds = binding["duration_seconds"]
        output_count = binding["output_count"]
        if (
            binding["prompt_id"] != context.scene_prompt_id
            or binding["project_id"] != context.provider_project_id
            or binding["auth_profile"] != context.auth_profile
            or type(duration_seconds) is not int
            or duration_seconds <= 0
            or type(output_count) is not int
            or output_count != 1
            or binding["aspect_ratio"] not in {"9:16", "16:9"}
            or not re.fullmatch(r"[0-9a-f]{64}", str(binding["prompt_sha256"]))
            or not re.fullmatch(r"[0-9a-f]{40}", str(binding["adapter_commit"]))
            or not re.fullmatch(r"[0-9a-f]{40}", str(binding["upstream_commit"]))
        ):
            raise ProductionExecutionError("upstream canary checkpoint binding is invalid")
        moment = now or datetime.now(UTC)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ProductionExecutionError("credit guard timestamp must be timezone-aware")
        day = moment.astimezone(_BANGKOK).date().isoformat()

        def update(payload: dict[str, object]) -> dict[str, object]:
            records = cast(list[dict[str, object]], payload["attempts"])
            existing = next(
                (row for row in records if row.get("attempt_id") == context.attempt_id), None
            )
            expected_binding = {f"upstream_{key}": value for key, value in binding.items()}
            if existing is not None:
                identity_binding = {
                    "environment": context.environment,
                    "provider_id": context.provider_id,
                    "provider_account_id": context.provider_account_id,
                    "provider_project_id": context.provider_project_id,
                    "production_job_id": context.production_job_id,
                    "scene_job_id": context.scene_job_id,
                }
                if any(existing.get(key) != value for key, value in identity_binding.items()):
                    raise ProductionExecutionError("upstream canary replay identity differs")
                if any(existing.get(key) != value for key, value in expected_binding.items()):
                    raise ProductionExecutionError("upstream canary replay binding differs")
                if existing.get("request_idempotency_key") != request_idempotency_key:
                    raise ProductionExecutionError("upstream canary replay key differs")
                if (
                    existing.get("quoted_credits") != str(quote)
                    or existing.get("reserved_credits")
                    != (
                        "0" if existing.get("state") == "UPSTREAM_PRE_SUBMIT_FAILED" else str(quote)
                    )
                    or existing.get("reservation_quote_input_digest")
                    != context.reservation_quote_input_digest
                    or existing.get("reservation_quote_source_version")
                    != context.reservation_quote_source_version
                    or existing.get("authorized_run_ceiling") != str(budget.authorized_run_ceiling)
                ):
                    raise ProductionExecutionError("upstream canary replay quote differs")
                return dict(existing)
            prior_key = next(
                (
                    row
                    for row in records
                    if row.get("upstream_canary") is True
                    and row.get("request_idempotency_key") == request_idempotency_key
                ),
                None,
            )
            if prior_key is not None:
                raise ProductionExecutionError("request key is bound to another canary invocation")
            if any(
                (row.get("provider_id"), row.get("date")) == (context.provider_id, day)
                and row.get("state") not in _TERMINAL
                for row in records
            ):
                raise ProductionExecutionError("an unresolved provider attempt already exists")
            reserved = sum(
                (
                    Decimal(str(row.get("reserved_credits", "0")))
                    for row in records
                    if (row.get("provider_id"), row.get("date")) == (context.provider_id, day)
                ),
                Decimal(0),
            )
            if reserved + quote > budget.daily_user_ceiling:
                raise ProductionExecutionError("upstream canary exceeds the shared daily ceiling")
            row: dict[str, object] = {
                "attempt_id": context.attempt_id,
                "environment": "PROD",
                "provider_id": context.provider_id,
                "provider_account_id": context.provider_account_id,
                "provider_project_id": context.provider_project_id,
                "production_job_id": context.production_job_id,
                "scene_job_id": context.scene_job_id,
                "date": day,
                "state": "UPSTREAM_READY",
                "upstream_canary": True,
                "request_idempotency_key": request_idempotency_key,
                "upstream_invocation_count": 0,
                "generate_click_count": 0,
                "quoted_credits": str(quote),
                "reserved_credits": str(quote),
                "max_reservation_credits": str(context.max_reservation_credits),
                "reservation_quote_input_digest": context.reservation_quote_input_digest,
                "reservation_quote_source_version": context.reservation_quote_source_version,
                "authorized_run_ceiling": str(budget.authorized_run_ceiling),
                "checkpoint_state": "READY_FOR_UPSTREAM_INVOCATION",
                "created_at": format_utc_timestamp(moment),
                **expected_binding,
            }
            records.append(row)
            return dict(row)

        return self._mutate(update)

    def start_upstream_invocation(self, attempt_id: str) -> dict[str, object]:
        """Persist invocation-start before launch; a replay can never start twice."""

        def update(payload: dict[str, object]) -> dict[str, object]:
            row = self._find(payload, attempt_id)
            if row.get("upstream_canary") is not True:
                raise ProductionExecutionError("attempt is not an upstream canary")
            if row.get("state") != "UPSTREAM_READY" or row.get("upstream_invocation_count") != 0:
                raise ProductionExecutionError("upstream canary invocation is already consumed")
            row["state"] = "UPSTREAM_INVOCATION_STARTED"
            row["checkpoint_state"] = "UPSTREAM_INVOCATION_STARTED"
            row["upstream_invocation_count"] = 1
            row["invocation_started_at"] = format_utc_timestamp(datetime.now(UTC))
            return dict(row)

        return self._mutate(update)

    def upstream_canary_row(self, attempt_id: str) -> dict[str, object]:
        """Read one durable canary row under the shared ledger lock."""
        with self._file_lock():
            row = self._find(self._read(), attempt_id)
            if row.get("upstream_canary") is not True:
                raise ProductionExecutionError("attempt is not an upstream canary")
            return dict(row)

    def record_upstream_result(
        self,
        attempt_id: str,
        *,
        status: str,
        result_fields: Mapping[str, object],
    ) -> dict[str, object]:
        """Persist an allowlisted normalized result without raw subprocess output."""
        allowed = {
            "UPSTREAM_GENERATION_COMPLETED",
            "PRE_SUBMIT_FAILURE",
            "AUTH_FAILURE",
            "PROJECT_NAVIGATION_FAILURE",
            "UNSUPPORTED_FLOW_STATE",
            "INSUFFICIENT_CREDITS",
            "POST_SUBMIT_UNKNOWN",
            "DOWNLOAD_FAILURE_AFTER_GENERATION",
        }
        if status not in allowed:
            status = "POST_SUBMIT_UNKNOWN"
        allowed_fields = {
            "upstream_operation_id",
            "media_id",
            "stdout_digest",
            "stderr_digest",
            "normalized_error_code",
            "upstream_error_type",
            "submit_may_have_occurred",
            "exit_code",
            "submission_state",
            "generate_action_observed",
            "generate_action_count",
            "submit_action_observed",
            "submit_transport_observed",
            "submit_timestamp",
            "provider_request_id",
            "provider_operation_id",
            "provider_media_id",
            "process_started_at",
            "process_ended_at",
            "process_duration_ms",
            "process_return_code",
            "process_signal",
            "process_timed_out",
            "process_killed_by_caller",
            "process_stdout_byte_count",
            "process_stderr_byte_count",
            "process_stdout_sha256",
            "process_stderr_sha256",
            "process_output_truncated",
            "parse_error_type",
            "parse_error_message",
            "output_top_level_type",
            "safe_stdout_prefix",
            "safe_stdout_suffix",
            "safe_stderr_prefix",
            "safe_stderr_suffix",
        }
        safe_fields = {
            key: value
            for key, value in result_fields.items()
            if key in allowed_fields
            and isinstance(value, (str, int, bool, type(None)))
            and not (isinstance(value, str) and len(value) > 512)
        }

        def update(payload: dict[str, object]) -> dict[str, object]:
            row = self._find(payload, attempt_id)
            if row.get("upstream_canary") is not True:
                raise ProductionExecutionError("attempt is not an upstream canary")
            if row.get("normalized_status") is not None:
                if (
                    row.get("normalized_status") == status
                    and row.get("normalized_result") == safe_fields
                ):
                    return dict(row)
                raise ProductionExecutionError("upstream result replay differs")
            if row.get("state") != "UPSTREAM_INVOCATION_STARTED":
                raise ProductionExecutionError("upstream invocation has not been durably started")
            row["normalized_status"] = status
            row["normalized_result"] = safe_fields
            proven_pre_submit = status in {
                "PRE_SUBMIT_FAILURE",
                "AUTH_FAILURE",
                "UNSUPPORTED_FLOW_STATE",
                "INSUFFICIENT_CREDITS",
            }
            if proven_pre_submit and safe_fields.get("submit_may_have_occurred") is False:
                row["state"] = "UPSTREAM_PRE_SUBMIT_FAILED"
                row["checkpoint_state"] = "PRE_SUBMIT_FAILURE"
                row["reserved_credits"] = "0"
            elif status == "UPSTREAM_GENERATION_COMPLETED":
                row["state"] = "UPSTREAM_ARTIFACT_PENDING"
                row["checkpoint_state"] = "UPSTREAM_ARTIFACT_PENDING"
            elif status == "DOWNLOAD_FAILURE_AFTER_GENERATION":
                row["state"] = "UPSTREAM_DOWNLOAD_RECOVERY_REQUIRED"
                row["checkpoint_state"] = "UPSTREAM_DOWNLOAD_RECOVERY_REQUIRED"
            else:
                row["state"] = "UPSTREAM_UNKNOWN_HOLD"
                row["checkpoint_state"] = "UPSTREAM_UNKNOWN_HOLD"
            events_value = row.get("upstream_evidence_events")
            if events_value is None:
                events: list[dict[str, object]] = []
                row["upstream_evidence_events"] = events
            elif isinstance(events_value, list):
                events = cast(list[dict[str, object]], events_value)
            else:
                raise ProductionExecutionError("upstream evidence event ledger is invalid")
            events.append(
                {
                    "sequence": len(events) + 1,
                    "event_type": "NORMALIZED_OUTCOME",
                    "recorded_at": format_utc_timestamp(datetime.now(UTC)),
                    "evidence": {
                        "normalized_status": status,
                        "submission_state": safe_fields.get("submission_state", "UNKNOWN"),
                        "normalized_error_code": safe_fields.get("normalized_error_code"),
                    },
                }
            )
            return dict(row)

        return self._mutate(update)

    def record_upstream_evidence(
        self,
        attempt_id: str,
        *,
        event_type: str,
        evidence: dict[str, object],
    ) -> dict[str, object]:
        """Append one bounded, allowlisted observation to the started invocation row."""
        event_fields = {
            "PROCESS_STARTED": {"process_started_at"},
            "SUBMIT_OBSERVATION": {
                "generate_action_observed",
                "generate_action_count",
                "submit_action_observed",
                "submit_transport_observed",
                "submit_timestamp",
                "provider_request_id",
                "operation_id",
                "media_id",
                "provider_status",
                "submission_state",
            },
            "PROCESS_ENDED": {
                "process_ended_at",
                "process_duration_ms",
                "process_return_code",
                "process_signal",
                "process_timed_out",
                "process_killed_by_caller",
                "process_stdout_byte_count",
                "process_stderr_byte_count",
                "process_stdout_sha256",
                "process_stderr_sha256",
                "process_output_truncated",
            },
            "PROVIDER_IDENTIFIED": {
                "provider_request_id",
                "operation_id",
                "media_id",
                "provider_status",
                "submission_state",
            },
            "PARSER_RESULT": {"parse_result", "output_top_level_type"},
            "PARSE_DIAGNOSTIC": {
                "parse_error_type",
                "parse_error_message",
                "output_top_level_type",
                "safe_stdout_prefix",
                "safe_stdout_suffix",
                "safe_stderr_prefix",
                "safe_stderr_suffix",
            },
        }
        allowed = event_fields.get(event_type)
        if allowed is None:
            raise ProductionExecutionError("upstream evidence event type is not allowlisted")
        safe_evidence = {
            key: value
            for key, value in evidence.items()
            if key in allowed
            and isinstance(value, (str, int, bool, type(None)))
            and not (isinstance(value, str) and len(value) > 512)
        }

        def update(payload: dict[str, object]) -> dict[str, object]:
            row = self._find(payload, attempt_id)
            if row.get("upstream_canary") is not True:
                raise ProductionExecutionError("attempt is not an upstream canary")
            if row.get("state") != "UPSTREAM_INVOCATION_STARTED":
                raise ProductionExecutionError("upstream invocation is not active")
            events_value = row.get("upstream_evidence_events")
            if events_value is None:
                events: list[dict[str, object]] = []
                row["upstream_evidence_events"] = events
            elif isinstance(events_value, list):
                events = cast(list[dict[str, object]], events_value)
            else:
                raise ProductionExecutionError("upstream evidence event ledger is invalid")
            events.append(
                {
                    "sequence": len(events) + 1,
                    "event_type": event_type,
                    "recorded_at": format_utc_timestamp(datetime.now(UTC)),
                    "evidence": safe_evidence,
                }
            )
            return dict(row)

        return self._mutate(update)

    def record_upstream_artifact(
        self,
        attempt_id: str,
        *,
        remote_file_id: str,
        local_sha256: str,
        remote_sha256: str,
    ) -> dict[str, object]:
        """Record Drive verification while leaving ordinary provider lineage unresolved."""
        if (
            not remote_file_id.strip()
            or not re.fullmatch(r"[0-9a-f]{64}", local_sha256)
            or remote_sha256 != local_sha256
        ):
            raise ProductionExecutionError("upstream artifact verification is incomplete")

        def update(payload: dict[str, object]) -> dict[str, object]:
            row = self._find(payload, attempt_id)
            if row.get("state") == "UPSTREAM_ARTIFACT_PERSISTED":
                if (
                    row.get("remote_file_id") != remote_file_id
                    or row.get("local_sha256") != local_sha256
                    or row.get("remote_sha256") != remote_sha256
                ):
                    raise ProductionExecutionError("persisted canary artifact replay differs")
                return dict(row)
            if row.get("state") not in {
                "UPSTREAM_ARTIFACT_PENDING",
                "UPSTREAM_DOWNLOAD_RECOVERY_REQUIRED",
            }:
                raise ProductionExecutionError("upstream artifact is not ready for persistence")
            row["state"] = "UPSTREAM_ARTIFACT_PERSISTED"
            row["checkpoint_state"] = "UPSTREAM_ARTIFACT_PERSISTED"
            row["remote_file_id"] = remote_file_id
            row["local_sha256"] = local_sha256
            row["remote_sha256"] = remote_sha256
            return dict(row)

        return self._mutate(update)

    def record_generate_click(self, attempt_id: str) -> None:
        self._record_generate_click(attempt_id, replacement_authorization_id=None)

    def record_replacement_generate_click(self, attempt_id: str, *, authorization_id: str) -> None:
        self._record_generate_click(attempt_id, replacement_authorization_id=authorization_id)

    def _record_generate_click(
        self, attempt_id: str, *, replacement_authorization_id: str | None
    ) -> None:
        def update(payload: dict[str, object]) -> dict[str, object]:
            row = self._find(payload, attempt_id)
            replacement_reserved = row.get("state") == "PRE_GENERATE_RESERVED"
            if (
                row.get("generate_click_count") != 0
                or (
                    replacement_reserved
                    and row.get("legacy_replacement_authorization_id")
                    != replacement_authorization_id
                )
                or (
                    not replacement_reserved
                    and (row.get("state") != "PREPARED" or replacement_authorization_id is not None)
                )
            ):
                raise ProductionExecutionError(
                    "attempt is not eligible for its single Generate click"
                )
            row["generate_click_count"] = 1
            row["state"] = "SUBMISSION_UNRESOLVED"
            return dict(row)

        self._mutate(update)

    def reserve_replacement_quote_before_submit(
        self,
        context: ProductionExecutionContext,
        *,
        quoted_credits: Decimal | int | float | str,
        budget: CreditBudgetContext,
        now: datetime | None = None,
    ) -> Decimal:
        """Reserve one exact approved replacement quote before its provider click."""
        authorization_id = context.legacy_replacement_authorization_id
        parent_attempt_id = context.legacy_replacement_parent_attempt_id
        quote_input_digest = context.legacy_replacement_quote_input_digest
        quote_source_version = context.legacy_replacement_quote_source_version
        expected_quote = context.legacy_replacement_quote_credits
        if (
            authorization_id is None
            or parent_attempt_id is None
            or quote_input_digest is None
            or quote_source_version is None
            or expected_quote is None
        ):
            raise ProductionExecutionError("legacy replacement capability is incomplete")
        try:
            quote = Decimal(str(quoted_credits))
        except InvalidOperation as exc:
            raise ProductionExecutionError("replacement quote is invalid") from exc
        if (
            not quote.is_finite()
            or quote <= 0
            or quote != expected_quote
            or quote > budget.daily_user_ceiling
            or not budget.is_free_quota
            or budget.paid_fallback
            or budget.provider_remaining_free is None
            or quote > budget.provider_remaining_free
        ):
            raise ProductionExecutionError("replacement quote exceeds its exact credit capability")
        moment = now or datetime.now(UTC)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ProductionExecutionError("credit guard timestamp must be timezone-aware")
        day = moment.astimezone(_BANGKOK).date().isoformat()

        def update(payload: dict[str, object]) -> dict[str, object]:
            records = cast(list[dict[str, object]], payload["attempts"])
            row = self._find(payload, context.attempt_id)
            expected_identity = (
                context.provider_id,
                day,
                context.provider_account_id,
                context.provider_project_id,
                context.environment,
                context.production_job_id,
                context.scene_job_id,
            )
            actual_identity = (
                row.get("provider_id"),
                row.get("date"),
                row.get("provider_account_id"),
                row.get("provider_project_id"),
                row.get("environment"),
                row.get("production_job_id"),
                row.get("scene_job_id"),
            )
            if actual_identity != expected_identity:
                raise ProductionExecutionError("replacement reservation identity differs")
            binding = {
                "legacy_replacement_authorization_id": authorization_id,
                "legacy_replacement_parent_attempt_id": parent_attempt_id,
                "legacy_replacement_quote_input_digest": quote_input_digest,
                "legacy_replacement_quote_source_version": quote_source_version,
            }
            if row.get("state") == "PRE_GENERATE_RESERVED":
                if (
                    any(row.get(key) != value for key, value in binding.items())
                    or row.get("quoted_credits") != str(quote)
                    or row.get("reserved_credits") != str(quote)
                ):
                    raise ProductionExecutionError("replacement reservation replay differs")
                return dict(row)
            if row.get("state") != "PREPARED" or row.get("generate_click_count") != 0:
                raise ProductionExecutionError("replacement attempt is not pre-Generate")
            if any(
                item.get("attempt_id") != context.attempt_id
                and item.get("provider_id") == context.provider_id
                and item.get("date") == day
                and item.get("state") not in _TERMINAL
                for item in records
            ):
                raise ProductionExecutionError("an unresolved provider attempt already exists")
            if any(
                item.get("provider_id") == context.provider_id
                and item.get("date") == day
                and item.get("state") in {"SUCCESS", "WAF_REJECTION"}
                for item in records
            ):
                raise ProductionExecutionError("provider generation stop condition is active")
            already_reserved = sum(
                (
                    Decimal(str(item.get("reserved_credits", "0")))
                    for item in records
                    if item.get("provider_id") == context.provider_id
                    and item.get("date") == day
                    and item.get("attempt_id") != context.attempt_id
                ),
                Decimal(0),
            )
            if already_reserved + quote > budget.daily_user_ceiling:
                raise ProductionExecutionError(
                    "quote exceeds remaining shared daily free credit ceiling"
                )
            row.update(binding)
            row["quoted_credits"] = str(quote)
            row["reserved_credits"] = str(quote)
            row["state"] = "PRE_GENERATE_RESERVED"
            row["reservation_created_at"] = format_utc_timestamp(moment)
            return dict(row)

        self._mutate(update)
        return quote

    def verify_replacement_confirmation(
        self,
        context: ProductionExecutionContext,
        *,
        video_count: int | None,
        quoted_credits: Decimal | int | float | str | None,
    ) -> Decimal:
        """Require Flow's confirmation to equal the already reserved quote."""
        authorization_id = context.legacy_replacement_authorization_id
        expected_quote = context.legacy_replacement_quote_credits
        if authorization_id is None or expected_quote is None:
            raise ProductionExecutionError("legacy replacement capability is incomplete")
        try:
            quote = Decimal(str(quoted_credits)) if quoted_credits is not None else None
        except InvalidOperation as exc:
            raise ProductionExecutionError("provider credit quote is invalid") from exc
        if video_count != 1 or quote is None or not quote.is_finite() or quote != expected_quote:
            raise ProductionExecutionError(
                "Flow confirmation differs from reserved replacement quote"
            )
        with self._file_lock():
            row = self._find(self._read(), context.attempt_id)
            if (
                row.get("state") != "SUBMISSION_UNRESOLVED"
                or row.get("generate_click_count") != 1
                or row.get("legacy_replacement_authorization_id") != authorization_id
                or row.get("legacy_replacement_parent_attempt_id")
                != context.legacy_replacement_parent_attempt_id
                or row.get("legacy_replacement_quote_input_digest")
                != context.legacy_replacement_quote_input_digest
                or row.get("legacy_replacement_quote_source_version")
                != context.legacy_replacement_quote_source_version
                or row.get("quoted_credits") != str(quote)
                or row.get("reserved_credits") != str(quote)
            ):
                raise ProductionExecutionError("persisted replacement reservation differs")
        return quote

    def release_replacement_before_submit(self, context: ProductionExecutionContext) -> None:
        """Release only an exact pre-Generate replacement reservation with zero clicks."""
        authorization_id = context.legacy_replacement_authorization_id
        if authorization_id is None:
            raise ProductionExecutionError("legacy replacement capability is incomplete")

        def update(payload: dict[str, object]) -> dict[str, object]:
            row = self._find(payload, context.attempt_id)
            if (
                row.get("state") != "PRE_GENERATE_RESERVED"
                or row.get("generate_click_count") != 0
                or row.get("legacy_replacement_authorization_id") != authorization_id
                or row.get("production_job_id") != context.production_job_id
                or row.get("scene_job_id") != context.scene_job_id
            ):
                raise ProductionExecutionError("replacement reservation is not pre-Generate")
            row["reserved_credits"] = "0"
            row["state"] = "FAILED_CONFIRMED"
            row["released_before_generate"] = True
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
            if row.get("environment") == "PROD":
                try:
                    maximum = Decimal(str(row.get("max_reservation_credits")))
                except InvalidOperation as exc:
                    raise ProductionExecutionError(
                        "validated canonical per-run quote cap is missing"
                    ) from exc
                if not maximum.is_finite() or maximum <= 0 or quote > maximum:
                    raise ProductionExecutionError(
                        "confirmation quote exceeds the validated per-run quote cap"
                    )
                if row.get("legacy_replacement_authorization_id") is None:
                    try:
                        authorized_ceiling = Decimal(str(row.get("authorized_run_ceiling")))
                        validated_quote = Decimal(str(row.get("validated_quote_credits")))
                    except InvalidOperation as exc:
                        raise ProductionExecutionError(
                            "validated quote or authorized per-run ceiling is missing"
                        ) from exc
                    if (
                        not authorized_ceiling.is_finite()
                        or not validated_quote.is_finite()
                        or quote > min(authorized_ceiling, validated_quote)
                    ):
                        raise ProductionExecutionError(
                            "confirmation quote exceeds the authorized per-run reservation"
                        )
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
            if row.get("state") == "UNKNOWN_SUBMISSION_STATE" and state == row.get("state"):
                return dict(row)
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
                if (
                    row.get("environment") == "PROD"
                    and row.get("legacy_replacement_authorization_id") is None
                ):
                    try:
                        authorized_ceiling = Decimal(str(row.get("authorized_run_ceiling")))
                        validated_quote = Decimal(str(row.get("validated_quote_credits")))
                    except InvalidOperation as exc:
                        raise ProductionExecutionError(
                            "validated quote or authorized per-run ceiling is missing"
                        ) from exc
                    if (
                        not authorized_ceiling.is_finite()
                        or not validated_quote.is_finite()
                        or validated_quote <= 0
                    ):
                        raise ProductionExecutionError(
                            "validated quote or authorized per-run ceiling is invalid"
                        )
                    observed_quote = Decimal(str(row.get("quoted_credits") or validated_quote))
                    row["reserved_credits"] = str(
                        min(authorized_ceiling, validated_quote, observed_quote)
                    )
                else:
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

    def release_proven_not_submitted(
        self,
        attempt_id: str,
        *,
        evidence_sha256: object,
        production_job_id: str,
        scene_job_id: str,
        provider_account_id: str,
        provider_project_id: str,
    ) -> None:
        """Release this attempt's reservation only after exact refusal evidence is classified."""
        if (
            not isinstance(evidence_sha256, str)
            or len(evidence_sha256) != 64
            or any(c not in "0123456789abcdef" for c in evidence_sha256)
        ):
            raise ProductionExecutionError("reconciliation evidence digest is invalid")
        from .phase_g import D2DailyCreditLedger
        from .phase_g_prod_reconciliation import (
            ProdAttemptIdentity,
            ProdReconciliationResult,
            classify_prod_reconciliation,
            prod_evidence_from_attempt,
        )

        matches = [
            row
            for row in D2DailyCreditLedger(self.root.parent).attempts()
            if row.get("d2_run_id") == attempt_id
        ]
        if len(matches) != 1:
            raise ProductionExecutionError("persisted D2 attempt is missing or ambiguous")
        attempt = matches[0]
        try:
            identity = ProdAttemptIdentity(
                production_job_id=production_job_id,
                scene_job_id=scene_job_id,
                d2_run_id=attempt_id,
                provider_account_id=provider_account_id,
                provider_project_id=provider_project_id,
                scene_id=str(attempt.get("scene_id", "")),
                scene_prompt_id=str(attempt.get("scene_prompt_id", "")),
                generation_profile_id=str(attempt.get("generation_profile_id", "")),
                prompt_sha256=str(attempt.get("prompt_sha256", "")),
            )
        except ValueError as exc:
            raise ProductionExecutionError("persisted PROD attempt identity is invalid") from exc
        evidence = prod_evidence_from_attempt(identity, attempt)
        history_value = attempt.get("prod_reconciliation_history", [])
        history_items = cast(list[object], history_value) if isinstance(history_value, list) else []
        has_authoritative_audit = any(
            isinstance(item, dict)
            and cast(dict[str, object], item).get("evidence_sha256") == evidence_sha256
            and cast(dict[str, object], item).get(
                "result", ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION.value
            )
            == ProdReconciliationResult.PROVEN_NOT_SUBMITTED.value
            and cast(dict[str, object], item).get("contract_version")
            == "D2_PROD_SAME_ATTEMPT_RECOVERY_V1"
            for item in history_items
        )
        if (
            evidence is None
            or evidence.digest() != evidence_sha256
            or classify_prod_reconciliation(identity, evidence, persisted_attempt=attempt)
            is not ProdReconciliationResult.PROVEN_NOT_SUBMITTED
            or not has_authoritative_audit
        ):
            raise ProductionExecutionError(
                "persisted PROD identity or authoritative refusal evidence "
                "does not authorize release"
            )

        def update(payload: dict[str, object]) -> dict[str, object]:
            row = self._find(payload, attempt_id)
            if (
                row.get("environment") != "PROD"
                or row.get("production_job_id") != production_job_id
                or row.get("scene_job_id") != scene_job_id
                or row.get("provider_account_id") != provider_account_id
                or row.get("provider_project_id") != provider_project_id
            ):
                raise ProductionExecutionError(
                    "credit reservation identity does not match PROD attempt"
                )
            if row.get("reconciliation_release_evidence_sha256") == evidence_sha256:
                return dict(row)
            if row.get("state") in _TERMINAL - {"WAF_REJECTION"}:
                raise ProductionExecutionError("terminal attempt cannot be released again")
            if row.get("state") == "WAF_REJECTION" and row.get(
                "reconciliation_release_evidence_sha256"
            ):
                raise ProductionExecutionError("WAF reservation was already reconciled")
            if row.get("generate_click_count") != 1 or row.get("state") not in {
                "SUBMISSION_UNRESOLVED",
                "QUOTED_RESERVED",
                "WAF_REJECTION",
            }:
                raise ProductionExecutionError("attempt is not eligible for reconciliation release")
            row["reserved_credits"] = "0"
            row["state"] = "WAF_REJECTION"
            row["billing_outcome"] = "UNKNOWN"
            row["reconciliation_release_evidence_sha256"] = evidence_sha256
            return dict(row)

        self._mutate(update)

    def release_legacy_proven_not_submitted(
        self,
        attempt_id: str,
        *,
        production_job_id: str,
        scene_job_id: str,
        provider_account_id: str,
        provider_project_id: str,
        classification_event_sha256: str,
        released_at: str,
        software_commit: str,
    ) -> dict[str, object]:
        """Release one legacy reservation only after a persisted, provenance-bound refusal proof."""
        from .phase_g import D2DailyCreditLedger
        from .phase_g_prod_legacy_reconciliation import (
            ProdLegacyEvidenceClass,
            find_legacy_prod_reconciliation_audit,
            legacy_reservation_release_audit_event,
            write_legacy_prod_reconciliation_audit,
        )
        from .phase_g_prod_reconciliation import (
            ProdAttemptIdentity,
            ProdReconciliationResult,
            classify_prod_reconciliation,
            prod_evidence_from_attempt,
        )

        try:
            classification_event = find_legacy_prod_reconciliation_audit(
                self.root.parent, classification_event_sha256
            )
        except ValueError as exc:
            raise ProductionExecutionError("legacy reconciliation audit is invalid") from exc
        if (
            classification_event is None
            or classification_event.get("contract_version") != "D2_PROD_LEGACY_ATTEMPT_RECOVERY_V1"
            or classification_event.get("result")
            != ProdReconciliationResult.PROVEN_NOT_SUBMITTED.value
            or classification_event.get("legacy_attempt_id") != attempt_id
            or classification_event.get("event_type", "CLASSIFICATION") != "CLASSIFICATION"
            or classification_event.get("mutation_performed") is not False
            or classification_event.get("reservation_action") != "UNCHANGED"
        ):
            raise ProductionExecutionError(
                "persisted legacy refusal classification is missing or invalid"
            )

        raw_fields = classification_event.get("evidence_provenance")
        if not isinstance(raw_fields, dict):
            raise ProductionExecutionError("legacy evidence provenance is missing")
        provenance = cast(dict[str, object], raw_fields)

        def evidence_value(name: str, expected_class: ProdLegacyEvidenceClass) -> object:
            raw = provenance.get(name)
            if not isinstance(raw, dict):
                raise ProductionExecutionError("legacy identity provenance is incomplete")
            field = cast(dict[str, object], raw)
            if field.get("provenance_class") != expected_class.value:
                raise ProductionExecutionError("legacy identity provenance class is invalid")
            value = field.get("value")
            if not field.get("sources"):
                raise ProductionExecutionError("legacy identity source reference is missing")
            return value

        derived = ProdLegacyEvidenceClass.DERIVED_CANONICAL_JOIN
        direct = ProdLegacyEvidenceClass.PERSISTED_DIRECT
        try:
            identity = ProdAttemptIdentity(
                production_job_id=str(evidence_value("production_job_id", derived)),
                scene_job_id=str(evidence_value("scene_job_id", derived)),
                d2_run_id=str(evidence_value("attempt_id", direct)),
                provider_account_id=str(evidence_value("provider_account_id", direct)),
                provider_project_id=str(evidence_value("flow_project_id", direct)),
                scene_id=str(evidence_value("scene_id", direct)),
                scene_prompt_id=str(evidence_value("prompt_id", derived)),
                generation_profile_id=str(evidence_value("generation_profile_id", direct)),
                prompt_sha256=str(evidence_value("prompt_sha256", direct)),
            )
        except ValueError as exc:
            raise ProductionExecutionError("legacy PROD identity is invalid") from exc
        environment = evidence_value("execution_environment", derived)
        action_at = evidence_value("generate_action_at", direct)
        if (
            identity.d2_run_id != attempt_id
            or identity.production_job_id != production_job_id
            or identity.scene_job_id != scene_job_id
            or identity.provider_account_id != provider_account_id
            or identity.provider_project_id != provider_project_id
            or environment != "PROD"
            or not isinstance(action_at, str)
        ):
            raise ProductionExecutionError("legacy refusal identity does not match reservation")

        attempts = [
            row
            for row in D2DailyCreditLedger(self.root.parent).attempts()
            if row.get("d2_run_id") == attempt_id
        ]
        if len(attempts) != 1:
            raise ProductionExecutionError("persisted D2 attempt is missing or ambiguous")
        persisted_attempt = dict(attempts[0])
        if any(
            persisted_attempt.get(field) != value
            for field, value in (
                ("scene_id", identity.scene_id),
                ("scene_prompt_id", identity.scene_prompt_id),
                ("generation_profile_id", identity.generation_profile_id),
                ("provider_account_id", identity.provider_account_id),
                ("provider_project_id", identity.provider_project_id),
                ("prompt_sha256", identity.prompt_sha256),
                ("generate_action_at", action_at),
            )
        ):
            raise ProductionExecutionError("persisted D2 attempt differs from legacy audit")
        normalized_attempt = {
            **persisted_attempt,
            "production_job_id": identity.production_job_id,
            "scene_job_id": identity.scene_job_id,
            "execution_environment": "PROD",
        }
        evidence = prod_evidence_from_attempt(identity, normalized_attempt)
        if (
            evidence is None
            or classify_prod_reconciliation(
                identity, evidence, persisted_attempt=normalized_attempt
            )
            is not ProdReconciliationResult.PROVEN_NOT_SUBMITTED
        ):
            raise ProductionExecutionError(
                "legacy refusal does not pass the unchanged modern proof rule"
            )

        def update(payload: dict[str, object]) -> dict[str, object]:
            row = self._find(payload, attempt_id)
            if (
                row.get("environment") != "PROD"
                or row.get("provider_account_id") != provider_account_id
                or row.get("provider_project_id") != provider_project_id
                or row.get("production_job_id") not in (None, "", production_job_id)
                or row.get("scene_job_id") not in (None, "", scene_job_id)
            ):
                raise ProductionExecutionError(
                    "credit reservation identity does not match legacy PROD attempt"
                )
            if row.get("reconciliation_release_evidence_sha256") == classification_event_sha256:
                return dict(row)
            try:
                reserved_credits = Decimal(str(row.get("reserved_credits")))
            except InvalidOperation as exc:
                raise ProductionExecutionError("legacy reservation amount is invalid") from exc
            if (
                row.get("state") == "WAF_REJECTION"
                or not reserved_credits.is_finite()
                or reserved_credits <= 0
            ):
                raise ProductionExecutionError(
                    "legacy reservation was already released differently"
                )
            if (
                row.get("state")
                not in {
                    "UNKNOWN_SUBMISSION_STATE",
                    "SUBMISSION_UNRESOLVED",
                    "QUOTED_RESERVED",
                }
                or type(row.get("generate_click_count")) is not int
                or row.get("generate_click_count") != 1
                or row.get("billing_outcome") not in (None, "", "UNKNOWN")
            ):
                raise ProductionExecutionError("legacy reservation is not eligible for release")
            row["reserved_credits"] = "0"
            row["state"] = "WAF_REJECTION"
            row["reconciliation_release_evidence_sha256"] = classification_event_sha256
            return dict(row)

        released = self._mutate(update)
        release_event = legacy_reservation_release_audit_event(
            classification_event,
            released_at=released_at,
            software_commit=software_commit,
        )
        try:
            write_legacy_prod_reconciliation_audit(self.root.parent, release_event)
        except ValueError as exc:
            raise ProductionExecutionError(
                "reservation was released but its legacy mutation audit needs retry"
            ) from exc
        return released

    def _find(self, payload: dict[str, object], attempt_id: str) -> dict[str, object]:
        for row in cast(list[dict[str, object]], payload["attempts"]):
            if row.get("attempt_id") == attempt_id:
                return row
        raise ProductionExecutionError("production attempt does not exist")

    def correct_legacy_unknown_reservation(
        self,
        *,
        attempt_id: str,
        production_job_id: str,
        scene_job_id: str,
        expected_row_sha256: str,
        expected_ledger_sha256: str,
        quote_input_digest: str,
        quote_source_version: str,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Apply the one audited Run 3 correction without resolving submission ambiguity."""
        target = {
            "attempt_id": attempt_id,
            "production_job_id": production_job_id,
            "scene_job_id": scene_job_id,
            "environment": "PROD",
            "provider_account_id": _PROD_PROVIDER_ACCOUNT_ID,
            "provider_project_id": _PROD_PROVIDER_PROJECT_ID,
            "reserved_credits": "50",
            "state": "UNKNOWN_SUBMISSION_STATE",
            "generate_click_count": 1,
            "max_reservation_credits": "10",
            "reservation_quote_input_digest": quote_input_digest,
            "reservation_quote_source_version": quote_source_version,
        }
        if (
            attempt_id != _RUN3_CORRECTION_ATTEMPT_ID
            or production_job_id != _RUN3_CORRECTION_PRODUCTION_JOB_ID
            or scene_job_id != _RUN3_CORRECTION_SCENE_JOB_ID
            or expected_row_sha256 != _RUN3_CORRECTION_BEFORE_ROW_SHA256
            or expected_ledger_sha256 != _RUN3_CORRECTION_BEFORE_LEDGER_SHA256
            or quote_input_digest != _RUN3_CORRECTION_QUOTE_INPUT_DIGEST
            or quote_source_version != _RUN3_CORRECTION_QUOTE_SOURCE_VERSION
        ):
            raise ProductionExecutionError("Run 3 correction authorization is invalid")
        moment = now or datetime.now(UTC)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ProductionExecutionError("credit correction timestamp must be timezone-aware")
        audit_id = "RCA_RUN3_" + expected_row_sha256[:16].upper()

        def digest(value: object) -> str:
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
            return hashlib.sha256(encoded).hexdigest()

        def ledger_digest(value: object) -> str:
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
            return hashlib.sha256(encoded + b"\n").hexdigest()

        def update(payload: dict[str, object]) -> dict[str, object]:
            records = cast(list[dict[str, object]], payload["attempts"])
            matches = [row for row in records if row.get("attempt_id") == attempt_id]
            if len(matches) != 1:
                raise ProductionExecutionError("Run 3 reservation identity is not unique")
            row = matches[0]
            history = row.get("reservation_correction_history", [])
            if not isinstance(history, list):
                raise ProductionExecutionError("reservation correction audit is malformed")
            prior: list[dict[str, object]] = []
            for event_value in cast(list[object], history):
                if not isinstance(event_value, dict):
                    continue
                event = cast(dict[str, object], event_value)
                if event.get("audit_record_id") == audit_id:
                    prior.append(event)
            if prior:
                replay_event = {
                    "audit_record_id": audit_id,
                    "reason_code": "LEGACY_UNKNOWN_STATE_DAILY_LIMIT_FALLBACK_CORRECTION",
                    "attempt_id": attempt_id,
                    "production_job_id": production_job_id,
                    "scene_job_id": scene_job_id,
                    "before_amount": "50",
                    "after_amount": "10",
                    "before_state": "UNKNOWN_SUBMISSION_STATE",
                    "after_state": "UNKNOWN_SUBMISSION_STATE",
                    "provider_billing_outcome": "UNKNOWN",
                    "credit_ledger_billing_outcome_before": row.get("billing_outcome"),
                    "generate_click_count": 1,
                    "quote_input_digest": quote_input_digest,
                    "quote_source_version": quote_source_version,
                    "before_row_sha256": expected_row_sha256,
                    "before_ledger_sha256": expected_ledger_sha256,
                }
                if (
                    len(prior) != 1
                    or any(prior[0].get(key) != value for key, value in replay_event.items())
                    or row.get("reserved_credits") != "10"
                    or row.get("state") != "UNKNOWN_SUBMISSION_STATE"
                    or row.get("billing_outcome") not in (None, "", "UNKNOWN")
                ):
                    raise ProductionExecutionError("Run 3 correction replay differs")
                return dict(row)
            if (
                ledger_digest(payload) != expected_ledger_sha256
                or digest(row) != expected_row_sha256
            ):
                raise ProductionExecutionError("Run 3 reservation changed since authorization")
            if any(row.get(key) != value for key, value in target.items()):
                raise ProductionExecutionError("Run 3 reservation lineage or quote differs")
            if row.get("billing_outcome") not in (None, "", "UNKNOWN"):
                raise ProductionExecutionError("Run 3 billing outcome is not unresolved")
            if any(
                key.startswith("forensic_closure") and value not in (None, "", {})
                for key, value in row.items()
            ):
                raise ProductionExecutionError("Run 3 reservation has a forensic closure")
            event: dict[str, object] = {
                "audit_record_id": audit_id,
                "timestamp": format_utc_timestamp(moment),
                "reason_code": "LEGACY_UNKNOWN_STATE_DAILY_LIMIT_FALLBACK_CORRECTION",
                "attempt_id": attempt_id,
                "production_job_id": production_job_id,
                "scene_job_id": scene_job_id,
                "before_amount": "50",
                "after_amount": "10",
                "before_state": "UNKNOWN_SUBMISSION_STATE",
                "after_state": "UNKNOWN_SUBMISSION_STATE",
                "provider_billing_outcome": "UNKNOWN",
                "credit_ledger_billing_outcome_before": row.get("billing_outcome"),
                "generate_click_count": 1,
                "quote_input_digest": quote_input_digest,
                "quote_source_version": quote_source_version,
                "before_row_sha256": expected_row_sha256,
                "before_ledger_sha256": expected_ledger_sha256,
            }
            row["reserved_credits"] = "10"
            row["reservation_correction_history"] = [*history, event]
            return dict(row)

        return self._mutate(update)

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

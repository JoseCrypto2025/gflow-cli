"""Offline, identity-bound PROD same-attempt reconciliation decisions."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import cast


class ProdReconciliationResult(StrEnum):
    SUBMISSION_PROVEN_PENDING = "SUBMISSION_PROVEN_PENDING"
    SUBMISSION_PROVEN_EXECUTION_FOUND = "SUBMISSION_PROVEN_EXECUTION_FOUND"
    LATE_CONFIRMATION_FOUND = "LATE_CONFIRMATION_FOUND"
    OUTPUT_ALREADY_EXISTS = "OUTPUT_ALREADY_EXISTS"
    PROVEN_NOT_SUBMITTED = "PROVEN_NOT_SUBMITTED"
    AMBIGUOUS_AFTER_RECONCILIATION = "AMBIGUOUS_AFTER_RECONCILIATION"


@dataclass(frozen=True, slots=True)
class ProdAttemptIdentity:
    production_job_id: str
    scene_job_id: str
    d2_run_id: str
    provider_account_id: str
    provider_project_id: str
    scene_id: str
    scene_prompt_id: str
    generation_profile_id: str
    prompt_sha256: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"PRD_[0-9]{6}", self.production_job_id):
            raise ValueError("Production Job ID is invalid")
        if not re.fullmatch(r"SJB_[0-9]{6}", self.scene_job_id):
            raise ValueError("Scene Job ID is invalid")
        if not re.fullmatch(r"D2_[A-Za-z0-9_-]{8,64}", self.d2_run_id):
            raise ValueError("D2 attempt ID is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.prompt_sha256):
            raise ValueError("prompt SHA-256 is invalid")
        if not all(
            value.strip()
            for value in (
                self.provider_account_id,
                self.provider_project_id,
                self.scene_id,
                self.scene_prompt_id,
                self.generation_profile_id,
            )
        ):
            raise ValueError("production attempt identity is incomplete")


@dataclass(frozen=True, slots=True)
class ProdReconciliationEvidence:
    """Sanitized, already-observed evidence; this value makes no provider calls."""

    identity: ProdAttemptIdentity
    observed_at: str
    submission_state: str | None = None
    provider_execution_id: str | None = None
    provider_media_ids: tuple[str, ...] = ()
    confirmation_id: str | None = None
    provider_submit_request_count: int = 0
    provider_submit_dispatch_window_completed: bool = False
    provider_submit_project_id: str | None = None
    provider_submit_response_observed: bool = False
    provider_submit_response_status: int | None = None
    provider_submit_generation_record_observed: bool = False
    provider_submit_refusal_code: str | None = None
    provider_submit_request_failed: bool | None = None
    baseline_execution_ids: tuple[str, ...] = ()
    baseline_media_ids: tuple[str, ...] = ()
    baseline_confirmation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.observed_at.strip():
            raise ValueError("evidence observation timestamp is required")
        if (
            type(self.provider_submit_request_count) is not int
            or self.provider_submit_request_count < 0
        ):
            raise ValueError("submit request count cannot be negative")
        if self.provider_submit_response_status is not None and (
            type(self.provider_submit_response_status) is not int
            or not 100 <= self.provider_submit_response_status <= 599
        ):
            raise ValueError("provider response status is invalid")
        if any(
            not item
            for item in (
                *self.provider_media_ids,
                *self.baseline_execution_ids,
                *self.baseline_media_ids,
                *self.baseline_confirmation_ids,
            )
        ):
            raise ValueError("evidence identities cannot be empty")
        for flag in (
            self.provider_submit_dispatch_window_completed,
            self.provider_submit_response_observed,
            self.provider_submit_generation_record_observed,
        ):
            if type(flag) is not bool:
                raise ValueError("provider evidence flag is invalid")
        if (
            self.provider_submit_request_failed is not None
            and type(self.provider_submit_request_failed) is not bool
        ):
            raise ValueError("provider request failure flag is invalid")
        refusal_code = self.provider_submit_refusal_code
        if refusal_code is not None and (not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", refusal_code)):
            raise ValueError("provider refusal code is invalid")
        if refusal_code is not None and (
            not self.provider_submit_response_observed
            or self.provider_submit_response_status is None
        ):
            raise ValueError("provider refusal code requires an observed response")

    def digest(self) -> str:
        value = asdict(self)
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return sha256(encoded.encode("utf-8")).hexdigest()


def classify_prod_reconciliation(
    expected: ProdAttemptIdentity,
    evidence: ProdReconciliationEvidence | None,
    *,
    persisted_attempt: Mapping[str, object],
) -> ProdReconciliationResult:
    """Classify one existing PROD attempt; absence and UI clicks remain ambiguous."""
    if not _persisted_identity_matches(expected, persisted_attempt):
        return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
    if type(persisted_attempt.get("generate_action_count")) is not int or (
        persisted_attempt.get("generate_action_count") != 1
    ):
        return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
    if persisted_attempt.get("baseline_captured") is not True:
        return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
    if evidence is not None and evidence.identity != expected:
        return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION

    if evidence is None:
        evidence = _evidence_from_attempt(expected, persisted_attempt)
        if evidence is None:
            return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
    if not _evidence_is_fresh(evidence, persisted_attempt):
        return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION

    if evidence.provider_submit_project_id not in {None, expected.provider_project_id}:
        return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
    if evidence.provider_submit_request_count > 1 or len(evidence.provider_media_ids) > 1:
        return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION

    if _proves_not_submitted(expected, evidence, persisted_attempt):
        return ProdReconciliationResult.PROVEN_NOT_SUBMITTED
    if evidence.provider_submit_refusal_code is not None:
        return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION

    execution_id = evidence.provider_execution_id
    media_ids = evidence.provider_media_ids
    attempt_baselines = _persisted_baselines(persisted_attempt)
    if attempt_baselines is None:
        return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
    baseline_execution_ids, baseline_media_ids, baseline_confirmation_ids = attempt_baselines
    if execution_id and (
        execution_id in baseline_execution_ids or execution_id in evidence.baseline_execution_ids
    ):
        return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
    if any(
        media_id in baseline_media_ids or media_id in evidence.baseline_media_ids
        for media_id in media_ids
    ):
        return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
    if evidence.confirmation_id and (
        evidence.confirmation_id in baseline_confirmation_ids
        or evidence.confirmation_id in evidence.baseline_confirmation_ids
    ):
        return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION

    submission_state = evidence.submission_state
    if len(media_ids) == 1 and submission_state in {
        "GENERATION_SUCCEEDED",
        "D2_SUCCEEDED",
        "OUTPUT_ALREADY_EXISTS",
    }:
        return ProdReconciliationResult.OUTPUT_ALREADY_EXISTS
    if execution_id and submission_state in {
        "SUBMISSION_ACKNOWLEDGED",
        "GENERATION_PENDING",
        "GENERATION_SUCCEEDED",
        "D2_SUCCEEDED",
    }:
        if submission_state == "GENERATION_PENDING":
            return ProdReconciliationResult.SUBMISSION_PROVEN_PENDING
        return ProdReconciliationResult.SUBMISSION_PROVEN_EXECUTION_FOUND
    if (
        evidence.provider_submit_response_observed
        and evidence.provider_submit_generation_record_observed
        and evidence.provider_submit_response_status is not None
        and 200 <= evidence.provider_submit_response_status < 300
        and evidence.provider_submit_request_count == 1
        and evidence.provider_submit_dispatch_window_completed
        and evidence.provider_submit_project_id == expected.provider_project_id
    ):
        return ProdReconciliationResult.SUBMISSION_PROVEN_PENDING
    if evidence.confirmation_id:
        return ProdReconciliationResult.LATE_CONFIRMATION_FOUND
    return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION


def evidence_audit_record(
    result: ProdReconciliationResult,
    evidence: ProdReconciliationEvidence,
) -> dict[str, object]:
    """Return a JSON-safe, sanitized audit record with a stable evidence digest."""
    return {
        "result": result.value,
        "evidence_sha256": evidence.digest(),
        "observed_at": evidence.observed_at,
        "identity": asdict(evidence.identity),
        "submission_state": evidence.submission_state,
        "provider_execution_id": evidence.provider_execution_id,
        "provider_media_ids": list(evidence.provider_media_ids),
        "confirmation_id": evidence.confirmation_id,
        "provider_submit_request_count": evidence.provider_submit_request_count,
        "provider_submit_dispatch_window_completed": (
            evidence.provider_submit_dispatch_window_completed
        ),
        "provider_submit_project_id": evidence.provider_submit_project_id,
        "provider_submit_response_observed": evidence.provider_submit_response_observed,
        "provider_submit_response_status": evidence.provider_submit_response_status,
        "provider_submit_generation_record_observed": (
            evidence.provider_submit_generation_record_observed
        ),
        "provider_submit_refusal_code": evidence.provider_submit_refusal_code,
        "provider_submit_request_failed": evidence.provider_submit_request_failed,
        "baseline_execution_ids": list(evidence.baseline_execution_ids),
        "baseline_media_ids": list(evidence.baseline_media_ids),
        "baseline_confirmation_ids": list(evidence.baseline_confirmation_ids),
    }


def prod_evidence_from_attempt(
    identity: ProdAttemptIdentity, attempt: Mapping[str, object]
) -> ProdReconciliationEvidence | None:
    """Build evidence only from fields already durably observed in one D2 row."""
    return _evidence_from_attempt(identity, attempt)


def _persisted_identity_matches(
    expected: ProdAttemptIdentity,
    attempt: Mapping[str, object],
) -> bool:
    return all(
        attempt.get(key) == value
        for key, value in (
            ("d2_run_id", expected.d2_run_id),
            ("production_job_id", expected.production_job_id),
            ("scene_job_id", expected.scene_job_id),
            ("provider_account_id", expected.provider_account_id),
            ("provider_project_id", expected.provider_project_id),
            ("scene_id", expected.scene_id),
            ("scene_prompt_id", expected.scene_prompt_id),
            ("generation_profile_id", expected.generation_profile_id),
            ("prompt_sha256", expected.prompt_sha256),
        )
    )


def _evidence_from_attempt(
    identity: ProdAttemptIdentity,
    attempt: Mapping[str, object],
) -> ProdReconciliationEvidence | None:
    state = attempt.get("submission_state")
    execution = attempt.get("provider_execution_id")
    media_value = attempt.get("provider_media_ids", [])
    if not isinstance(media_value, list):
        return None
    media_items = cast(list[object], media_value)
    if any(not isinstance(item, str) for item in media_items):
        return None
    media_ids = tuple(cast(list[str], media_items))
    for value in (execution, attempt.get("provider_media_id")):
        if value is not None and not isinstance(value, str):
            return None
    if not isinstance(state, str):
        return None
    observed_at = attempt.get("updated_at")
    if not isinstance(observed_at, str) or not observed_at:
        return None
    return ProdReconciliationEvidence(
        identity=identity,
        observed_at=observed_at,
        submission_state=state,
        provider_execution_id=cast(str | None, execution),
        provider_media_ids=media_ids
        or ((cast(str, attempt["provider_media_id"]),) if attempt.get("provider_media_id") else ()),
        confirmation_id=cast(str | None, attempt.get("observed_confirmation_id")),
        provider_submit_request_count=_integer(attempt.get("provider_submit_request_count")),
        provider_submit_dispatch_window_completed=(
            attempt.get("provider_submit_dispatch_window_completed") is True
        ),
        provider_submit_project_id=cast(str | None, attempt.get("provider_submit_project_id")),
        provider_submit_response_observed=(
            attempt.get("provider_submit_response_observed") is True
        ),
        provider_submit_response_status=_optional_integer(
            attempt.get("provider_submit_response_status")
        ),
        provider_submit_generation_record_observed=(
            attempt.get("provider_submit_generation_record_observed") is True
        ),
        provider_submit_refusal_code=cast(str | None, attempt.get("provider_submit_refusal_code")),
        provider_submit_request_failed=cast(
            bool | None, attempt.get("provider_submit_request_failed")
        ),
        baseline_execution_ids=_string_tuple(attempt.get("baseline_execution_ids")) or (),
        baseline_media_ids=_string_tuple(attempt.get("baseline_media_ids")) or (),
        baseline_confirmation_ids=_string_tuple(attempt.get("baseline_confirmation_ids")) or (),
    )


def _proves_not_submitted(
    expected: ProdAttemptIdentity,
    evidence: ProdReconciliationEvidence,
    attempt: Mapping[str, object],
) -> bool:
    return (
        attempt.get("provider_submit_rpc_observed") is True
        and attempt.get("provider_submit_rpc_id") == "YhhmEf"
        and attempt.get("provider_submit_http_method") == "POST"
        and type(attempt.get("provider_submit_request_count")) is int
        and attempt.get("provider_submit_request_count") == 1
        and _request_after_generate_action(attempt)
        and attempt.get("provider_submit_dispatch_window_completed") is True
        and attempt.get("provider_submit_project_id") == expected.provider_project_id
        and attempt.get("provider_submit_response_observed") is True
        and attempt.get("provider_submit_response_status") == 403
        and attempt.get("provider_submit_generation_record_observed") is False
        and attempt.get("provider_submit_refusal_detected") is True
        and attempt.get("provider_submit_refusal_code") == "PUBLIC_ERROR_UNUSUAL_ACTIVITY"
        and attempt.get("provider_submit_request_failed") is False
        and attempt.get("provider_execution_id") is None
        and attempt.get("provider_media_id") is None
        and attempt.get("provider_media_ids") == []
        and attempt.get("confirmation_id") is None
        and attempt.get("observed_confirmation_id") is None
        and evidence.provider_submit_request_count == 1
        and evidence.provider_submit_dispatch_window_completed
        and evidence.provider_submit_project_id == expected.provider_project_id
        and evidence.provider_submit_response_observed
        and evidence.provider_submit_response_status == 403
        and evidence.provider_submit_refusal_code == "PUBLIC_ERROR_UNUSUAL_ACTIVITY"
        and evidence.provider_submit_generation_record_observed is False
        and evidence.provider_submit_request_failed is False
        and evidence.provider_execution_id is None
        and not evidence.provider_media_ids
        and evidence.confirmation_id is None
        and evidence.submission_state
        not in {
            "SUBMISSION_ACKNOWLEDGED",
            "GENERATION_PENDING",
            "GENERATION_SUCCEEDED",
            "D2_SUCCEEDED",
        }
    )


def _evidence_is_fresh(evidence: ProdReconciliationEvidence, attempt: Mapping[str, object]) -> bool:
    action_at = attempt.get("generate_action_at")
    if not isinstance(action_at, str):
        return False
    try:
        action_time = datetime.fromisoformat(action_at.replace("Z", "+00:00"))
        observed_time = datetime.fromisoformat(evidence.observed_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (
        action_time.tzinfo is not None
        and observed_time.tzinfo is not None
        and observed_time >= action_time
    )


def _request_after_generate_action(attempt: Mapping[str, object]) -> bool:
    action_at = attempt.get("generate_action_at")
    request_at = attempt.get("provider_submit_observed_at")
    if not isinstance(action_at, str) or not isinstance(request_at, str):
        return False
    try:
        action_time = datetime.fromisoformat(action_at.replace("Z", "+00:00"))
        request_time = datetime.fromisoformat(request_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (
        action_time.tzinfo is not None
        and request_time.tzinfo is not None
        and request_time >= action_time
    )


def _integer(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def _optional_integer(value: object) -> int | None:
    return value if type(value) is int else None


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, tuple):
        items = cast(tuple[object, ...], value)
        if all(isinstance(item, str) for item in items):
            return tuple(cast(tuple[str, ...], items))
    if isinstance(value, list):
        items = cast(list[object], value)
        if all(isinstance(item, str) for item in items):
            return tuple(cast(list[str], items))
    return ()


def _persisted_baselines(
    attempt: Mapping[str, object],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None:
    values = tuple(
        _string_tuple(attempt.get(key))
        for key in ("baseline_execution_ids", "baseline_media_ids", "baseline_confirmation_ids")
    )
    if any(
        not isinstance(attempt.get(key), list)
        for key in (
            "baseline_execution_ids",
            "baseline_media_ids",
            "baseline_confirmation_ids",
        )
    ):
        return None
    for key, parsed in zip(
        ("baseline_execution_ids", "baseline_media_ids", "baseline_confirmation_ids"),
        values,
        strict=True,
    ):
        raw = attempt.get(key)
        if not isinstance(raw, list):
            return None
        raw_items = cast(list[object], raw)
        if len(parsed) != len(raw_items):
            return None
    return cast(tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]], values)

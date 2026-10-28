"""Fail-closed quote and one-time authorization for ambiguous legacy PROD replacements."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

from .ambiguous_legacy_recovery_policy import (
    AUDIT_NAME as POLICY_AUDIT_NAME,
)
from .ambiguous_legacy_recovery_policy import (
    ambiguous_legacy_policy_id,
    existing_policy_event,
)
from .google_sheets import (
    APPROVED_SPREADSHEET_ID,
    SOURCE_LIVE_GOOGLE_SHEETS,
    GoogleSheetsSnapshot,
)
from .phase_g import PhaseGError
from .phase_g_prod_legacy_reconciliation import find_legacy_prod_reconciliation_audit

if TYPE_CHECKING:
    from .phase_g import D2DailyCreditLedger, PhaseGExecutionRequest

_AUTHORIZATION_VERSION = "D2_PROD_LEGACY_REPLACEMENT_AUTHORIZATION_V2"
_AUTHORIZATION_ID_RE = re.compile(r"^LRA_[0-9A-F]{24}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ReplacementQuoteStatus(StrEnum):
    VALID = "VALID"
    QUOTE_UNAVAILABLE = "QUOTE_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ReplacementGenerationConfig:
    provider_id: str
    provider_account_id: str
    model_id: str
    generation_profile_id: str
    generation_mode: str
    pricing_variant: str
    account_pricing_class: str
    output_count: int
    duration_sec: float | None = None
    aspect_ratio: str | None = None
    resolution: str | None = None
    audio_mode: str | None = None
    quality_tier: str | None = None

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.provider_id,
                self.provider_account_id,
                self.model_id,
                self.generation_profile_id,
                self.generation_mode,
                self.pricing_variant,
                self.account_pricing_class,
            )
        ):
            raise ValueError("replacement generation configuration is incomplete")
        if (
            (
                self.duration_sec is not None
                and (
                    type(self.duration_sec) not in (int, float)
                    or not math.isfinite(self.duration_sec)
                    or self.duration_sec <= 0
                )
            )
            or type(self.output_count) is not int
            or self.output_count < 1
        ):
            raise ValueError("replacement generation dimensions are invalid")


@dataclass(frozen=True, slots=True)
class ReplacementQuote:
    status: ReplacementQuoteStatus
    amount: Decimal | None
    unit: str | None
    source: str | None
    source_version: str | None
    input_digest: str
    quoted_at: str
    valid: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ReplacementDecision:
    status: str
    quote: ReplacementQuote
    existing_unresolved_exposure: Decimal | None
    maximum_combined_exposure: Decimal | None
    input_digest: str
    authorization_created: bool = False


@dataclass(frozen=True, slots=True)
class LegacyReplacementAuthorizationRequest:
    parent_attempt_id: str
    production_job_id: str
    scene_job_id: str
    replacement_production_job_id: str
    replacement_scene_job_id: str
    policy_id: str
    reconciliation_audit_id: str
    decision: ReplacementDecision
    legacy_exposure: Decimal
    created_at: str
    created_by: str
    software_commit: str


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def legacy_replacement_request_idempotency_key(
    *,
    parent_attempt_id: str,
    policy_id: str,
    reconciliation_audit_id: str,
    decision_input_digest: str,
) -> str:
    """Return the stable V2 Production Job key bound to one approved decision."""
    authorization_id = (
        "LRA_"
        + hashlib.sha256(
            (
                f"{parent_attempt_id}:{policy_id}:{reconciliation_audit_id}:{decision_input_digest}"
            ).encode()
        )
        .hexdigest()[:24]
        .upper()
    )
    if not _AUTHORIZATION_ID_RE.fullmatch(authorization_id) or not _SHA256_RE.fullmatch(
        decision_input_digest
    ):
        raise ValueError("legacy replacement decision identity is invalid")
    return f"LEGACY_REPLACEMENT_V2_{authorization_id}"


def _decimal_value(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _profile_matches(profile: Mapping[str, object], config: ReplacementGenerationConfig) -> bool:
    return (
        profile.get("Generation_Profile_ID") == config.generation_profile_id
        and profile.get("Provider_Account_ID") == config.provider_account_id
        and profile.get("Model_ID") == config.model_id
        and profile.get("Generation_Mode") == config.generation_mode
        and profile.get("Environment") == "PROD"
        and profile.get("Status") == "ACTIVE"
    )


def _account_matches(account: Mapping[str, object], config: ReplacementGenerationConfig) -> bool:
    return (
        account.get("Provider_Account_ID") == config.provider_account_id
        and account.get("Provider_ID") == config.provider_id
        and account.get("Environment") == "PROD"
        and account.get("Status") == "ACTIVE"
    )


def _model_matches(model: Mapping[str, object], config: ReplacementGenerationConfig) -> bool:
    name = model.get("Model_Name")
    normalized_name = re.sub(r"[^a-z0-9]+", "", name.casefold()) if isinstance(name, str) else ""
    return (
        model.get("Model_ID") == config.model_id
        and model.get("Provider_ID") == config.provider_id
        and model.get("Status") == "ACTIVE"
        and normalized_name == "veo31lite"
    )


def _row_matches(row: Mapping[str, object], config: ReplacementGenerationConfig) -> bool:
    return (
        row.get("Provider_ID") == config.provider_id
        and row.get("Provider_Account_ID") == config.provider_account_id
        and row.get("Model_ID") == config.model_id
        and row.get("Billing_Plan_Name") == config.account_pricing_class
        and row.get("Generation_Mode") == config.generation_mode
        and row.get("Cost_Unit") == "CREDIT"
        and row.get("Currency") in (None, "")
        and row.get("Status") == "ACTIVE"
    )


def quote_generation_reservation(
    config: ReplacementGenerationConfig,
    *,
    snapshot: GoogleSheetsSnapshot,
    quoted_at: datetime,
) -> ReplacementQuote:
    """Resolve a canonical exact-scope cost row without attempt, reserve, or provider calls."""
    if quoted_at.tzinfo is None or quoted_at.utcoffset() is None:
        raise ValueError("quote time must be timezone-aware")
    input_digest = _digest(
        {
            "config": asdict(config),
            "spreadsheet_id": snapshot.spreadsheet_id,
            "snapshot_read_at": snapshot.read_at,
        }
    )
    when = quoted_at.astimezone(UTC)
    unavailable = ReplacementQuote(
        ReplacementQuoteStatus.QUOTE_UNAVAILABLE,
        None,
        None,
        None,
        None,
        input_digest,
        when.isoformat(),
        False,
        "NO_UNIQUE_EFFECTIVE_VERIFIED_DIRECT_CREDIT_ROW",
    )
    snapshot_read_at = _timestamp(snapshot.read_at)
    if (
        config.pricing_variant != "STANDARD_LITE"
        or config.account_pricing_class != "NON_ULTRA"
        or snapshot.spreadsheet_id != APPROVED_SPREADSHEET_ID
        or snapshot.source != SOURCE_LIVE_GOOGLE_SHEETS
        or snapshot_read_at is None
        or snapshot_read_at != when
    ):
        return unavailable
    accounts = snapshot.tables.get("PROVIDER_ACCOUNT_MASTER", [])
    account_matches = [row for row in accounts if _account_matches(row, config)]
    account_identity_rows = [
        row for row in accounts if row.get("Provider_Account_ID") == config.provider_account_id
    ]
    models = snapshot.tables.get("MODEL_MASTER", [])
    model_matches = [row for row in models if _model_matches(row, config)]
    model_identity_rows = [row for row in models if row.get("Model_ID") == config.model_id]
    profiles = snapshot.tables.get("GENERATION_PROFILE_MASTER", [])
    profile_matches = [row for row in profiles if _profile_matches(row, config)]
    profile_identity_rows = [
        row for row in profiles if row.get("Generation_Profile_ID") == config.generation_profile_id
    ]
    if (
        len(account_matches) != 1
        or len(account_identity_rows) != 1
        or len(model_matches) != 1
        or len(model_identity_rows) != 1
        or len(profile_matches) != 1
        or len(profile_identity_rows) != 1
    ):
        return unavailable
    cost_rows = snapshot.tables.get("PROVIDER_COST_MASTER", [])
    matching: list[Mapping[str, object]] = []
    for row in cost_rows:
        if not _row_matches(row, config):
            continue
        source = row.get("Source")
        parsed_source = urlparse(source) if isinstance(source, str) else None
        verified_at = _timestamp(row.get("Verified_At"))
        start = _timestamp(row.get("Effective_From"))
        end_value = row.get("Effective_To")
        end = _timestamp(end_value) if end_value not in (None, "") else None
        if (
            parsed_source is None
            or parsed_source.scheme != "https"
            or parsed_source.hostname != "support.google.com"
            or parsed_source.path != "/flow/answer/16526234"
            or verified_at is None
            or verified_at > when
            or start is None
            or start > when
            or (end_value not in (None, "") and end is None)
            or (end is not None and when >= end)
        ):
            continue
        matching.append(row)
    if len(matching) != 1:
        return unavailable
    row = matching[0]
    try:
        per_generation_rate = Decimal(str(row.get("Cost_Rate")))
    except (InvalidOperation, ValueError):
        return unavailable
    if not per_generation_rate.is_finite() or per_generation_rate <= 0:
        return unavailable
    amount = per_generation_rate * config.output_count
    cost_id = row.get("Provider_Cost_ID")
    if not isinstance(cost_id, str) or not cost_id.strip():
        return unavailable
    input_digest = _digest(
        {
            "config": asdict(config),
            "spreadsheet_id": snapshot.spreadsheet_id,
            "account": dict(account_matches[0]),
            "model": dict(model_matches[0]),
            "profile": dict(profile_matches[0]),
            "cost_row": dict(row),
        }
    )
    return ReplacementQuote(
        ReplacementQuoteStatus.VALID,
        amount,
        "CREDIT",
        cast(str, row["Source"]),
        f"{cost_id}:{row['Verified_At']}:"
        f"{_digest({'spreadsheet_id': snapshot.spreadsheet_id, 'row': dict(row)})}",
        input_digest,
        when.isoformat(),
        True,
        "SOURCE_VERIFIED_EFFECTIVE_DIRECT_CREDIT_RATE",
    )


def evaluate_replacement_decision(
    *,
    gate_a_pass: bool,
    gate_b_pass: bool,
    quote: ReplacementQuote,
    existing_unresolved_exposure: Decimal | None,
    replacement_authorization_ready: bool,
    parent_linkage_ready: bool,
    parent_quarantined: bool,
    zero_existing_replacements: bool,
    zero_conflicting_authorizations: bool,
    evidence_capture_ready: bool,
) -> ReplacementDecision:
    """Evaluate readiness without creating an authorization or enabling Generate."""
    exposure = existing_unresolved_exposure
    combined = (
        exposure + quote.amount
        if exposure is not None
        and exposure.is_finite()
        and exposure >= 0
        and quote.valid
        and quote.amount is not None
        else None
    )
    inputs = {
        "gate_a_pass": gate_a_pass,
        "gate_b_pass": gate_b_pass,
        "quote_input_digest": quote.input_digest,
        "quote_source_version": quote.source_version,
        "existing_unresolved_exposure": str(exposure) if exposure is not None else None,
        "maximum_combined_exposure": str(combined) if combined is not None else None,
        "replacement_authorization_ready": replacement_authorization_ready,
        "parent_linkage_ready": parent_linkage_ready,
        "parent_quarantined": parent_quarantined,
        "zero_existing_replacements": zero_existing_replacements,
        "zero_conflicting_authorizations": zero_conflicting_authorizations,
        "evidence_capture_ready": evidence_capture_ready,
    }
    approved = all(
        (
            gate_a_pass,
            gate_b_pass,
            quote.valid,
            quote.status is ReplacementQuoteStatus.VALID,
            quote.unit == "CREDIT",
            combined is not None,
            replacement_authorization_ready,
            parent_linkage_ready,
            parent_quarantined,
            zero_existing_replacements,
            zero_conflicting_authorizations,
            evidence_capture_ready,
        )
    )
    return ReplacementDecision(
        "APPROVE" if approved else "HOLD",
        quote,
        exposure,
        combined,
        _digest(inputs),
        False,
    )


def _validate_authorization_audits(
    ledger: D2DailyCreditLedger,
    *,
    parent_attempt_id: str,
    production_job_id: str,
    scene_job_id: str,
    policy_id: str,
    reconciliation_audit_id: str,
    legacy_exposure: Decimal,
) -> None:
    production_root = ledger.root.parent
    runtime_root = production_root.parent
    policy_path = ledger.root / POLICY_AUDIT_NAME
    try:
        policy = existing_policy_event(
            policy_path, event_type="POLICY_APPLIED", policy_id=policy_id
        )
        reconciliation = find_legacy_prod_reconciliation_audit(
            runtime_root, reconciliation_audit_id
        )
    except (OSError, ValueError) as exc:
        raise PhaseGError(
            "PHASE_G_LEGACY_REPLACEMENT_AUDIT_INVALID",
            "legacy replacement audit verification failed",
        ) from exc
    if policy is None or reconciliation is None:
        raise PhaseGError(
            "PHASE_G_LEGACY_REPLACEMENT_AUDIT_MISSING",
            "legacy replacement requires applied policy and reconciliation audits",
        )
    replacement_value = policy.get("replacement")
    exposure_value = policy.get("unresolved_credit_exposure")
    if not isinstance(replacement_value, dict) or not isinstance(exposure_value, dict):
        raise PhaseGError(
            "PHASE_G_LEGACY_REPLACEMENT_AUDIT_MISMATCH",
            "legacy replacement audits have invalid policy structures",
        )
    replacement = cast(dict[str, object], replacement_value)
    exposure = cast(dict[str, object], exposure_value)
    if (
        policy.get("attempt_id") != parent_attempt_id
        or policy.get("production_job_id") != production_job_id
        or policy.get("scene_job_id") != scene_job_id
        or policy.get("reconciliation_audit_sha256") != reconciliation_audit_id
        or policy.get("production_job_target") != "QUARANTINED"
        or policy.get("scene_job_target") != "QUARANTINED"
        or replacement.get("eligible_after_quarantine") is not True
        or replacement.get("authorized") is not False
        or exposure.get("held_reservation_state") != "UNKNOWN_SUBMISSION_STATE"
        or _decimal_value(exposure.get("held_reservation_credits")) != legacy_exposure
        or reconciliation.get("legacy_attempt_id") != parent_attempt_id
        or reconciliation.get("result") != "AMBIGUOUS_AFTER_RECONCILIATION"
        or reconciliation.get("reservation_action") != "UNCHANGED"
    ):
        raise PhaseGError(
            "PHASE_G_LEGACY_REPLACEMENT_AUDIT_MISMATCH",
            "legacy replacement audits do not bind the exact quarantined parent",
        )
    provenance_value = reconciliation.get("evidence_provenance")
    if not isinstance(provenance_value, dict):
        raise PhaseGError(
            "PHASE_G_LEGACY_REPLACEMENT_AUDIT_MISMATCH",
            "legacy replacement requires source-bound reconciliation identities",
        )
    provenance = cast(dict[str, object], provenance_value)
    for key, expected in (
        ("production_job_id", production_job_id),
        ("scene_job_id", scene_job_id),
        ("attempt_id", parent_attempt_id),
    ):
        identity_value = provenance.get(key)
        if not isinstance(identity_value, dict):
            raise PhaseGError(
                "PHASE_G_LEGACY_REPLACEMENT_AUDIT_MISMATCH",
                "legacy replacement reconciliation identity is mismatched",
            )
        identity = cast(dict[str, object], identity_value)
        if identity.get("value") != expected:
            raise PhaseGError(
                "PHASE_G_LEGACY_REPLACEMENT_AUDIT_MISMATCH",
                "legacy replacement reconciliation identity is mismatched",
            )


def create_legacy_replacement_authorization(
    ledger: D2DailyCreditLedger,
    request: LegacyReplacementAuthorizationRequest,
) -> dict[str, object]:
    """Persist one approval record; this function never creates a D2 attempt or Generate action."""
    decision = request.decision
    quote = decision.quote
    authorization_id = (
        "LRA_"
        + hashlib.sha256(
            (
                f"{request.parent_attempt_id}:{request.policy_id}:"
                f"{request.reconciliation_audit_id}:{decision.input_digest}"
            ).encode()
        )
        .hexdigest()[:24]
        .upper()
    )
    if (
        decision.status != "APPROVE"
        or decision.authorization_created
        or quote.status is not ReplacementQuoteStatus.VALID
        or not quote.valid
        or quote.amount is None
        or not quote.amount.is_finite()
        or quote.amount <= 0
        or quote.unit != "CREDIT"
        or not _SHA256_RE.fullmatch(decision.input_digest)
        or not _SHA256_RE.fullmatch(quote.input_digest)
        or not quote.source_version
        or not quote.source
        or urlparse(quote.source).scheme != "https"
        or not urlparse(quote.source).netloc
        or _timestamp(quote.quoted_at) is None
        or not _AUTHORIZATION_ID_RE.fullmatch(authorization_id)
    ):
        raise PhaseGError(
            "PHASE_G_LEGACY_REPLACEMENT_AUTH_INVALID",
            "legacy replacement authorization requires an approved valid quote",
        )
    if (
        not re.fullmatch(r"D2_[A-Za-z0-9_-]{8,64}", request.parent_attempt_id)
        or not re.fullmatch(r"PRD_[0-9]{6}", request.production_job_id)
        or not re.fullmatch(r"SJB_[0-9]{6}", request.scene_job_id)
        or not re.fullmatch(r"PRD_[0-9]{6}", request.replacement_production_job_id)
        or not re.fullmatch(r"SJB_[0-9]{6}", request.replacement_scene_job_id)
        or request.production_job_id == request.replacement_production_job_id
        or request.scene_job_id == request.replacement_scene_job_id
        or request.policy_id != ambiguous_legacy_policy_id(request.parent_attempt_id)
        or not _SHA256_RE.fullmatch(request.reconciliation_audit_id)
        or not _SHA256_RE.fullmatch(quote.input_digest)
        or not _COMMIT_RE.fullmatch(request.software_commit)
        or not request.created_by.strip()
        or _timestamp(request.created_at) is None
        or not request.decision.maximum_combined_exposure
        or request.decision.existing_unresolved_exposure != request.legacy_exposure
        or not request.legacy_exposure.is_finite()
        or request.legacy_exposure <= 0
        or request.decision.maximum_combined_exposure != request.legacy_exposure + quote.amount
    ):
        raise PhaseGError(
            "PHASE_G_LEGACY_REPLACEMENT_AUTH_INVALID",
            "legacy replacement authorization identity or exposure is invalid",
        )
    _validate_authorization_audits(
        ledger,
        parent_attempt_id=request.parent_attempt_id,
        production_job_id=request.production_job_id,
        scene_job_id=request.scene_job_id,
        policy_id=request.policy_id,
        reconciliation_audit_id=request.reconciliation_audit_id,
        legacy_exposure=request.legacy_exposure,
    )
    record: dict[str, object] = {
        "contract_version": _AUTHORIZATION_VERSION,
        "authorization_id": authorization_id,
        "parent_attempt_id": request.parent_attempt_id,
        "production_job_id": request.production_job_id,
        "scene_job_id": request.scene_job_id,
        "replacement_production_job_id": request.replacement_production_job_id,
        "replacement_scene_job_id": request.replacement_scene_job_id,
        "replacement_request_idempotency_key": legacy_replacement_request_idempotency_key(
            parent_attempt_id=request.parent_attempt_id,
            policy_id=request.policy_id,
            reconciliation_audit_id=request.reconciliation_audit_id,
            decision_input_digest=decision.input_digest,
        ),
        "policy_id": request.policy_id,
        "reconciliation_audit_id": request.reconciliation_audit_id,
        "decision_input_digest": decision.input_digest,
        "legacy_exposure": str(request.legacy_exposure),
        "replacement_quote": str(quote.amount),
        "replacement_quote_unit": quote.unit,
        "replacement_quote_source": quote.source,
        "replacement_quote_source_version": quote.source_version,
        "replacement_quote_input_digest": quote.input_digest,
        "replacement_quote_at": quote.quoted_at,
        "maximum_combined_exposure": str(decision.maximum_combined_exposure),
        "authorized_replacement_count": 1,
        "replacement_created": False,
        "replacement_attempt_id": None,
        "consumption_sha256": None,
        "authorization_status": "APPROVED",
        "created_at": request.created_at,
        "created_by": request.created_by,
        "software_commit": request.software_commit,
        "generate_authorized": False,
    }
    return ledger.create_legacy_replacement_authorization(record)


def consume_legacy_replacement_authorization(
    ledger: D2DailyCreditLedger,
    authorization_id: str,
    request: PhaseGExecutionRequest,
    *,
    parent_quarantined: bool,
    current_quote: ReplacementQuote,
) -> dict[str, object]:
    """Consume an exact V2 authorization with one job-bound PREPARED D2 row."""
    if not parent_quarantined:
        raise PhaseGError(
            "PHASE_G_LEGACY_REPLACEMENT_PARENT_INVALID",
            "legacy replacement parent must remain quarantined",
        )
    authorization = ledger.get_legacy_replacement_authorization(authorization_id)
    try:
        exposure = Decimal(str(authorization.get("legacy_exposure")))
    except InvalidOperation as exc:
        raise PhaseGError(
            "PHASE_G_LEGACY_REPLACEMENT_AUTH_INVALID",
            "legacy replacement exposure is invalid",
        ) from exc
    if not exposure.is_finite() or exposure <= 0:
        raise PhaseGError(
            "PHASE_G_LEGACY_REPLACEMENT_AUTH_INVALID",
            "legacy replacement exposure is invalid",
        )
    context = request.execution_context
    if (
        context is None
        or context.legacy_replacement_authorization_id != authorization_id
        or context.legacy_replacement_parent_attempt_id != authorization.get("parent_attempt_id")
        or context.production_job_id != authorization.get("replacement_production_job_id")
        or context.scene_job_id != authorization.get("replacement_scene_job_id")
        or context.legacy_replacement_quote_credits
        != _decimal_value(authorization.get("replacement_quote"))
        or context.legacy_replacement_quote_input_digest
        != authorization.get("replacement_quote_input_digest")
        or context.legacy_replacement_quote_source_version
        != authorization.get("replacement_quote_source_version")
    ):
        raise PhaseGError(
            "PHASE_G_LEGACY_REPLACEMENT_AUTH_INVALID",
            "replacement request does not bind the exact V2 child jobs and quote",
        )
    _validate_authorization_audits(
        ledger,
        parent_attempt_id=str(authorization.get("parent_attempt_id", "")),
        production_job_id=str(authorization.get("production_job_id", "")),
        scene_job_id=str(authorization.get("scene_job_id", "")),
        policy_id=str(authorization.get("policy_id", "")),
        reconciliation_audit_id=str(authorization.get("reconciliation_audit_id", "")),
        legacy_exposure=exposure,
    )
    return ledger.consume_legacy_replacement_authorization(
        authorization_id,
        request,
        current_quote={
            "status": current_quote.status.value,
            "amount": str(current_quote.amount) if current_quote.amount is not None else None,
            "unit": current_quote.unit,
            "source_version": current_quote.source_version,
            "input_digest": current_quote.input_digest,
            "valid": current_quote.valid,
        },
    )

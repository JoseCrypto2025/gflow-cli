from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from gflow_cli.v2_runtime.production_execution import (
    CreditBudgetContext,
    ProductionCreditGuard,
    ProductionExecutionContext,
    ProductionExecutionError,
)

_PROD_PROJECT = "aafe9282-071f-4333-baa4-92b9f76a9c27"


def _context(
    environment: str = "PROD", attempt: str = "D6_attempt_0001"
) -> ProductionExecutionContext:
    return ProductionExecutionContext(
        environment,
        "PRV_0001",
        "PVA_0002" if environment == "PROD" else "PVA_0001",
        _PROD_PROJECT if environment == "PROD" else "test-project",
        "GFLOW_PROD_AUTH" if environment == "PROD" else "GFLOW_V2_TEST_AUTH",
        "SCN_000001",
        "GPR_0002" if environment == "PROD" else "GPR_0001",
        "SPV_000006" if environment == "PROD" else "SPV_000004",
        attempt,
        CreditBudgetContext(
            authorized_run_ceiling=Decimal("30"),
            provider_remaining_free=Decimal("50"),
        ),
        production_job_id="PRD_000001" if environment == "PROD" else None,
        scene_job_id="SJB_000001" if environment == "PROD" else None,
        max_reservation_credits=Decimal("30") if environment == "PROD" else None,
        reservation_quote_input_digest="a" * 64 if environment == "PROD" else None,
        reservation_quote_source_version="PCT_000001:verified:snapshot:sha256"
        if environment == "PROD"
        else None,
    )


def _replacement_context() -> ProductionExecutionContext:
    return replace(
        _context("PROD", "D2_replacement-0001"),
        legacy_replacement_authorization_id="LRA_" + "A" * 24,
        legacy_replacement_parent_attempt_id="D2_parent-00000001",
        legacy_replacement_quote_credits=Decimal("10"),
        legacy_replacement_quote_input_digest="a" * 64,
        legacy_replacement_quote_source_version="PCT_000001:verified:snapshot:sha256",
    )


def test_replacement_quote_is_reserved_before_generate_and_exactly_once(tmp_path: Path) -> None:
    guard = ProductionCreditGuard(tmp_path / "runtime")
    fixed = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    context = _replacement_context()
    guard.prepare(context, now=fixed)

    amount = guard.reserve_replacement_quote_before_submit(
        context,
        quoted_credits=Decimal("10"),
        budget=context.credit_budget_context,
        now=fixed,
    )
    replay = guard.reserve_replacement_quote_before_submit(
        context,
        quoted_credits=Decimal("10"),
        budget=context.credit_budget_context,
        now=fixed,
    )
    row = guard._read()["attempts"][0]  # noqa: SLF001 - inspect persisted pre-click state

    assert amount == replay == Decimal("10")
    assert row["state"] == "PRE_GENERATE_RESERVED"
    assert row["quoted_credits"] == row["reserved_credits"] == "10"
    assert row["generate_click_count"] == 0
    assert row["legacy_replacement_authorization_id"] == context.legacy_replacement_authorization_id

    guard.record_replacement_generate_click(
        context.attempt_id,
        authorization_id=str(context.legacy_replacement_authorization_id),
    )
    with pytest.raises(ProductionExecutionError, match="single Generate"):
        guard.record_replacement_generate_click(
            context.attempt_id,
            authorization_id=str(context.legacy_replacement_authorization_id),
        )


def test_replacement_confirmation_must_match_pre_reserved_quote(tmp_path: Path) -> None:
    guard = ProductionCreditGuard(tmp_path / "runtime")
    fixed = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    context = _replacement_context()
    guard.prepare(context, now=fixed)
    guard.reserve_replacement_quote_before_submit(
        context,
        quoted_credits=10,
        budget=context.credit_budget_context,
        now=fixed,
    )
    guard.record_replacement_generate_click(
        context.attempt_id,
        authorization_id=str(context.legacy_replacement_authorization_id),
    )

    assert guard.verify_replacement_confirmation(context, video_count=1, quoted_credits=10) == 10
    with pytest.raises(ProductionExecutionError, match="differs"):
        guard.verify_replacement_confirmation(context, video_count=1, quoted_credits=11)
    with pytest.raises(ProductionExecutionError, match="differs"):
        guard.verify_replacement_confirmation(context, video_count=2, quoted_credits=10)

    guard.resolve(context.attempt_id, "UNKNOWN_SUBMISSION_STATE")
    row = guard._read()["attempts"][0]  # noqa: SLF001 - unknown retains exact quote exposure
    assert row["state"] == "UNKNOWN_SUBMISSION_STATE"
    assert row["reserved_credits"] == "10"


def test_replacement_reservation_rejects_mismatch_day_conflict_and_release_after_click(
    tmp_path: Path,
) -> None:
    guard = ProductionCreditGuard(tmp_path / "runtime")
    fixed = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    context = _replacement_context()
    guard.prepare(context, now=fixed)
    with pytest.raises(ProductionExecutionError, match="exact credit capability"):
        guard.reserve_replacement_quote_before_submit(
            context,
            quoted_credits=11,
            budget=context.credit_budget_context,
            now=fixed,
        )
    guard.reserve_replacement_quote_before_submit(
        context,
        quoted_credits=10,
        budget=context.credit_budget_context,
        now=fixed,
    )
    with pytest.raises(ProductionExecutionError, match="single Generate"):
        guard.record_generate_click(context.attempt_id)
    guard.record_replacement_generate_click(
        context.attempt_id,
        authorization_id=str(context.legacy_replacement_authorization_id),
    )
    with pytest.raises(ProductionExecutionError, match="pre-Generate"):
        guard.release_replacement_before_submit(context)

    conflicting = replace(_context("PROD", "D2_replacement-0002"))
    with pytest.raises(ProductionExecutionError, match="unresolved"):
        guard.prepare(conflicting, now=fixed)


def test_pre_generate_replacement_release_requires_zero_clicks(tmp_path: Path) -> None:
    guard = ProductionCreditGuard(tmp_path / "runtime")
    fixed = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    context = _replacement_context()
    guard.prepare(context, now=fixed)
    guard.reserve_replacement_quote_before_submit(
        context,
        quoted_credits=10,
        budget=context.credit_budget_context,
        now=fixed,
    )

    guard.release_replacement_before_submit(context)
    row = guard._read()["attempts"][0]  # noqa: SLF001 - validate safe zero-click release
    assert row["state"] == "FAILED_CONFIRMED"
    assert row["reserved_credits"] == "0"
    assert row["generate_click_count"] == 0


def test_execution_context_requires_exact_resolved_identity() -> None:
    context = _context()
    resolved = {
        "environment": "PROD",
        "provider_id": "PRV_0001",
        "provider_account_id": "PVA_0002",
        "provider_project_id": _PROD_PROJECT,
        "auth_profile": "GFLOW_PROD_AUTH",
        "scene_id": "SCN_000001",
        "generation_profile_id": "GPR_0002",
        "scene_prompt_id": "SPV_000006",
    }
    context.validate(resolved)
    resolved["provider_account_id"] = "PVA_0001"
    with pytest.raises(ProductionExecutionError, match="differs"):
        context.validate(resolved)


def test_prod_context_rejects_noncanonical_project() -> None:
    with pytest.raises(ProductionExecutionError, match="canonically pinned"):
        replace(_context(), provider_project_id="date-stamped-project").validate(
            {
                "environment": "PROD",
                "provider_id": "PRV_0001",
                "provider_account_id": "PVA_0002",
                "provider_project_id": "date-stamped-project",
                "auth_profile": "GFLOW_PROD_AUTH",
                "scene_id": "SCN_000001",
                "generation_profile_id": "GPR_0002",
                "scene_prompt_id": "SPV_000006",
            }
        )


def test_daily_credit_guard_is_shared_across_test_and_prod(tmp_path: Path) -> None:
    guard = ProductionCreditGuard(tmp_path / "runtime")
    fixed = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    test_context = _context("TEST", "D6_test_0001")
    prod_context = _context("PROD", "D6_prod_0001")
    guard.prepare(test_context, now=fixed)
    guard.record_generate_click(test_context.attempt_id)
    guard.reserve_quote(
        test_context.attempt_id,
        video_count=1,
        quoted_credits=Decimal("30"),
        budget=test_context.credit_budget_context,
    )
    guard.resolve(test_context.attempt_id, "FAILED_CONFIRMED", actual_credits=Decimal("30"))

    with pytest.raises(ProductionExecutionError, match="shared daily"):
        guard.prepare(prod_context, now=fixed)

    assert all(row["attempt_id"] != prod_context.attempt_id for row in guard._read()["attempts"])  # noqa: SLF001 - insufficient daily capacity creates no attempt or reservation


def test_prod_quote_cannot_exceed_bound_per_run_cap(tmp_path: Path) -> None:
    guard = ProductionCreditGuard(tmp_path / "runtime")
    fixed = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    context = replace(
        _context("PROD", "D2_capped-0001"),
        max_reservation_credits=Decimal("10"),
        reservation_quote_input_digest="a" * 64,
        reservation_quote_source_version="PCT_000001:verified:snapshot:sha256",
    )
    guard.prepare(context, now=fixed)
    guard.record_generate_click(context.attempt_id)

    with pytest.raises(ProductionExecutionError, match="per-run quote cap"):
        guard.reserve_quote(
            context.attempt_id,
            video_count=1,
            quoted_credits=Decimal("50"),
            budget=context.credit_budget_context,
        )

    assert guard._read()["attempts"][0]["reserved_credits"] == "0"  # noqa: SLF001


def test_prod_attempt_requires_quote_cap_before_ledger_creation(tmp_path: Path) -> None:
    guard = ProductionCreditGuard(tmp_path / "runtime")
    context = replace(
        _context("PROD", "D2_missing-cap-0001"),
        max_reservation_credits=None,
        reservation_quote_input_digest=None,
        reservation_quote_source_version=None,
        legacy_replacement_quote_credits=None,
    )

    with pytest.raises(ProductionExecutionError, match="canonical per-run quote"):
        guard.prepare(context, now=datetime(2026, 10, 18, 12, 0, tzinfo=UTC))

    assert guard._read()["attempts"] == []  # noqa: SLF001


@pytest.mark.parametrize("quote", [Decimal("10"), Decimal("20")])
def test_unknown_prod_attempt_reserves_validated_quote_not_daily_budget(
    tmp_path: Path, quote: Decimal
) -> None:
    guard = ProductionCreditGuard(tmp_path / "runtime")
    context = replace(
        _context("PROD", f"D2_quote-{quote}"),
        max_reservation_credits=quote,
    )
    guard.prepare(context, now=datetime(2026, 10, 18, 12, 0, tzinfo=UTC))
    guard.record_generate_click(context.attempt_id)

    guard.resolve(context.attempt_id, "UNKNOWN_SUBMISSION_STATE")

    row = guard._read()["attempts"][0]  # noqa: SLF001 - inspect held exposure
    assert row["validated_quote_credits"] == str(quote)
    assert row["authorized_run_ceiling"] == "30"
    assert row["reserved_credits"] == str(quote)
    assert row["state"] == "UNKNOWN_SUBMISSION_STATE"


def test_prod_quote_above_authorized_ceiling_stops_before_ledger_creation(
    tmp_path: Path,
) -> None:
    guard = ProductionCreditGuard(tmp_path / "runtime")
    context = replace(
        _context("PROD", "D2_quote-over-ceiling"),
        max_reservation_credits=Decimal("40"),
    )

    with pytest.raises(ProductionExecutionError, match="authorized per-run credit ceiling"):
        guard.prepare(context, now=datetime(2026, 10, 18, 12, 0, tzinfo=UTC))

    assert guard._read()["attempts"] == []  # noqa: SLF001 - no reservation was created


def test_prod_confirmation_quote_may_reserve_less_than_validated_quote(tmp_path: Path) -> None:
    guard = ProductionCreditGuard(tmp_path / "runtime")
    context = replace(
        _context("PROD", "D2_lower-confirmation-quote"),
        max_reservation_credits=Decimal("10"),
    )
    guard.prepare(context, now=datetime(2026, 10, 18, 12, 0, tzinfo=UTC))
    guard.record_generate_click(context.attempt_id)
    guard.reserve_quote(
        context.attempt_id,
        video_count=1,
        quoted_credits=Decimal("6"),
        budget=context.credit_budget_context,
    )

    guard.resolve(context.attempt_id, "UNKNOWN_SUBMISSION_STATE")

    assert guard._read()["attempts"][0]["reserved_credits"] == "6"  # noqa: SLF001


def test_replayed_unknown_state_preserves_existing_historical_reservation(tmp_path: Path) -> None:
    guard = ProductionCreditGuard(tmp_path / "runtime")
    context = replace(
        _context("PROD", "D2_historical-unknown"),
        max_reservation_credits=Decimal("10"),
    )
    guard.prepare(context, now=datetime(2026, 10, 18, 12, 0, tzinfo=UTC))
    guard.record_generate_click(context.attempt_id)
    guard.resolve(context.attempt_id, "UNKNOWN_SUBMISSION_STATE")

    def seed_historical_reservation(payload: dict[str, object]) -> dict[str, object]:
        attempts = payload["attempts"]
        assert isinstance(attempts, list)
        attempts[0]["reserved_credits"] = "50"
        attempts[0]["quoted_credits"] = None
        return payload

    guard._mutate(seed_historical_reservation)  # noqa: SLF001 - model existing deployed row
    before = guard._read()["attempts"][0]  # noqa: SLF001 - verify immutable replay
    guard.resolve(context.attempt_id, "UNKNOWN_SUBMISSION_STATE")
    after = guard._read()["attempts"][0]  # noqa: SLF001 - verify immutable replay

    assert after == before
    assert after["reserved_credits"] == "50"


def test_unknown_attempt_reserves_ceiling_and_blocks_next_environment(tmp_path: Path) -> None:
    guard = ProductionCreditGuard(tmp_path / "runtime")
    fixed = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    context = _context("TEST", "D6_test_0002")
    guard.prepare(context, now=fixed)
    guard.record_generate_click(context.attempt_id)
    guard.resolve(context.attempt_id, "UNKNOWN_SUBMISSION_STATE")
    prod = _context("PROD", "D6_prod_0002")
    with pytest.raises(ProductionExecutionError, match="unresolved"):
        guard.prepare(prod, now=fixed)


def test_proven_not_submitted_release_is_identity_bound_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gflow_cli.v2_runtime import phase_g
    from gflow_cli.v2_runtime.phase_g_prod_reconciliation import (
        ProdAttemptIdentity,
        ProdReconciliationEvidence,
        ProdReconciliationResult,
        evidence_audit_record,
    )

    guard = ProductionCreditGuard(tmp_path / "runtime")
    context = _context("PROD", "D2_prod-release-0001")
    guard.prepare(context, now=datetime(2026, 10, 18, 12, 0, tzinfo=UTC))
    guard.record_generate_click(context.attempt_id)
    guard.reserve_quote(
        context.attempt_id,
        video_count=1,
        quoted_credits=Decimal("10"),
        budget=context.credit_budget_context,
    )
    guard.resolve(context.attempt_id, "WAF_REJECTION")
    identity = ProdAttemptIdentity(
        "PRD_000001",
        "SJB_000001",
        context.attempt_id,
        context.provider_account_id,
        context.provider_project_id,
        context.scene_id,
        context.scene_prompt_id,
        context.generation_profile_id,
        "a" * 64,
    )
    evidence = ProdReconciliationEvidence(
        identity,
        "2026-10-18T12:01:00Z",
        submission_state="WAF_REJECTED",
        provider_submit_request_count=1,
        provider_submit_dispatch_window_completed=True,
        provider_submit_project_id=context.provider_project_id,
        provider_submit_response_observed=True,
        provider_submit_response_status=403,
        provider_submit_generation_record_observed=False,
        provider_submit_refusal_code="PUBLIC_ERROR_UNUSUAL_ACTIVITY",
        provider_submit_request_failed=False,
    )
    audit = evidence_audit_record(ProdReconciliationResult.PROVEN_NOT_SUBMITTED, evidence)
    audit["contract_version"] = "D2_PROD_SAME_ATTEMPT_RECOVERY_V1"
    attempt = {
        "execution_environment": "PROD",
        "production_job_id": identity.production_job_id,
        "scene_job_id": identity.scene_job_id,
        "d2_run_id": identity.d2_run_id,
        "provider_account_id": identity.provider_account_id,
        "provider_project_id": identity.provider_project_id,
        "scene_id": identity.scene_id,
        "scene_prompt_id": identity.scene_prompt_id,
        "generation_profile_id": identity.generation_profile_id,
        "prompt_sha256": identity.prompt_sha256,
        "generate_action_count": 1,
        "generate_action_at": "2026-10-18T12:00:00Z",
        "baseline_captured": True,
        "baseline_execution_ids": [],
        "baseline_media_ids": [],
        "baseline_confirmation_ids": [],
        "updated_at": evidence.observed_at,
        "submission_state": evidence.submission_state,
        "provider_submit_request_count": evidence.provider_submit_request_count,
        "provider_submit_rpc_observed": True,
        "provider_submit_rpc_id": "YhhmEf",
        "provider_submit_http_method": "POST",
        "provider_submit_observed_at": "2026-10-18T12:00:30Z",
        "provider_submit_dispatch_window_completed": True,
        "provider_submit_project_id": evidence.provider_submit_project_id,
        "provider_submit_response_observed": True,
        "provider_submit_response_status": 403,
        "provider_submit_generation_record_observed": False,
        "provider_submit_refusal_detected": True,
        "provider_submit_refusal_code": evidence.provider_submit_refusal_code,
        "provider_submit_request_failed": False,
        "provider_execution_id": None,
        "provider_media_ids": [],
        "provider_media_id": None,
        "observed_confirmation_id": None,
        "prod_reconciliation_history": [audit],
    }

    class PersistedLedger:
        def __init__(self, _root: Path) -> None:
            pass

        def attempts(self):
            return (attempt,)

    monkeypatch.setattr(phase_g, "D2DailyCreditLedger", PersistedLedger)
    release = {
        "evidence_sha256": evidence.digest(),
        "production_job_id": identity.production_job_id,
        "scene_job_id": identity.scene_job_id,
        "provider_account_id": identity.provider_account_id,
        "provider_project_id": identity.provider_project_id,
    }
    guard.release_proven_not_submitted(context.attempt_id, **release)
    guard.release_proven_not_submitted(context.attempt_id, **release)
    row = guard._read()["attempts"][0]  # noqa: SLF001 - validates persisted reservation state
    assert row["state"] == "WAF_REJECTION"
    assert row["reserved_credits"] == "0"
    assert row["billing_outcome"] == "UNKNOWN"

    with pytest.raises(ProductionExecutionError, match="identity"):
        guard.release_proven_not_submitted(
            context.attempt_id,
            **{**release, "scene_job_id": "SJB_000002"},
        )


@pytest.mark.parametrize(
    "budget_args",
    [
        {"is_free_quota": False},
        {"paid_fallback": True},
        {"quote_unit": "USD"},
        {"provider_remaining_free": Decimal("4")},
    ],
)
def test_credit_guard_refuses_paid_unknown_unit_or_insufficient_balance(
    tmp_path: Path, budget_args: dict[str, object]
) -> None:
    guard = ProductionCreditGuard(tmp_path / "runtime")
    context = _context()
    guard.prepare(context)
    guard.record_generate_click(context.attempt_id)
    with pytest.raises(ProductionExecutionError):
        budget = CreditBudgetContext(**budget_args)  # type: ignore[arg-type]
        guard.reserve_quote(
            context.attempt_id,
            video_count=1,
            quoted_credits=5,
            budget=budget,
        )


def test_credit_guard_refuses_unknown_quote_and_second_click(tmp_path: Path) -> None:
    guard = ProductionCreditGuard(tmp_path / "runtime")
    context = _context()
    guard.prepare(context)
    guard.record_generate_click(context.attempt_id)
    with pytest.raises(ProductionExecutionError, match="known positive"):
        guard.reserve_quote(
            context.attempt_id,
            video_count=1,
            quoted_credits=None,
            budget=context.credit_budget_context,
        )
    with pytest.raises(ProductionExecutionError, match="single Generate"):
        guard.record_generate_click(context.attempt_id)


def test_credit_guard_attempt_cannot_be_rebound_to_other_account(tmp_path: Path) -> None:
    guard = ProductionCreditGuard(tmp_path / "runtime")
    fixed = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    context = _context("TEST", "D6_shared_0001")
    guard.prepare(context, now=fixed)
    rebound = _context("PROD", "D6_shared_0001")
    with pytest.raises(ProductionExecutionError, match="another identity"):
        guard.prepare(rebound, now=fixed)

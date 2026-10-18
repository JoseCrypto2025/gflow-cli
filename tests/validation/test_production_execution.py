from __future__ import annotations

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


def _context(
    environment: str = "PROD", attempt: str = "D6_attempt_0001"
) -> ProductionExecutionContext:
    return ProductionExecutionContext(
        environment,
        "PRV_0001",
        "PVA_0002" if environment == "PROD" else "PVA_0001",
        "prod-project" if environment == "PROD" else "test-project",
        "GFLOW_PROD_AUTH" if environment == "PROD" else "GFLOW_V2_TEST_AUTH",
        "SCN_000001",
        "GPR_0002" if environment == "PROD" else "GPR_0001",
        "SPV_000006" if environment == "PROD" else "SPV_000004",
        attempt,
        CreditBudgetContext(provider_remaining_free=Decimal("50")),
    )


def test_execution_context_requires_exact_resolved_identity() -> None:
    context = _context()
    resolved = {
        "environment": "PROD",
        "provider_id": "PRV_0001",
        "provider_account_id": "PVA_0002",
        "provider_project_id": "prod-project",
        "auth_profile": "GFLOW_PROD_AUTH",
        "scene_id": "SCN_000001",
        "generation_profile_id": "GPR_0002",
        "scene_prompt_id": "SPV_000006",
    }
    context.validate(resolved)
    resolved["provider_account_id"] = "PVA_0001"
    with pytest.raises(ProductionExecutionError, match="differs"):
        context.validate(resolved)


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

    guard.prepare(prod_context, now=fixed)
    guard.record_generate_click(prod_context.attempt_id)
    with pytest.raises(ProductionExecutionError, match="shared daily"):
        guard.reserve_quote(
            prod_context.attempt_id,
            video_count=1,
            quoted_credits=Decimal("21"),
            budget=prod_context.credit_budget_context,
        )


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

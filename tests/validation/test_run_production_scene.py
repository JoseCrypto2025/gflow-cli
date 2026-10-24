from __future__ import annotations

from decimal import Decimal

import pytest

from scripts.operations import run_production_scene as operator


def test_production_request_key_is_stable_and_binds_approved_prompt() -> None:
    request_key = operator._request_key("SCN_000001", "GPR_0002", "SPV_000007", "a" * 64)

    assert request_key == operator._request_key("SCN_000001", "GPR_0002", "SPV_000007", "a" * 64)
    assert request_key != operator._request_key("SCN_000001", "GPR_0002", "SPV_000007", "b" * 64)


def test_execution_context_requires_exact_live_prod_identity() -> None:
    tables = {
        "PROVIDER_ACCOUNT_MASTER": [
            {
                "Provider_Account_ID": "PVA_0002",
                "Environment": "PROD",
                "Status": "ACTIVE",
                "Auth_Profile_Key": "GFLOW_PROD_AUTH",
                "Execution_Target": "WEB_UI",
                "Base_URL": "https://flow.google.com/",
                "Provider_Project_ID": "aafe9282-071f-4333-baa4-92b9f76a9c27",
                "Provider_ID": "GOOGLE_FLOW",
            }
        ]
    }

    context = operator._execution_context(
        tables=tables,
        scene_id="SCN_000001",
        profile_id="GPR_0002",
        account_id="PVA_0002",
        project_id="aafe9282-071f-4333-baa4-92b9f76a9c27",
        prompt_id="SPV_000007",
        attempt_id="D2_1234567890abcdef",
        visible_free_credits=Decimal("50"),
    )

    assert context.environment == "PROD"
    assert context.provider_account_id == "PVA_0002"
    assert context.provider_project_id == "aafe9282-071f-4333-baa4-92b9f76a9c27"
    assert context.credit_budget_context.provider_remaining_free == Decimal("50")

    with pytest.raises(operator.ProductionSceneOperatorError, match="identity differs"):
        operator._execution_context(
            tables=tables,
            scene_id="SCN_000001",
            profile_id="GPR_0002",
            account_id="PVA_0002",
            project_id="00000000-0000-0000-0000-000000000000",
            prompt_id="SPV_000007",
            attempt_id="D2_1234567890abcdef",
            visible_free_credits=Decimal("50"),
        )


def test_current_production_credits_require_authenticated_expected_account(monkeypatch) -> None:
    async def inspect(_profile: str) -> dict[str, object]:
        return {
            "status": "ok",
            "authenticated": True,
            "email": "singleava508@gmail.com",
            "credits": 50,
        }

    monkeypatch.setattr(operator, "inspect_profile", inspect)

    assert operator._read_current_production_credits("singleava508@gmail.com") == Decimal("50")

    with pytest.raises(operator.ProductionSceneOperatorError, match="identity differs"):
        operator._read_current_production_credits("different@example.com")


def test_current_production_credits_fail_closed_when_balance_is_missing(monkeypatch) -> None:
    async def inspect(_profile: str) -> dict[str, object]:
        return {
            "status": "ok",
            "authenticated": True,
            "email": "singleava508@gmail.com",
            "credits": None,
        }

    monkeypatch.setattr(operator, "inspect_profile", inspect)

    with pytest.raises(operator.ProductionSceneOperatorError, match="balance is unavailable"):
        operator._read_current_production_credits("singleava508@gmail.com")

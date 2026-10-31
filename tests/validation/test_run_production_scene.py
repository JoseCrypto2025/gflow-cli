from __future__ import annotations

from collections.abc import Coroutine
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from gflow_cli.v2_runtime.google_sheets import (
    APPROVED_SPREADSHEET_ID,
    SOURCE_LIVE_GOOGLE_SHEETS,
    GoogleSheetsSnapshot,
)
from gflow_cli.v2_runtime.legacy_replacement import (
    ReplacementQuote,
    ReplacementQuoteStatus,
)
from scripts.operations import run_production_scene as operator


def _quote() -> ReplacementQuote:
    return ReplacementQuote(
        ReplacementQuoteStatus.VALID,
        Decimal("10"),
        "CREDIT",
        "https://support.google.com/flow/answer/16526234",
        "PCT_000001:verified:snapshot:sha256",
        "a" * 64,
        "2026-10-31T00:00:00+00:00",
        True,
        "SOURCE_VERIFIED_EFFECTIVE_DIRECT_CREDIT_RATE",
    )


def _run_immediate(awaitable: Coroutine[Any, Any, object]) -> object:
    try:
        awaitable.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError("test coroutine unexpectedly yielded")


def test_production_request_key_is_stable_and_binds_approved_prompt() -> None:
    request_key = operator._request_key(
        "SCN_000001", "GPR_0002", "SPV_000007", "a" * 64, "cert-run-a"
    )

    assert request_key == operator._request_key(
        "SCN_000001", "GPR_0002", "SPV_000007", "a" * 64, "cert-run-a"
    )
    assert request_key != operator._request_key(
        "SCN_000001", "GPR_0002", "SPV_000007", "a" * 64, "cert-run-b"
    )
    assert request_key != operator._request_key(
        "SCN_000001", "GPR_0002", "SPV_000007", "b" * 64, "cert-run-a"
    )
    assert request_key.endswith("_cert-run-a")
    assert (
        operator._request_key("SCN_000001", "GPR_0002", "SPV_000007", "a" * 64, request_key)
        == request_key
    )
    with pytest.raises(operator.ProductionSceneOperatorError, match="another workload"):
        operator._request_key("SCN_000002", "GPR_0002", "SPV_000007", "a" * 64, request_key)


def test_persisted_request_key_recovers_same_run_after_operator_restart() -> None:
    from gflow_cli.v2_runtime.production_jobs import (
        InMemoryProductionJobStore,
        ProductionJobCoordinator,
        ProductionRequest,
    )

    first_key = operator._request_key(
        "SCN_000001", "GPR_0002", "SPV_000007", "a" * 64, "certification-run-01"
    )
    first_store = InMemoryProductionJobStore()
    first_request = ProductionRequest("SCR_000001", "PROD", first_key, ("SPV_000007",))
    first_claim = ProductionJobCoordinator(first_store).submit_request(first_request)

    recovered_intent = first_claim.production_job.request_idempotency_key
    recovered_key = operator._request_key(
        "SCN_000001", "GPR_0002", "SPV_000007", "a" * 64, recovered_intent
    )
    replay_request = ProductionRequest("SCR_000001", "PROD", recovered_key, ("SPV_000007",))
    replay = ProductionJobCoordinator(first_store).submit_request(replay_request)

    assert recovered_key == first_key
    assert replay.replayed
    assert replay.production_job.production_job_id == first_claim.production_job.production_job_id


def test_unresolved_historical_same_workload_does_not_block_explicit_new_run() -> None:
    from gflow_cli.v2_runtime.production_jobs import ProductionRequest

    request = ProductionRequest("SCR_000001", "PROD", "run-3", ("SPV_000007",))
    historical = {
        "Script_ID": "SCR_000001",
        "Request_Idempotency_Key": "LEGACY_REPLACEMENT_V2_LRA_18A2C0EE3248A7C93E6D1991",
        "Scene_Set_SHA256": request.scene_set_sha256,
        "Job_Status": "REQUESTED",
    }

    assert not operator._has_unresolved_different_workload([historical], request)


def test_unresolved_different_workload_still_blocks_explicit_run() -> None:
    from gflow_cli.v2_runtime.production_jobs import ProductionRequest

    request = ProductionRequest("SCR_000001", "PROD", "run-3", ("SPV_000007",))
    other_workload = {
        "Script_ID": "SCR_000001",
        "Request_Idempotency_Key": "other-run",
        "Scene_Set_SHA256": "0" * 64,
        "Job_Status": "REQUESTED",
    }

    assert operator._has_unresolved_different_workload([other_workload], request)


def test_production_profile_home_is_pinned_to_existing_gflow_prod_path(monkeypatch) -> None:
    monkeypatch.delenv("GFLOW_CLI_HOME", raising=False)
    assert operator._require_production_profile_home() == Path("/var/lib/gflow/browser-state")
    assert operator.os.environ["GFLOW_CLI_HOME"] == operator._PROD_BROWSER_STATE_ROOT.as_posix()

    monkeypatch.setenv("GFLOW_CLI_HOME", "/tmp/other-profile-root")
    with pytest.raises(operator.ProductionSceneOperatorError, match="profile root differs"):
        operator._require_production_profile_home()


def test_execution_requires_explicit_run_intent_before_live_work(monkeypatch) -> None:
    monkeypatch.delenv("GFLOW_CLI_HOME", raising=False)
    with pytest.raises(operator.ProductionSceneOperatorError, match="explicit stable"):
        operator.run_once(
            scene_id="SCN_000001",
            profile_id="GPR_0002",
            account_id="PVA_0002",
            project_id="aafe9282-071f-4333-baa4-92b9f76a9c27",
            expected_email="singleava508@gmail.com",
            execute=True,
        )


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
        quote=_quote(),
        production_job_id="PRD_000003",
        scene_job_id="SJB_000003",
    )

    assert context.environment == "PROD"
    assert context.provider_account_id == "PVA_0002"
    assert context.provider_project_id == "aafe9282-071f-4333-baa4-92b9f76a9c27"
    assert context.credit_budget_context.provider_remaining_free == Decimal("50")
    assert context.production_job_id == "PRD_000003"
    assert context.scene_job_id == "SJB_000003"

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
            quote=_quote(),
        )


def test_unavailable_canonical_quote_fails_before_job_or_attempt_claim(monkeypatch) -> None:
    unavailable = ReplacementQuote(
        ReplacementQuoteStatus.QUOTE_UNAVAILABLE,
        None,
        None,
        None,
        None,
        "a" * 64,
        "2026-10-31T00:00:00+00:00",
        False,
        "NO_UNIQUE_EFFECTIVE_VERIFIED_DIRECT_CREDIT_ROW",
    )
    monkeypatch.setattr(operator, "_quote_config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        operator, "quote_generation_reservation", lambda *_args, **_kwargs: unavailable
    )
    snapshot = GoogleSheetsSnapshot(
        APPROVED_SPREADSHEET_ID,
        "2026-10-31T00:00:00Z",
        SOURCE_LIVE_GOOGLE_SHEETS,
        {},
    )

    with pytest.raises(operator.ProductionSceneOperatorError, match="quote is unavailable"):
        operator._validated_quote(
            snapshot,
            {},
            scene_id="SCN_000001",
            profile_id="GPR_0002",
            account_id="PVA_0002",
            effective_config={"duration_sec": 8, "aspect_ratio": "9:16", "output_count": 1},
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
    monkeypatch.setattr(operator.asyncio, "run", _run_immediate)

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
    monkeypatch.setattr(operator.asyncio, "run", _run_immediate)

    with pytest.raises(operator.ProductionSceneOperatorError, match="balance is unavailable"):
        operator._read_current_production_credits("singleava508@gmail.com")

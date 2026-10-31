from __future__ import annotations

from collections.abc import Coroutine
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
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
                "Provider_ID": "PRV_0001",
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
    assert context.credit_budget_context.authorized_run_ceiling == Decimal("30")
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


def test_preclaim_validation_does_not_require_production_or_scene_job_ids() -> None:
    from gflow_cli.v2_runtime.phase_e import PreflightResult
    from gflow_cli.v2_runtime.production_jobs import ProductionRequest

    prompt_sha = "a" * 64
    tables = {
        "PROVIDER_ACCOUNT_MASTER": [
            {
                "Provider_Account_ID": "PVA_0002",
                "Provider_ID": "PRV_0001",
                "Environment": "PROD",
                "Status": "ACTIVE",
                "Auth_Profile_Key": "GFLOW_PROD_AUTH",
                "Execution_Target": "WEB_UI",
                "Base_URL": "https://flow.google.com/",
                "Provider_Project_ID": "aafe9282-071f-4333-baa4-92b9f76a9c27",
            }
        ]
    }
    preflight = PreflightResult(
        "PREFLIGHT_PASS",
        True,
        "SCN_000001",
        "GPR_0002",
        resolved={
            "provider_id": "PRV_0001",
            "provider_account_id": "PVA_0002",
            "model_id": "MDL_0001",
            "generation_mode": "TEXT_TO_VIDEO",
        },
        technical_configuration={
            "duration_sec": 8.0,
            "aspect_ratio": "9:16",
            "output_count": 1,
        },
        prompt_sha256=prompt_sha,
    )
    request_key = operator._request_key(
        "SCN_000001", "GPR_0002", "SPV_000007", prompt_sha, "certification-run-03"
    )
    request = ProductionRequest("SCR_000001", "PROD", request_key, ("SPV_000007",))

    operator._validate_preclaim(
        tables=tables,
        preflight=preflight,
        request=request,
        quote=_quote(),
        scene_id="SCN_000001",
        profile_id="GPR_0002",
        account_id="PVA_0002",
        project_id="aafe9282-071f-4333-baa4-92b9f76a9c27",
        prompt_id="SPV_000007",
        prompt_sha256=prompt_sha,
    )


def test_full_prod_context_still_rejects_missing_job_ids() -> None:
    from gflow_cli.v2_runtime.production_execution import ProductionExecutionError

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
                "Provider_ID": "PRV_0001",
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
    )

    with pytest.raises(ProductionExecutionError, match="bind production and scene jobs"):
        context.validate(
            {
                "environment": "PROD",
                "provider_id": "PRV_0001",
                "provider_account_id": "PVA_0002",
                "provider_project_id": "aafe9282-071f-4333-baa4-92b9f76a9c27",
                "auth_profile": "GFLOW_PROD_AUTH",
                "scene_id": "SCN_000001",
                "generation_profile_id": "GPR_0002",
                "scene_prompt_id": "SPV_000007",
            }
        )


def test_claimed_run_lineage_is_validated_before_f5(monkeypatch) -> None:
    from gflow_cli.v2_runtime.production_jobs import (
        InMemoryProductionJobStore,
        ProductionJobCoordinator,
        ProductionRequest,
    )

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
                "Provider_ID": "PRV_0001",
            }
        ]
    }
    request = ProductionRequest(
        "SCR_000001",
        "PROD",
        operator._request_key(
            "SCN_000001", "GPR_0002", "SPV_000007", "a" * 64, "certification-run-03"
        ),
        ("SPV_000007",),
    )
    claim = ProductionJobCoordinator(InMemoryProductionJobStore()).submit_request(request)
    scene_job = claim.scene_jobs[0]
    context = operator._execution_context(
        tables=tables,
        scene_id="SCN_000001",
        profile_id="GPR_0002",
        account_id="PVA_0002",
        project_id="aafe9282-071f-4333-baa4-92b9f76a9c27",
        prompt_id="SPV_000007",
        attempt_id=operator.scene_phase_g_run_id(scene_job),
        visible_free_credits=Decimal("50"),
        quote=_quote(),
        production_job_id=claim.production_job.production_job_id,
        scene_job_id=scene_job.scene_job_id,
        production_run_no=claim.production_job.production_run_no,
        request_idempotency_key=claim.production_job.request_idempotency_key,
    )
    d2_run_id = operator.scene_phase_g_run_id(scene_job)
    phase_g_request = SimpleNamespace(execution_context=context, d2_run_id=d2_run_id)
    f5_calls: list[str] = []

    def fake_f5(_request, *, expected_email: str) -> object:
        f5_calls.append(expected_email)
        return object()

    monkeypatch.setattr(operator, "run_fresh_presubmit", fake_f5)
    operator._run_f5_after_bound_claim(
        request,
        phase_g_request,
        claim,
        expected_email="singleava508@gmail.com",
    )

    assert context.production_job_id == claim.production_job.production_job_id
    assert context.scene_job_id == scene_job.scene_job_id
    assert context.production_run_no == claim.production_job.production_run_no
    assert context.request_idempotency_key == claim.production_job.request_idempotency_key
    assert f5_calls == ["singleava508@gmail.com"]

    unbound_request = SimpleNamespace(
        d2_run_id=d2_run_id,
        execution_context=operator._execution_context(
            tables=tables,
            scene_id="SCN_000001",
            profile_id="GPR_0002",
            account_id="PVA_0002",
            project_id="aafe9282-071f-4333-baa4-92b9f76a9c27",
            prompt_id="SPV_000007",
            attempt_id=operator.scene_phase_g_run_id(scene_job),
            visible_free_credits=Decimal("50"),
            quote=_quote(),
        ),
    )
    with pytest.raises(operator.ProductionSceneOperatorError, match="bound claim"):
        operator._run_f5_after_bound_claim(
            request,
            unbound_request,
            claim,
            expected_email="singleava508@gmail.com",
        )
    assert f5_calls == ["singleava508@gmail.com"]


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

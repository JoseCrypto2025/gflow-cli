from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

import gflow_cli.v2_runtime.google_flow as flow
from gflow_cli.errors import WafRejectionError
from gflow_cli.v2_runtime.agent_recovery import AgentSettingsRecoveryJournal
from gflow_cli.v2_runtime.google_flow import (
    FlowPresubmitRequest,
    PersistedPromptVersion,
)
from gflow_cli.v2_runtime.phase_e import PreflightResult
from gflow_cli.v2_runtime.phase_g import (
    D2DailyCreditLedger,
    D2RetryAuthorization,
    D2TestSessionAuthorization,
    FlowConfirmationGate,
    FlowSubmissionBaseline,
    GoogleFlowAgentOnlyGenerationExecutor,
    PhaseGAttemptEvidence,
    PhaseGError,
    PhaseGExecutionRequest,
    PhaseGSubmitTransportEvidence,
    validate_phase_g_request,
)
from gflow_cli.v2_runtime.phase_g_artifacts import sha256_file
from gflow_cli.v2_runtime.phase_g_live_flow import parse_confirmation_quote
from gflow_cli.v2_runtime.phase_g_test_session_no_charge import (
    preapproval_no_charge_evidence_digest,
)
from gflow_cli.v2_runtime.production_execution import (
    CreditBudgetContext,
    ProductionCreditGuard,
    ProductionExecutionContext,
)


def request_for(tmp_path: Path, *, status: str = "APPROVED") -> FlowPresubmitRequest:
    prompt_text = "A red kite over the ocean."
    negative = ""
    source_hash = "a" * 64
    prompt_hash = "b" * 64
    preflight = PreflightResult(
        "PREFLIGHT_PASS",
        True,
        "SCN_000001",
        "GPR_0001",
        resolved={
            "provider_id": "PRV_0001",
            "provider_account_id": "PVA_0001",
            "model_id": "MDL_0001",
            "model_compatibility_sha256": "c" * 64,
            "generation_mode": "TEXT_TO_VIDEO",
        },
        technical_configuration={
            "generation_mode": "TEXT_TO_VIDEO",
            "duration_sec": 8,
            "aspect_ratio": "9:16",
            "output_count": 1,
            "canonical_parameters": {"DURATION_SEC": 8},
            "adapter_parameters": {"google_flow.agent.video.duration_instruction": 8},
        },
        compiled_prompt=prompt_text,
        compiled_negative_prompt=negative,
        source_context_sha256=source_hash,
        prompt_sha256=prompt_hash,
    )
    prompt = PersistedPromptVersion(
        "SPV_000002",
        "SCN_000001",
        "GPR_0001",
        status,
        prompt_text,
        negative,
        source_hash,
        prompt_hash,
    )
    return FlowPresubmitRequest(
        preflight,
        prompt,
        "PRV_0001",
        "PVA_0001",
        "MDL_0001",
        "TEXT_TO_VIDEO",
        "34e60953-e534-46e9-ac65-993893e61065",
        tmp_path / "state",
        tmp_path / "temp",
    )


def phase_g_request(tmp_path: Path, *, status: str = "APPROVED") -> PhaseGExecutionRequest:
    return PhaseGExecutionRequest.from_presubmit(
        request_for(tmp_path, status=status), "D2_test-run-0001"
    )


def _capture_ledger_baseline(ledger: D2DailyCreditLedger) -> None:
    ledger.capture_baseline(
        "D2_test-run-0001",
        FlowSubmissionBaseline(
            "34e60953-e534-46e9-ac65-993893e61065",
            frozenset({"old-media"}),
            frozenset(),
            0,
            frozenset({"old-confirmation"}),
            "turn-before",
        ),
    )


def _observe_ledger_confirmation(
    ledger: D2DailyCreditLedger,
    *,
    confirmation_id: str = "new",
    video_count: int | None = 1,
    credit_cost: Decimal | int | None = 10,
) -> None:
    ledger.observe_confirmation(
        "D2_test-run-0001",
        confirmation_id=confirmation_id,
        video_count=video_count,
        credit_cost=credit_cost,
        visible_balance=None,
    )


def test_phase_g_requires_fresh_matching_approved_prompt(tmp_path: Path) -> None:
    assert phase_g_request(tmp_path).output_count == 1
    for status in ("SUPERSEDED", "VALIDATED"):
        with pytest.raises(PhaseGError, match="not APPROVED"):
            phase_g_request(tmp_path, status=status)

    value = phase_g_request(tmp_path)
    bad_prompt = replace(value, compiled_prompt_text="mutated")
    with pytest.raises(PhaseGError, match="compiled prompt"):
        validate_phase_g_request(bad_prompt)
    bad_hash = replace(value, prompt_sha256="c" * 64)
    with pytest.raises(PhaseGError, match="hash differs"):
        validate_phase_g_request(bad_hash)


def test_phase_g_is_separate_and_f5_submission_kill_switch_stays_closed() -> None:
    assert flow.FLOW_SUBMISSION_ENABLED is False
    assert "submit" not in dir(flow.FlowPreparationPort)
    assert not hasattr(flow.GoogleFlowProviderAdapter, "submit")


def test_ledger_enforces_one_generate_and_one_quoted_confirmation(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    ledger.create_attempt(phase_g_request(tmp_path))
    _capture_ledger_baseline(ledger)
    ledger.mark_generate_action("D2_test-run-0001")
    with pytest.raises(PhaseGError) as duplicate:
        ledger.mark_generate_action("D2_test-run-0001")
    assert duplicate.value.code == "PHASE_G_DUPLICATE_SUBMIT_FORBIDDEN"
    with pytest.raises(PhaseGError) as count_error:
        ledger.approve_gate(
            "D2_test-run-0001", video_count=2, credit_cost=10, confirmation_id="new"
        )
    assert count_error.value.code == "PHASE_G_VIDEO_COUNT_UNPROVEN"
    with pytest.raises(PhaseGError) as cost_error:
        ledger.approve_gate(
            "D2_test-run-0001", video_count=1, credit_cost=None, confirmation_id="new"
        )
    assert cost_error.value.code == "PHASE_G_CREDIT_COST_UNKNOWN"

    _observe_ledger_confirmation(ledger)
    ledger.approve_gate(
        "D2_test-run-0001",
        video_count=1,
        credit_cost=Decimal("10"),
        confirmation_id="new",
    )
    with pytest.raises(PhaseGError) as second_gate:
        ledger.approve_gate(
            "D2_test-run-0001", video_count=1, credit_cost=10, confirmation_id="new"
        )
    assert second_gate.value.code == "UNEXPECTED_SECOND_CONFIRMATION"
    assert ledger.accounted_today() == Decimal("10")


def test_unknown_post_submit_state_reserves_remaining_daily_budget(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    ledger.create_attempt(phase_g_request(tmp_path))
    _capture_ledger_baseline(ledger)
    ledger.mark_generate_action("D2_test-run-0001")
    ledger.record_state("D2_test-run-0001", submission_state="UNKNOWN_SUBMISSION_STATE")
    assert ledger.accounted_today() == Decimal("50")
    with pytest.raises(PhaseGError) as duplicate:
        ledger.mark_generate_action("D2_test-run-0001")
    assert duplicate.value.code == "PHASE_G_DUPLICATE_SUBMIT_FORBIDDEN"


def _prod_certification_request(
    tmp_path: Path,
    *,
    run_id: str,
    production_job_id: str | None,
    scene_job_id: str | None,
) -> PhaseGExecutionRequest:
    base = request_for(tmp_path)
    preflight = replace(
        base.preflight,
        generation_profile_id="GPR_0002",
        resolved={**base.preflight.resolved, "provider_account_id": "PVA_0002"},
    )
    prompt = replace(base.prompt_version, generation_profile_id="GPR_0002")
    production_request = replace(
        base,
        preflight=preflight,
        prompt_version=prompt,
        provider_account_id="PVA_0002",
        provider_project_id="aafe9282-071f-4333-baa4-92b9f76a9c27",
    )
    context = ProductionExecutionContext(
        environment="PROD",
        provider_id="PRV_0001",
        provider_account_id="PVA_0002",
        provider_project_id="aafe9282-071f-4333-baa4-92b9f76a9c27",
        auth_profile="GFLOW_PROD_AUTH",
        scene_id="SCN_000001",
        generation_profile_id="GPR_0002",
        scene_prompt_id="SPV_000002",
        attempt_id=run_id,
        credit_budget_context=CreditBudgetContext(provider_remaining_free=Decimal("50")),
        production_job_id=production_job_id,
        scene_job_id=scene_job_id,
        max_reservation_credits=Decimal("10"),
        reservation_quote_input_digest="d" * 64,
        reservation_quote_source_version="PCT_000001:verified:test",
    )
    return PhaseGExecutionRequest.from_presubmit(
        production_request, run_id, execution_context=context
    )


def _record_unknown_prod_attempt(
    ledger: D2DailyCreditLedger, request: PhaseGExecutionRequest
) -> None:
    ledger.capture_baseline(
        request.d2_run_id,
        FlowSubmissionBaseline(
            request.provider_project_id,
            frozenset(),
            frozenset(),
            0,
            frozenset(),
            "turn-before",
        ),
    )
    ledger.mark_generate_action(request.d2_run_id)
    ledger.record_state(
        request.d2_run_id,
        submission_state="UNKNOWN_SUBMISSION_STATE",
        accounted_credit_cost=50,
    )


def test_prod_unknown_attempt_blocks_replay_of_same_certification_run(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    first = _prod_certification_request(
        tmp_path,
        run_id="D2_prod-run-0001",
        production_job_id="PRD_000001",
        scene_job_id="SJB_000001",
    )
    ledger.create_attempt(first)
    _record_unknown_prod_attempt(ledger, first)

    replay = _prod_certification_request(
        tmp_path,
        run_id="D2_prod-run-0002",
        production_job_id="PRD_000001",
        scene_job_id="SJB_000001",
    )
    with pytest.raises(PhaseGError) as blocked:
        ledger.create_attempt(replay)

    assert blocked.value.code == "PHASE_G_ATTEMPT_UNRESOLVED"
    assert len(ledger.attempts()) == 1


def test_prod_reconciled_ambiguous_attempt_blocks_same_run_retry(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    first = _prod_certification_request(
        tmp_path,
        run_id="D2_prod-run-0001",
        production_job_id="PRD_000001",
        scene_job_id="SJB_000001",
    )
    ledger.create_attempt(first)
    _record_unknown_prod_attempt(ledger, first)
    ledger._mutate(  # noqa: SLF001 - seed a previously persisted PROD reconciliation result
        lambda payload: payload["attempts"][0].update(
            prod_reconciliation_history=[
                {
                    "contract_version": "D2_PROD_SAME_ATTEMPT_RECOVERY_V1",
                    "result": "AMBIGUOUS_AFTER_RECONCILIATION",
                }
            ]
        )
    )
    replay = _prod_certification_request(
        tmp_path,
        run_id="D2_prod-run-0002",
        production_job_id="PRD_000001",
        scene_job_id="SJB_000001",
    )

    with pytest.raises(PhaseGError) as blocked:
        ledger.create_attempt(replay)

    assert blocked.value.code == "PHASE_G_ATTEMPT_UNRESOLVED"
    assert len(ledger.attempts()) == 1


def test_prod_explicit_new_certification_run_skips_historical_unknowns_immutably(
    tmp_path: Path,
) -> None:
    from gflow_cli.v2_runtime.production_jobs import (
        InMemoryProductionJobStore,
        ProductionJobCoordinator,
        ProductionRequest,
    )

    job_store = InMemoryProductionJobStore()
    first_claim = ProductionJobCoordinator(job_store).submit_request(
        ProductionRequest("SCR_000001", "PROD", "certification-run-intent-01", ("SPV_000007",))
    )
    second_request = ProductionRequest(
        "SCR_000001", "PROD", "certification-run-intent-02", ("SPV_000007",)
    )
    second_claim = ProductionJobCoordinator(job_store).submit_request(second_request)
    run_two_replay = ProductionJobCoordinator(job_store).submit_request(second_request)
    third_claim = ProductionJobCoordinator(job_store).submit_request(
        ProductionRequest("SCR_000001", "PROD", "certification-run-intent-03", ("SPV_000007",))
    )
    assert [
        first_claim.production_job.production_run_no,
        second_claim.production_job.production_run_no,
        third_claim.production_job.production_run_no,
    ] == [1, 2, 3]
    assert run_two_replay.replayed
    assert (
        run_two_replay.production_job.production_job_id
        == second_claim.production_job.production_job_id
    )
    assert first_claim.production_job.request_idempotency_key != (
        second_claim.production_job.request_idempotency_key
    )
    assert second_claim.production_job.request_idempotency_key != (
        third_claim.production_job.request_idempotency_key
    )

    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    historical_requests = [
        _prod_certification_request(
            tmp_path,
            run_id="D2_prod-run-0001",
            production_job_id=first_claim.production_job.production_job_id,
            scene_job_id=first_claim.scene_jobs[0].scene_job_id,
        ),
        _prod_certification_request(
            tmp_path,
            run_id="D2_prod-run-0002",
            production_job_id=second_claim.production_job.production_job_id,
            scene_job_id=second_claim.scene_jobs[0].scene_job_id,
        ),
    ]
    for request in historical_requests:
        ledger.create_attempt(request)
        _record_unknown_prod_attempt(ledger, request)
        if request is historical_requests[0]:
            previous_day = (
                datetime.now(UTC).astimezone(timezone(timedelta(hours=7))).date()
                - timedelta(days=1)
            ).isoformat()
            ledger._mutate(  # noqa: SLF001 - model a reservation from an earlier Bangkok day
                lambda payload, day=previous_day: payload["attempts"][0].update(accounting_date=day)
            )
    historical_before = ledger.attempts()

    run_three = _prod_certification_request(
        tmp_path,
        run_id="D2_prod-run-0003",
        production_job_id=third_claim.production_job.production_job_id,
        scene_job_id=third_claim.scene_jobs[0].scene_job_id,
    )
    created = ledger.create_attempt(run_three)

    historical_after = ledger.attempts()[:2]
    assert historical_after == historical_before
    assert [row["generate_action_count"] for row in historical_after] == [1, 1]
    assert [row["accounted_credit_cost"] for row in historical_after] == ["50", "50"]
    assert created["production_job_id"] == third_claim.production_job.production_job_id
    assert created["scene_job_id"] == third_claim.scene_jobs[0].scene_job_id
    assert created["generate_action_count"] == 0
    assert created["approval_action_count"] == 0
    assert ledger.accounted_today() == Decimal("50")


def test_prod_unknown_history_without_run_lineage_still_blocks(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    historical = _prod_certification_request(
        tmp_path,
        run_id="D2_prod-run-0001",
        production_job_id="PRD_000001",
        scene_job_id="SJB_000001",
    )
    ledger.create_attempt(historical)
    _record_unknown_prod_attempt(ledger, historical)

    missing_lineage = _prod_certification_request(
        tmp_path,
        run_id="D2_prod-run-0002",
        production_job_id=None,
        scene_job_id=None,
    )
    with pytest.raises(PhaseGError) as blocked:
        ledger.create_attempt(missing_lineage)

    assert blocked.value.code == "PHASE_G_ATTEMPT_UNRESOLVED"
    assert len(ledger.attempts()) == 1


def test_submit_transport_evidence_is_sanitized_and_explicit(tmp_path: Path) -> None:
    request = phase_g_request(tmp_path)
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    ledger.create_attempt(request)
    baseline = FlowSubmissionBaseline(
        request.provider_project_id, frozenset(), frozenset(), 0, frozenset(), "0:"
    )
    ledger.capture_baseline(request.d2_run_id, baseline)
    ledger.mark_generate_action(request.d2_run_id)

    attempt = ledger.record_submit_transport_evidence(
        request.d2_run_id,
        PhaseGSubmitTransportEvidence(
            generate_click_dispatched=True,
            provider_submit_rpc_observed=True,
            provider_submit_rpc_id="YhhmEf",
            provider_submit_observed_at="2026-10-23T00:00:00.000Z",
            provider_submit_http_method="POST",
            provider_submit_project_id=request.provider_project_id,
            provider_submit_request_count=1,
            provider_submit_dispatch_window_completed=True,
            provider_submit_response_observed=False,
            provider_submit_request_sha256="a" * 64,
        ),
    )

    assert attempt["provider_submit_rpc_observed"] is True
    assert attempt["provider_submit_request_count"] == 1
    assert attempt["provider_submit_request_sha256"] == "a" * 64
    assert attempt["provider_submit_response_sha256"] is None
    assert attempt["provider_submit_transport_classification"] == ("SUBMIT_SENT_RESPONSE_UNKNOWN")
    assert "request_body" not in attempt
    assert "headers" not in attempt


def test_generate_checkpoint_and_outcome_snapshot_are_durable_and_idempotent(
    tmp_path: Path,
) -> None:
    request = phase_g_request(tmp_path)
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    ledger.create_attempt(request)
    ledger.capture_baseline(
        request.d2_run_id,
        FlowSubmissionBaseline(
            request.provider_project_id,
            frozenset({"old-media"}),
            frozenset({"old-execution"}),
            0,
            frozenset(),
            "0:",
        ),
    )
    ledger._mutate(  # noqa: SLF001 - exercise PROD-only click evidence on a focused attempt
        lambda payload: payload["attempts"][0].update(execution_environment="PROD")
    )
    ledger.mark_generate_action(request.d2_run_id)
    attempted = ledger.record_state(request.d2_run_id, submission_state="UNKNOWN_SUBMISSION_STATE")
    assert attempted["baseline_project_id"] == request.provider_project_id
    assert attempted["baseline_captured_at"]
    assert attempted["generate_action_intent_at"] == attempted["generate_action_at"]
    assert attempted["output_count_at_click"] == 1
    assert attempted["active_project_id_at_click"] == request.provider_project_id
    assert attempted["prompt_sha256_at_click"] == request.prompt_sha256

    snapshot = {
        "project_id": request.provider_project_id,
        "media_ids": ("old-media", "new-media"),
        "execution_ids": ("old-execution", "new-execution"),
    }
    first = ledger.capture_outcome_snapshot(
        request.d2_run_id, **snapshot, observed_at="2026-10-30T12:00:00Z"
    )
    replay = ledger.capture_outcome_snapshot(
        request.d2_run_id, **snapshot, observed_at="2026-10-30T12:01:00Z"
    )
    assert len(first["asset_outcome_snapshots"]) == 1
    assert len(replay["asset_outcome_snapshots"]) == 1
    assert replay["generate_action_count"] == 1
    assert replay["accounted_credit_cost"] == attempted["accounted_credit_cost"]
    assert replay["provider_project_id"] == request.provider_project_id
    with pytest.raises(PhaseGError) as mismatch:
        ledger.capture_outcome_snapshot(
            request.d2_run_id,
            project_id="wrong-project",
            media_ids=("new-media",),
            execution_ids=(),
            observed_at="2026-10-30T12:02:00Z",
        )
    assert mismatch.value.code == "PHASE_G_PROJECT_MISMATCH"


def test_correlated_submit_rpc_reason_is_allowlisted_without_terminalizing_attempt(
    tmp_path: Path,
) -> None:
    request = phase_g_request(tmp_path)
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    ledger.create_attempt(request)
    _capture_ledger_baseline(ledger)
    ledger.mark_generate_action(request.d2_run_id)

    attempt = ledger.record_submit_transport_evidence(
        request.d2_run_id,
        PhaseGSubmitTransportEvidence(
            generate_click_dispatched=True,
            provider_submit_rpc_observed=True,
            provider_submit_rpc_id="YhhmEf",
            provider_submit_observed_at="2026-10-23T00:00:00.000Z",
            provider_submit_http_method="POST",
            provider_submit_project_id=request.provider_project_id,
            provider_submit_request_count=1,
            provider_submit_dispatch_window_completed=True,
            provider_submit_response_observed=True,
            provider_submit_response_status=200,
            provider_submit_generation_record_observed=False,
            provider_submit_refusal_detected=True,
            provider_submit_refusal_code="TEST_ONLY_REASON_TOKEN",
            provider_submit_request_failed=False,
            provider_submit_response_sha256="b" * 64,
            provider_submit_response_at="2026-10-23T00:00:01.000Z",
        ),
    )

    assert attempt["submission_state"] == "GENERATE_ACTION_RECORDED"
    assert attempt["provider_submit_refusal_detected"] is True
    assert attempt["provider_submit_response_sha256"] == "b" * 64
    assert attempt["provider_submit_response_at"] == "2026-10-23T00:00:01.000Z"
    assert attempt["provider_submit_transport_classification"] == "SUBMIT_RESPONSE_PROVIDER_REFUSAL"


def test_submit_rpc_reason_rejects_unstructured_provider_text(tmp_path: Path) -> None:
    request = phase_g_request(tmp_path)
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    ledger.create_attempt(request)
    _capture_ledger_baseline(ledger)
    ledger.mark_generate_action(request.d2_run_id)

    with pytest.raises(PhaseGError, match="refusal code is invalid"):
        ledger.record_submit_transport_evidence(
            request.d2_run_id,
            PhaseGSubmitTransportEvidence(
                generate_click_dispatched=True,
                provider_submit_rpc_observed=True,
                provider_submit_rpc_id="YhhmEf",
                provider_submit_observed_at="2026-10-23T00:00:00.000Z",
                provider_submit_http_method="POST",
                provider_submit_project_id=request.provider_project_id,
                provider_submit_request_count=1,
                provider_submit_dispatch_window_completed=True,
                provider_submit_response_observed=True,
                provider_submit_response_status=200,
                provider_submit_generation_record_observed=False,
                provider_submit_refusal_detected=True,
                provider_submit_refusal_code="capacity unavailable; retry later",
                provider_submit_request_failed=False,
            ),
        )


def test_late_gate_reconciles_unknown_reservation_without_claiming_provider_charge(
    tmp_path: Path,
) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    ledger.create_attempt(phase_g_request(tmp_path))
    _capture_ledger_baseline(ledger)
    ledger.mark_generate_action("D2_test-run-0001")
    ledger.record_state("D2_test-run-0001", submission_state="UNKNOWN_SUBMISSION_STATE")
    expected_sha = sha256_file(ledger.path)

    reconciled = ledger.record_late_confirmation(
        "D2_test-run-0001",
        expected_ledger_sha256=expected_sha,
        confirmation_id="late-gate",
        project_id="34e60953-e534-46e9-ac65-993893e61065",
        video_count=1,
        credit_cost=10,
    )

    assert reconciled["prior_unknown_reservation_credit_cost"] == "50"
    assert reconciled["quoted_credit_cost"] == "10"
    assert reconciled["accounted_credit_cost"] == "10"
    assert reconciled["provider_billing_outcome"] == "UNKNOWN"
    assert ledger.accounted_today() == Decimal("10")

    approved = ledger.approve_gate(
        "D2_test-run-0001", video_count=1, credit_cost=10, confirmation_id="late-gate"
    )
    assert approved["approval_action_count"] == 1
    assert approved["submission_state"] == "COST_RESERVED_AWAITING_APPROVAL"
    assert ledger.accounted_today() == Decimal("10")
    with pytest.raises(PhaseGError, match="already approved"):
        ledger.approve_gate(
            "D2_test-run-0001", video_count=1, credit_cost=10, confirmation_id="late-gate"
        )


def test_late_gate_reconciliation_requires_exact_ledger_hash(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    ledger.create_attempt(phase_g_request(tmp_path))
    _capture_ledger_baseline(ledger)
    ledger.mark_generate_action("D2_test-run-0001")
    ledger.record_state("D2_test-run-0001", submission_state="UNKNOWN_SUBMISSION_STATE")
    with pytest.raises(PhaseGError) as changed:
        ledger.record_late_confirmation(
            "D2_test-run-0001",
            expected_ledger_sha256="0" * 64,
            confirmation_id="late-gate",
            project_id="34e60953-e534-46e9-ac65-993893e61065",
            video_count=1,
            credit_cost=10,
        )
    assert changed.value.code == "PHASE_G_LEDGER_CHANGED"


@pytest.mark.asyncio
async def test_optional_confirmation_can_complete_same_single_submit(tmp_path: Path) -> None:
    port = FakeGenerationPort()
    request, ledger, _journal, executor = make_executor(tmp_path, port)

    async def no_gate(_baseline: FlowSubmissionBaseline, *, observation_deadline: float):
        del observation_deadline
        port.calls.append("no_gate")
        return None

    port.await_confirmation = no_gate  # type: ignore[method-assign]
    port.evidence = replace(
        port.evidence,
        accounted_credit_cost=None,
        attributed_confirmation_id=None,
    )

    result = await executor.execute(request)

    attempt = ledger.attempts()[0]
    assert result.submission_state == "GENERATION_SUCCEEDED"
    assert result.confirmation_id == ""
    assert attempt["generate_action_count"] == 1
    assert attempt["approval_action_count"] == 0
    assert attempt["accounted_credit_cost"] == "50"
    assert attempt["provider_billing_outcome"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_provider_progress_before_late_gate_is_observed_without_approval(
    tmp_path: Path,
) -> None:
    port = FakeGenerationPort()
    request, ledger, _journal, executor = make_executor(tmp_path, port)

    async def late_gate_after_progress(
        _baseline: FlowSubmissionBaseline, *, observation_deadline: float
    ) -> FlowConfirmationGate:
        del observation_deadline
        return replace(port.gate, progress_before_confirmation=True)

    port.await_confirmation = late_gate_after_progress  # type: ignore[method-assign]
    port.evidence = replace(port.evidence, attributed_confirmation_id=None)

    result = await executor.execute(request)

    assert result.submission_state == "GENERATION_SUCCEEDED"
    assert port.calls.count("generate") == 1
    assert port.calls.count("approve") == 0
    assert ledger.attempts()[0]["approval_action_count"] == 0


@pytest.mark.asyncio
async def test_retry_baseline_unions_live_and_historical_provider_identities(
    tmp_path: Path,
) -> None:
    project_id = "34e60953-e534-46e9-ac65-993893e61065"
    historical_workflow = "aaaaaaaa-0000-0000-0000-000000000001"
    historical_media = "bbbbbbbb-0000-0000-0000-000000000002"
    historical_media_list = "cccccccc-0000-0000-0000-000000000003"
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    predecessor_id = "D2_history-run-0001"
    predecessor = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), predecessor_id)
    ledger.create_attempt(predecessor)
    ledger.capture_baseline(
        predecessor_id,
        FlowSubmissionBaseline(project_id, frozenset(), frozenset(), 0, frozenset(), "0:"),
    )
    ledger.mark_generate_action(predecessor_id)
    ledger.observe_confirmation(
        predecessor_id,
        confirmation_id="history-confirmation",
        video_count=1,
        credit_cost=10,
        visible_balance=None,
    )
    ledger.approve_gate(
        predecessor_id,
        video_count=1,
        credit_cost=10,
        confirmation_id="history-confirmation",
    )
    ledger.record_state(
        predecessor_id,
        submission_state="UNEXPECTED_MULTI_OUTPUT",
        terminal_state="UNEXPECTED_MULTI_OUTPUT",
        provider_execution_id=historical_workflow,
        provider_media_id=historical_media,
        provider_media_ids=(historical_media_list,),
    )

    request = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-retry-0002")
    authorization = D2RetryAuthorization(predecessor_id, "UNEXPECTED_MULTI_OUTPUT")
    port = FakeGenerationPort()
    port.baseline = FlowSubmissionBaseline(
        project_id,
        frozenset({"live-media"}),
        frozenset({"live-execution"}),
        0,
        frozenset(),
        "0:",
    )
    port.evidence = replace(
        port.evidence,
        d2_run_id=request.d2_run_id,
        attributed_confirmation_id=port.gate.confirmation_id,
    )
    observed_baselines: list[FlowSubmissionBaseline] = []
    observe_attempt = port.observe_attempt

    async def capture_observed_baseline(
        baseline: FlowSubmissionBaseline,
        confirmation_id: str | None,
        *,
        observation_deadline: float,
    ) -> PhaseGAttemptEvidence:
        observed_baselines.append(baseline)
        return await observe_attempt(
            baseline, confirmation_id, observation_deadline=observation_deadline
        )

    port.observe_attempt = capture_observed_baseline  # type: ignore[method-assign]
    journal = AgentSettingsRecoveryJournal(tmp_path / "runtime", "PVA_0001", project_id)
    executor = GoogleFlowAgentOnlyGenerationExecutor(port, ledger, journal)

    await executor.execute(request, retry_authorization=authorization)

    assert len(observed_baselines) == 1
    assert observed_baselines[0].media_ids == frozenset(
        {"live-media", historical_media, historical_media_list}
    )
    assert observed_baselines[0].execution_ids == frozenset({"live-execution", historical_workflow})
    retry = next(row for row in ledger.attempts() if row["d2_run_id"] == request.d2_run_id)
    assert retry["baseline_media_ids"] == [historical_media, historical_media_list, "live-media"]
    assert retry["baseline_execution_ids"] == [historical_workflow, "live-execution"]


def test_ledger_rejects_over_budget_and_unexpected_state_transitions(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    ledger.create_attempt(phase_g_request(tmp_path))
    _capture_ledger_baseline(ledger)
    ledger.mark_generate_action("D2_test-run-0001")
    _observe_ledger_confirmation(ledger, credit_cost=51)
    with pytest.raises(PhaseGError) as budget:
        ledger.approve_gate(
            "D2_test-run-0001", video_count=1, credit_cost=51, confirmation_id="new"
        )
    assert budget.value.code == "DAILY_FREE_CREDIT_BUDGET_EXHAUSTED"
    with pytest.raises(PhaseGError) as state:
        ledger.record_state("D2_test-run-0001", submission_state="RETRY")
    assert state.value.code == "PHASE_G_STATE_INVALID"


def test_ledger_rejects_wrong_project_evidence(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    ledger.create_attempt(phase_g_request(tmp_path))
    _capture_ledger_baseline(ledger)
    ledger.mark_generate_action("D2_test-run-0001")
    with pytest.raises(PhaseGError) as mismatch:
        ledger.record_state(
            "D2_test-run-0001",
            submission_state="UNKNOWN_SUBMISSION_STATE",
            provider_project_id="another-project",
        )
    assert mismatch.value.code == "PHASE_G_PROJECT_MISMATCH"


class FakeGenerationPort:
    def __init__(self) -> None:
        self.settings: dict[str, object] = {
            "model_id": "MDL_0001",
            "generation_mode": "TEXT_TO_VIDEO",
            "aspect_ratio": "16:9",
            "output_count": 1,
            "confirm_before_generating": "Always",
            "resolution": "720p",
        }
        self.original = dict(self.settings)
        self.baseline = FlowSubmissionBaseline(
            "34e60953-e534-46e9-ac65-993893e61065",
            frozenset({"old-media"}),
            frozenset(),
            0,
            frozenset({"old-confirmation"}),
            "turn-before",
        )
        self.gate = FlowConfirmationGate(
            "confirmation-new",
            "34e60953-e534-46e9-ac65-993893e61065",
            1,
            Decimal(10),
            Decimal(50),
        )
        self.evidence = PhaseGAttemptEvidence(
            "D2_test-run-0001",
            "GENERATION_SUCCEEDED",
            provider_execution_id="execution-new",
            provider_media_id="media-new",
            provider_project_id="34e60953-e534-46e9-ac65-993893e61065",
            terminal_state="GENERATION_SUCCEEDED",
            accounted_credit_cost=Decimal(10),
            attributed_confirmation_id="confirmation-new",
        )
        self.calls: list[str] = []
        self.raise_after_click = False
        self.pending_count = 0
        self.fail_restore = False
        self.prompt = ""
        self.raise_waf_on_observe = False
        self.raise_submit_refusal = False
        self.submit_evidence = PhaseGSubmitTransportEvidence(
            generate_click_dispatched=True,
            provider_submit_rpc_observed=True,
            provider_submit_rpc_id="YhhmEf",
            provider_submit_observed_at="2026-10-23T00:00:00.000Z",
            provider_submit_http_method="POST",
            provider_submit_project_id="34e60953-e534-46e9-ac65-993893e61065",
            provider_submit_request_count=1,
            provider_submit_dispatch_window_completed=True,
            provider_submit_request_failed=False,
            provider_submit_response_observed=True,
            provider_submit_response_status=200,
            provider_submit_generation_record_observed=True,
            provider_submit_refusal_detected=False,
        )

    async def bind_request(self, request: PhaseGExecutionRequest) -> None:
        self.calls.append("bind")
        assert request.d2_run_id.startswith(("D2_", "D6_"))

    async def snapshot_settings(self) -> Mapping[str, object]:
        self.calls.append("snapshot")
        return dict(self.settings)

    async def apply_settings(self, settings: Mapping[str, object]) -> Mapping[str, object]:
        self.calls.append("apply")
        self.settings.update(settings)
        return dict(self.settings)

    async def read_settings(self) -> Mapping[str, object]:
        self.calls.append("read")
        return dict(self.settings)

    async def restore_settings(self, settings: Mapping[str, object]) -> None:
        self.calls.append("restore")
        if self.fail_restore:
            raise OSError("simulated restore failure")
        self.settings.update(settings)

    async def fill_prompt(self, prompt: str, negative_prompt: str) -> None:
        self.calls.append("prompt")
        self.prompt = prompt
        assert (
            prompt
            == "Make me a 8 second video of A red kite over the ocean. in a 9:16 aspect ratio."
        )
        assert negative_prompt == ""

    async def read_prompt(self) -> str | None:
        self.calls.append("read_prompt")
        return self.prompt

    async def capture_submission_baseline(self) -> FlowSubmissionBaseline:
        self.calls.append("baseline")
        return replace(self.baseline, pending_count=self.pending_count)

    async def arm_submit_request_observer(self) -> None:
        self.calls.append("arm_submit_observer")

    async def click_generate_once(self) -> None:
        assert self.calls[-1] == "arm_submit_observer"
        self.calls.append("generate")
        if self.raise_after_click:
            raise TimeoutError("ambiguous post-click timeout")

    async def await_submit_dispatch(self) -> PhaseGSubmitTransportEvidence:
        self.calls.append("await_submit_dispatch")
        return self.submit_evidence

    def read_submit_transport_evidence(self) -> PhaseGSubmitTransportEvidence:
        return self.submit_evidence

    def disarm_submit_request_observer(self) -> None:
        self.calls.append("disarm_submit_observer")

    async def await_confirmation(
        self, baseline: FlowSubmissionBaseline, *, observation_deadline: float
    ) -> FlowConfirmationGate | None:
        self.calls.append("gate")
        if self.raise_submit_refusal:
            raise PhaseGError(
                "PHASE_G_PROVIDER_SUBMIT_REFUSED", "synthetic correlated submit RPC error"
            )
        return self.gate

    async def approve_confirmation(self, confirmation_id: str) -> None:
        self.calls.append("approve")
        assert confirmation_id == self.gate.confirmation_id

    async def observe_attempt(
        self,
        baseline: FlowSubmissionBaseline,
        confirmation_id: str | None,
        *,
        observation_deadline: float,
    ) -> PhaseGAttemptEvidence:
        self.calls.append("observe")
        if self.raise_waf_on_observe:
            raise WafRejectionError("Flow unusual activity", status=403)
        return self.evidence


def make_executor(tmp_path: Path, port: FakeGenerationPort):
    request = phase_g_request(tmp_path)
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    journal = AgentSettingsRecoveryJournal(
        tmp_path / "runtime", "PVA_0001", request.provider_project_id
    )
    return request, ledger, journal, GoogleFlowAgentOnlyGenerationExecutor(port, ledger, journal)


@pytest.mark.asyncio
async def test_unconsumed_legacy_replacement_stops_before_ui_or_credit_mutation(
    tmp_path: Path,
) -> None:
    port = FakeGenerationPort()
    base = request_for(tmp_path)
    context = ProductionExecutionContext(
        environment="PROD",
        provider_id="PRV_0001",
        provider_account_id="PVA_0001",
        provider_project_id=base.provider_project_id,
        auth_profile="GFLOW_PROD_AUTH",
        scene_id="SCN_000001",
        generation_profile_id="GPR_0001",
        scene_prompt_id="SPV_000002",
        attempt_id="D2_replacement-0001",
        credit_budget_context=CreditBudgetContext(provider_remaining_free=Decimal("50")),
        production_job_id="PRD_000002",
        scene_job_id="SJB_000002",
        legacy_replacement_authorization_id="LRA_" + "A" * 24,
        legacy_replacement_parent_attempt_id="D2_parent-00000001",
        legacy_replacement_quote_credits=Decimal("10"),
        legacy_replacement_quote_input_digest="d" * 64,
        legacy_replacement_quote_source_version="PCT_000001:verified:source-hash",
    )
    request = PhaseGExecutionRequest.from_presubmit(
        base, context.attempt_id, execution_context=context
    )
    ledger = D2DailyCreditLedger(tmp_path / "production-runtime")
    journal = AgentSettingsRecoveryJournal(
        tmp_path / "runtime", "PVA_0001", request.provider_project_id
    )
    guard = ProductionCreditGuard(tmp_path / "runtime")

    with pytest.raises(PhaseGError, match="consumed replacement") as refused:
        await GoogleFlowAgentOnlyGenerationExecutor(
            port, ledger, journal, production_credit_guard=guard
        ).execute(request)

    assert refused.value.code == "PHASE_G_LEGACY_REPLACEMENT_AUTH_INVALID"
    assert port.calls == []
    assert ledger.attempts() == ()
    assert not guard.path.exists()


@pytest.mark.asyncio
async def test_phase_g_executes_single_approved_gate_and_restores_original_settings(
    tmp_path: Path,
) -> None:
    port = FakeGenerationPort()
    request, ledger, journal, executor = make_executor(tmp_path, port)
    result = await executor.execute(request)
    assert result.submission_state == "GENERATION_SUCCEEDED"
    assert result.provider_media_id == "media-new"
    assert port.calls.count("generate") == 1
    assert port.calls.count("approve") == 1
    assert port.calls.index("snapshot") < port.calls.index("apply")
    assert port.calls.index("baseline") < port.calls.index("generate")
    assert port.settings == port.original
    assert journal.read().state == "CLEAN"  # type: ignore[union-attr]
    assert ledger.accounted_today() == Decimal(10)
    attempt = ledger.attempts()[0]
    assert attempt["approved_prompt_sha256"] == request.prompt_sha256
    assert attempt["provider_directive_sha256"] == hashlib.sha256(port.prompt.encode()).hexdigest()


@pytest.mark.asyncio
async def test_phase_g_journals_and_restores_semantic_settings_only(tmp_path: Path) -> None:
    port = FakeGenerationPort()
    port.settings.update({"model_name": "Veo 3.1 Lite", "model_priority": "STANDARD"})
    port.original = dict(port.settings)

    async def restore_semantic_settings(settings: Mapping[str, object]) -> None:
        port.calls.append("restore")
        if port.fail_restore:
            raise OSError("simulated restore failure")
        port.settings.update(settings)

    port.restore_settings = restore_semantic_settings  # type: ignore[method-assign]
    request, ledger, journal, executor = make_executor(tmp_path, port)

    result = await executor.execute(request)

    assert result.submission_state == "GENERATION_SUCCEEDED"
    assert journal.read().original_settings == {
        "model_id": "MDL_0001",
        "generation_mode": "TEXT_TO_VIDEO",
        "aspect_ratio": "16:9",
        "output_count": 1,
        "confirm_before_generating": "Always",
    }
    assert port.settings == port.original


@pytest.mark.asyncio
async def test_submit_rpc_refusal_keeps_attempt_unknown_and_budget_reserved(
    tmp_path: Path,
) -> None:
    port = FakeGenerationPort()
    port.raise_submit_refusal = True
    port.submit_evidence = replace(
        port.submit_evidence,
        provider_submit_response_observed=True,
        provider_submit_response_status=200,
        provider_submit_generation_record_observed=False,
        provider_submit_refusal_detected=True,
        provider_submit_refusal_code="TEST_ONLY_REASON_TOKEN",
    )
    request, ledger, _journal, executor = make_executor(tmp_path, port)

    with pytest.raises(PhaseGError, match="synthetic correlated submit RPC error"):
        await executor.execute(request)

    attempt = ledger.attempts()[0]
    assert attempt["submission_state"] == "UNKNOWN_SUBMISSION_STATE"
    assert attempt["generate_action_count"] == 1
    assert attempt["approval_action_count"] == 0
    assert attempt["provider_submit_refusal_detected"] is True
    assert port.calls.count("generate") == 1
    assert "approve" not in port.calls
    assert ledger.accounted_today() == Decimal("50")


@pytest.mark.asyncio
async def test_phase_g_uses_shared_production_credit_guard_before_confirmation(
    tmp_path: Path,
) -> None:
    port = FakeGenerationPort()
    base = request_for(tmp_path)
    context = ProductionExecutionContext(
        "TEST",
        "PRV_0001",
        "PVA_0001",
        "34e60953-e534-46e9-ac65-993893e61065",
        "GFLOW_V2_TEST_AUTH",
        "SCN_000001",
        "GPR_0001",
        "SPV_000002",
        "D2_prod-run-0001",
        CreditBudgetContext(provider_remaining_free=Decimal("50")),
    )
    request = PhaseGExecutionRequest.from_presubmit(
        base, context.attempt_id, execution_context=context
    )
    port.evidence = replace(port.evidence, d2_run_id=context.attempt_id)
    ledger = D2DailyCreditLedger(tmp_path / "production-runtime")
    journal = AgentSettingsRecoveryJournal(
        tmp_path / "runtime", "PVA_0001", request.provider_project_id
    )
    guard = ProductionCreditGuard(tmp_path / "runtime")
    result = await GoogleFlowAgentOnlyGenerationExecutor(
        port, ledger, journal, production_credit_guard=guard
    ).execute(request)

    assert result.submission_state == "GENERATION_SUCCEEDED"
    assert port.calls.count("generate") == port.calls.count("approve") == 1
    saved = guard._read()["attempts"][0]  # noqa: SLF001 - verifies the shared durable contract
    assert saved["environment"] == "TEST"
    assert saved["state"] == "SUCCESS"
    assert saved["generate_click_count"] == 1
    assert saved["reserved_credits"] == "10"


@pytest.mark.asyncio
async def test_phase_g_credit_balance_failure_does_not_approve_or_reserve(
    tmp_path: Path,
) -> None:
    port = FakeGenerationPort()
    base = request_for(tmp_path)
    context = ProductionExecutionContext(
        "TEST",
        "PRV_0001",
        "PVA_0001",
        "34e60953-e534-46e9-ac65-993893e61065",
        "GFLOW_V2_TEST_AUTH",
        "SCN_000001",
        "GPR_0001",
        "SPV_000002",
        "D2_prod-run-0002",
        CreditBudgetContext(provider_remaining_free=Decimal("5")),
    )
    request = PhaseGExecutionRequest.from_presubmit(
        base, context.attempt_id, execution_context=context
    )
    port.evidence = replace(port.evidence, d2_run_id=context.attempt_id)
    ledger = D2DailyCreditLedger(tmp_path / "production-runtime")
    journal = AgentSettingsRecoveryJournal(
        tmp_path / "runtime", "PVA_0001", request.provider_project_id
    )
    guard = ProductionCreditGuard(tmp_path / "runtime")

    with pytest.raises(PhaseGError) as refused:
        await GoogleFlowAgentOnlyGenerationExecutor(
            port, ledger, journal, production_credit_guard=guard
        ).execute(request)

    assert refused.value.code == "PHASE_G_CREDIT_GUARD_HOLD"
    assert port.calls.count("approve") == 0
    assert ledger.accounted_today() == Decimal(0)
    assert guard._read()["attempts"][0]["reserved_credits"] == "0"  # noqa: SLF001


@pytest.mark.asyncio
async def test_phase_g_ambiguous_post_click_timeout_is_non_retryable_and_reserved(
    tmp_path: Path,
) -> None:
    port = FakeGenerationPort()
    port.raise_after_click = True
    request, ledger, journal, executor = make_executor(tmp_path, port)
    with pytest.raises(TimeoutError):
        await executor.execute(request)
    assert port.calls.count("generate") == 1
    assert port.calls.count("approve") == 0
    assert ledger.accounted_today() == Decimal(50)
    assert ledger.attempts()[0]["submission_state"] == "UNKNOWN_SUBMISSION_STATE"
    assert port.settings == port.original
    assert journal.read().state == "CLEAN"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_no_submit_rpc_observed_stops_early_without_retry_or_credit_release(
    tmp_path: Path,
) -> None:
    port = FakeGenerationPort()
    port.submit_evidence = PhaseGSubmitTransportEvidence(
        generate_click_dispatched=True,
        provider_submit_rpc_observed=False,
        provider_submit_request_count=0,
        provider_submit_dispatch_window_completed=True,
        provider_submit_response_observed=False,
    )
    request, ledger, _journal, executor = make_executor(tmp_path, port)

    with pytest.raises(PhaseGError) as error:
        await executor.execute(request)

    attempt = ledger.attempts()[0]
    assert error.value.code == "PHASE_G_SUBMIT_RPC_NOT_OBSERVED"
    assert port.calls.count("generate") == 1
    assert port.calls.count("approve") == 0
    assert attempt["generate_action_count"] == 1
    assert attempt["submission_state"] == "UNKNOWN_SUBMISSION_STATE"
    assert attempt["accounted_credit_cost"] == "50"
    assert attempt.get("provider_outcome", "UNKNOWN") == "UNKNOWN"
    assert attempt["provider_billing_outcome"] == "UNKNOWN"
    assert attempt["provider_submit_transport_classification"] == "SUBMIT_RPC_NOT_OBSERVED"


@pytest.mark.asyncio
async def test_agent_only_turn_dispatch_does_not_require_yhhmef(tmp_path: Path) -> None:
    port = FakeGenerationPort()
    port.submit_evidence = PhaseGSubmitTransportEvidence(
        generate_click_dispatched=True,
        provider_submit_rpc_observed=False,
        provider_submit_request_count=0,
        provider_submit_dispatch_window_completed=True,
        provider_submit_response_observed=False,
        submission_surface="AGENT_ONLY",
        agent_turn_dispatched=True,
    )
    request, ledger, _journal, executor = make_executor(tmp_path, port)

    result = await executor.execute(request)

    assert result.submission_state == "GENERATION_SUCCEEDED"
    attempt = ledger.attempts()[0]
    assert attempt["submission_surface"] == "AGENT_ONLY"
    assert attempt["agent_turn_dispatched"] is True
    assert attempt["provider_submit_rpc_id"] is None
    assert attempt["provider_submit_transport_classification"] == "AGENT_TURN_DISPATCHED"


@pytest.mark.asyncio
async def test_agent_only_missing_turn_fails_closed_without_retry(tmp_path: Path) -> None:
    port = FakeGenerationPort()
    port.submit_evidence = PhaseGSubmitTransportEvidence(
        generate_click_dispatched=True,
        provider_submit_rpc_observed=False,
        provider_submit_request_count=0,
        provider_submit_dispatch_window_completed=True,
        provider_submit_response_observed=False,
        submission_surface="AGENT_ONLY",
        agent_turn_dispatched=False,
    )
    request, ledger, _journal, executor = make_executor(tmp_path, port)

    with pytest.raises(PhaseGError) as error:
        await executor.execute(request)

    assert error.value.code == "PHASE_G_AGENT_TURN_NOT_DISPATCHED"
    assert port.calls.count("generate") == 1
    assert port.calls.count("approve") == 0
    assert ledger.attempts()[0]["submission_state"] == "UNKNOWN_SUBMISSION_STATE"


@pytest.mark.asyncio
async def test_duplicate_submit_rpcs_after_one_click_fail_closed(tmp_path: Path) -> None:
    port = FakeGenerationPort()
    port.submit_evidence = replace(port.submit_evidence, provider_submit_request_count=2)
    request, ledger, _journal, executor = make_executor(tmp_path, port)

    with pytest.raises(PhaseGError) as error:
        await executor.execute(request)

    attempt = ledger.attempts()[0]
    assert error.value.code == "PHASE_G_DUPLICATE_SUBMIT_RPC"
    assert port.calls.count("generate") == 1
    assert port.calls.count("approve") == 0
    assert attempt["provider_submit_transport_classification"] == "DUPLICATE_SUBMIT_RPC"
    assert attempt["submission_state"] == "UNKNOWN_SUBMISSION_STATE"
    assert attempt["accounted_credit_cost"] == "50"


@pytest.mark.asyncio
async def test_submit_response_without_generation_record_cannot_be_success(
    tmp_path: Path,
) -> None:
    port = FakeGenerationPort()
    port.submit_evidence = replace(
        port.submit_evidence,
        provider_submit_generation_record_observed=False,
    )
    request, ledger, _journal, executor = make_executor(tmp_path, port)

    with pytest.raises(PhaseGError) as error:
        await executor.execute(request)

    attempt = ledger.attempts()[0]
    assert error.value.code == "PHASE_G_SUBMIT_RESPONSE_NO_GENERATION_RECORD"
    assert attempt["submission_state"] == "UNKNOWN_SUBMISSION_STATE"
    assert attempt["provider_submit_transport_classification"] == (
        "SUBMIT_RESPONSE_WITHOUT_GENERATION_RECORD"
    )
    assert attempt["accounted_credit_cost"] == "10"
    assert port.calls.count("generate") == 1
    assert port.calls.count("approve") == 1


@pytest.mark.asyncio
async def test_phase_g_rejects_pending_baseline_without_generate(tmp_path: Path) -> None:
    port = FakeGenerationPort()
    port.pending_count = 1
    request, ledger, journal, executor = make_executor(tmp_path, port)
    with pytest.raises(PhaseGError) as active:
        await executor.execute(request)
    assert active.value.code == "PHASE_G_UNRELATED_GENERATION_ACTIVE"
    assert port.calls.count("generate") == 0
    assert ledger.attempts()[0]["generate_action_count"] == 0
    assert port.settings == port.original
    assert journal.read().state == "CLEAN"  # type: ignore[union-attr]


def test_confirmation_quote_parser_fails_closed_on_ambiguous_numbers() -> None:
    assert parse_confirmation_quote("Generate 1 video for 10 credits") == (
        1,
        Decimal("10"),
    )
    assert parse_confirmation_quote("Generate 1 video for 1.5 credits") == (
        1,
        Decimal("1.5"),
    )
    assert parse_confirmation_quote("Generate 1 video for 1,000 credits") == (None, None)
    assert parse_confirmation_quote("Generate 1 video for 10 or 20 credits") == (None, None)


@pytest.mark.asyncio
async def test_phase_g_restore_failure_leaves_journal_dirty_and_blocks_next_run(
    tmp_path: Path,
) -> None:
    port = FakeGenerationPort()
    port.fail_restore = True
    request, ledger, journal, executor = make_executor(tmp_path, port)
    with pytest.raises(OSError):
        await executor.execute(request)
    assert journal.read().state == "RESTORING"  # type: ignore[union-attr]
    with pytest.raises(Exception, match="recovery is required"):
        journal.require_clean_or_missing()


@pytest.mark.asyncio
async def test_phase_g_refuses_non_one_video_gate_without_approval_or_charge(
    tmp_path: Path,
) -> None:
    port = FakeGenerationPort()
    port.gate = replace(port.gate, video_count=2)
    request, ledger, journal, executor = make_executor(tmp_path, port)
    with pytest.raises(PhaseGError) as count_error:
        await executor.execute(request)
    assert count_error.value.code == "PHASE_G_VIDEO_COUNT_UNPROVEN"
    assert port.calls.count("generate") == 1
    assert port.calls.count("approve") == 0
    assert ledger.accounted_today() == Decimal(0)
    assert ledger.attempts()[0]["submission_state"] == "RECONCILED_NO_CHARGE"
    assert journal.read().state == "CLEAN"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_phase_g_waf_is_terminal_and_prevents_another_attempt(tmp_path: Path) -> None:
    port = FakeGenerationPort()
    port.evidence = replace(
        port.evidence,
        submission_state="WAF_REJECTED",
        terminal_state="WAF_REJECTED",
    )
    request, ledger, journal, executor = make_executor(tmp_path, port)
    with pytest.raises(PhaseGError) as refusal:
        await executor.execute(request)
    assert refusal.value.code == "WAF_REJECTED"
    assert port.calls.count("generate") == 1
    assert ledger.attempts()[0]["submission_state"] == "WAF_REJECTED"
    second = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-run-0002")
    with pytest.raises(PhaseGError) as stop:
        ledger.create_attempt(second)
    assert stop.value.code == "WAF_REJECTED"
    assert journal.read().state == "CLEAN"  # type: ignore[union-attr]


def test_phase_g_allows_only_one_generate_action_across_run_ids(tmp_path: Path) -> None:
    first_request = phase_g_request(tmp_path)
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    ledger.create_attempt(first_request)
    _capture_ledger_baseline(ledger)
    ledger.mark_generate_action(first_request.d2_run_id)

    second_request = PhaseGExecutionRequest.from_presubmit(
        request_for(tmp_path), "D2_test-run-0002"
    )
    with pytest.raises(PhaseGError) as stopped:
        ledger.create_attempt(second_request)
    assert stopped.value.code == "PHASE_G_PAID_ATTEMPT_ALREADY_STARTED"


@pytest.mark.parametrize("state", ["GENERATION_SUCCEEDED", "PERSISTENCE_FAILED"])
def test_artifact_recovery_states_still_block_a_new_attempt(tmp_path: Path, state: str) -> None:
    request = phase_g_request(tmp_path)
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    ledger.create_attempt(request)
    _capture_ledger_baseline(ledger)
    ledger.mark_generate_action(request.d2_run_id)
    ledger.record_state(request.d2_run_id, submission_state="SUBMISSION_ACKNOWLEDGED")
    ledger.record_state(
        request.d2_run_id,
        submission_state=state,
        terminal_state="PERSISTENCE_FAILED" if state == "PERSISTENCE_FAILED" else None,
    )
    next_request = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-run-0002")
    with pytest.raises(PhaseGError) as blocked:
        ledger.create_attempt(next_request)
    assert blocked.value.code == "PHASE_G_ARTIFACT_RECOVERY_REQUIRED"


def _retry_fixture(
    tmp_path: Path,
    *,
    state: str = "UNEXPECTED_MULTI_OUTPUT",
    terminal_state: str | None = None,
    accounted_credit_cost: int = 10,
) -> tuple[D2DailyCreditLedger, PhaseGExecutionRequest, D2RetryAuthorization]:
    predecessor_id = "D2_2aebc5af59bd423b"
    predecessor_request = PhaseGExecutionRequest.from_presubmit(
        request_for(tmp_path), predecessor_id
    )
    ledger = D2DailyCreditLedger(tmp_path / "retry-runtime")
    ledger.create_attempt(predecessor_request)
    ledger.capture_baseline(
        predecessor_id,
        FlowSubmissionBaseline(
            "34e60953-e534-46e9-ac65-993893e61065",
            frozenset(),
            frozenset(),
            0,
            frozenset(),
            "turn-before",
        ),
    )
    ledger.mark_generate_action(predecessor_id)
    if state == "D2_SUCCEEDED":
        ledger.observe_confirmation(
            predecessor_id,
            confirmation_id="retry-fixture-confirmation",
            video_count=1,
            credit_cost=10,
            visible_balance=None,
        )
        ledger.approve_gate(
            predecessor_id,
            video_count=1,
            credit_cost=10,
            confirmation_id="retry-fixture-confirmation",
        )
        ledger.record_state(
            predecessor_id,
            submission_state="GENERATION_SUCCEEDED",
            terminal_state="GENERATION_SUCCEEDED",
            provider_media_id="media-success",
        )
        ledger.record_state(
            predecessor_id,
            submission_state="D2_SUCCEEDED",
            terminal_state="D2_SUCCEEDED",
        )
    else:
        if state in {"UNEXPECTED_MULTI_OUTPUT", "GENERATION_FAILED"}:
            ledger.observe_confirmation(
                predecessor_id,
                confirmation_id="retry-fixture-confirmation",
                video_count=1,
                credit_cost=accounted_credit_cost,
                visible_balance=None,
            )
            ledger.approve_gate(
                predecessor_id,
                video_count=1,
                credit_cost=accounted_credit_cost,
                confirmation_id="retry-fixture-confirmation",
            )
        ledger.record_state(
            predecessor_id,
            submission_state=state,
            terminal_state=terminal_state if terminal_state is not None else state,
            accounted_credit_cost=(
                accounted_credit_cost if state == "UNEXPECTED_MULTI_OUTPUT" else None
            ),
        )
    request = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-retry-0002")
    authorization = D2RetryAuthorization(predecessor_id, "UNEXPECTED_MULTI_OUTPUT")
    return ledger, request, authorization


def test_phase_g_explicit_retry_is_default_deny_and_exactly_bound(tmp_path: Path) -> None:
    ledger, request, authorization = _retry_fixture(tmp_path)
    with pytest.raises(PhaseGError) as denied:
        ledger.create_attempt(request)
    assert denied.value.code == "PHASE_G_PAID_ATTEMPT_ALREADY_STARTED"

    created = ledger.create_authorized_retry_attempt(request, authorization)
    assert created["submission_state"] == "PREPARED"
    assert created["generate_action_count"] == 0
    retry_record = created["retry_authorization"]
    assert isinstance(retry_record, dict)
    assert retry_record["contract_version"] == 1
    assert retry_record["predecessor_d2_run_id"] == "D2_2aebc5af59bd423b"
    assert retry_record["expected_terminal_state"] == "UNEXPECTED_MULTI_OUTPUT"
    assert isinstance(retry_record["consumed_at"], str)
    assert ledger.attempts()[0]["submission_state"] == "UNEXPECTED_MULTI_OUTPUT"
    assert ledger.attempts()[0]["accounted_credit_cost"] == "10"
    assert ledger.accounted_today() == Decimal("10")


def test_phase_g_retry_rejects_wrong_or_missing_predecessor(tmp_path: Path) -> None:
    ledger, request, authorization = _retry_fixture(tmp_path)
    wrong = D2RetryAuthorization("D2_wrong-predecessor", "UNEXPECTED_MULTI_OUTPUT")
    with pytest.raises(PhaseGError) as missing:
        ledger.create_authorized_retry_attempt(request, wrong)
    assert missing.value.code == "PHASE_G_RETRY_PREDECESSOR_NOT_FOUND"

    wrong_state = D2RetryAuthorization(authorization.predecessor_d2_run_id, "GENERATION_FAILED")
    with pytest.raises(PhaseGError) as state:
        ledger.create_authorized_retry_attempt(request, wrong_state)
    assert state.value.code == "PHASE_G_RETRY_AUTHORIZATION_INVALID"


@pytest.mark.parametrize(
    ("state", "terminal_state", "expected_code"),
    [
        ("UNKNOWN_SUBMISSION_STATE", None, "PHASE_G_ATTEMPT_UNRESOLVED"),
        ("D2_SUCCEEDED", "D2_SUCCEEDED", "PHASE_G_SUCCESS_ALREADY_PROVEN"),
        ("WAF_REJECTED", "WAF_REJECTED", "WAF_REJECTED"),
        ("GENERATION_FAILED", "UNEXPECTED_MULTI_OUTPUT", "PHASE_G_RETRY_PREDECESSOR_MISMATCH"),
    ],
)
def test_phase_g_retry_rejects_unsafe_predecessor_states(
    tmp_path: Path,
    state: str,
    terminal_state: str | None,
    expected_code: str,
) -> None:
    ledger, request, authorization = _retry_fixture(
        tmp_path, state=state, terminal_state=terminal_state
    )
    with pytest.raises(PhaseGError) as rejected:
        ledger.create_authorized_retry_attempt(request, authorization)
    assert rejected.value.code == expected_code


def test_phase_g_retry_authorization_is_one_time_and_run_id_is_unique(tmp_path: Path) -> None:
    ledger, request, authorization = _retry_fixture(tmp_path)
    created = ledger.create_authorized_retry_attempt(request, authorization)
    with pytest.raises(PhaseGError) as duplicate:
        ledger.create_authorized_retry_attempt(request, authorization)
    assert duplicate.value.code == "PHASE_G_DUPLICATE_RUN_ID"

    second_request = PhaseGExecutionRequest.from_presubmit(
        request_for(tmp_path), "D2_test-retry-0003"
    )
    with pytest.raises(PhaseGError) as replay:
        ledger.create_authorized_retry_attempt(second_request, authorization)
    assert replay.value.code == "PHASE_G_RETRY_AUTHORIZATION_CONSUMED"
    assert created["generate_action_count"] == 0


def test_phase_g_retry_authorization_is_consumed_atomically(tmp_path: Path) -> None:
    ledger, request, authorization = _retry_fixture(tmp_path)
    second_request = PhaseGExecutionRequest.from_presubmit(
        request_for(tmp_path), "D2_test-retry-0003"
    )

    def create(candidate: PhaseGExecutionRequest) -> str:
        try:
            ledger.create_authorized_retry_attempt(candidate, authorization)
        except PhaseGError as exc:
            return exc.code
        return "CREATED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, (request, second_request)))

    assert sorted(results) == ["CREATED", "PHASE_G_RETRY_AUTHORIZATION_CONSUMED"]
    assert len(ledger.attempts()) == 2


def test_phase_g_retry_requires_daily_budget_headroom(tmp_path: Path) -> None:
    ledger, request, authorization = _retry_fixture(tmp_path, accounted_credit_cost=50)
    with pytest.raises(PhaseGError) as budget:
        ledger.create_authorized_retry_attempt(request, authorization)
    assert budget.value.code == "DAILY_FREE_CREDIT_BUDGET_EXHAUSTED"
    assert ledger.accounted_today() == Decimal("50")


def _test_budget_session_authorization(
    ledger: D2DailyCreditLedger,
    *,
    starting_balance: int = 40,
    session_id: str = "D2TS_0123456789abcdef",
    **changes: object,
) -> D2TestSessionAuthorization:
    now_bangkok = datetime.now(timezone(timedelta(hours=7)))
    expiry = now_bangkok.replace(hour=23, minute=59, second=59, microsecond=0)
    authorization = D2TestSessionAuthorization(
        session_id=session_id,
        provider_account_id="PVA_0001",
        provider_project_id="34e60953-e534-46e9-ac65-993893e61065",
        auth_profile_key="GFLOW_V2_TEST_AUTH",
        runtime_state_root=ledger.root.parent,
        starting_provider_credits=Decimal(starting_balance),
        authorization_evidence_sha256="d" * 64,
        implementation_commit="a" * 40,
        expires_at=expiry.isoformat(),
    )
    return replace(authorization, **changes)


def _open_test_budget_session(ledger: D2DailyCreditLedger, *, starting_balance: int = 40) -> str:
    authorization = _test_budget_session_authorization(ledger, starting_balance=starting_balance)
    ledger_sha = (
        sha256_file(ledger.path) if ledger.path.exists() else hashlib.sha256(b"").hexdigest()
    )
    ledger.open_test_session(authorization, expected_ledger_sha256=ledger_sha)
    return authorization.session_id


def test_test_session_migrates_v1_without_changing_historical_attempts(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "state")
    ledger.root.mkdir(parents=True)
    historical = {
        "d2_run_id": "D2_history-0001",
        "submission_state": "FORENSICALLY_CLOSED_UNKNOWN",
        "terminal_state": "FORENSICALLY_CLOSED_UNKNOWN",
        "accounted_credit_cost": "50",
        "accounting_date": datetime.now(timezone(timedelta(hours=7))).date().isoformat(),
        "generate_action_count": 1,
    }
    legacy = {"schema_version": 1, "daily_budget": 50, "attempts": [historical]}
    ledger.path.write_text(json.dumps(legacy), encoding="utf-8")
    before = json.loads(ledger.path.read_text(encoding="utf-8"))["attempts"]

    _open_test_budget_session(ledger)

    migrated = json.loads(ledger.path.read_text(encoding="utf-8"))
    assert migrated["schema_version"] == 4
    assert migrated["daily_budget"] == 50
    assert migrated["attempts"] == before
    assert migrated["attempts"][0]["accounted_credit_cost"] == "50"
    assert len(migrated["test_sessions"]) == 1
    assert migrated["legacy_replacement_authorizations"] == []
    assert migrated["legacy_job_linkage_repairs"] == []


def test_test_session_migrates_schema_two_without_changing_existing_rows(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "state")
    ledger.root.mkdir(parents=True)
    old = {
        "schema_version": 2,
        "daily_budget": 50,
        "attempts": [{"d2_run_id": "D2_existing-0001", "accounted_credit_cost": "12"}],
        "test_sessions": [{"session_id": "D2TS_existing", "status": "STOPPED"}],
    }
    ledger.path.write_text(json.dumps(old), encoding="utf-8")

    ledger._mutate(lambda payload: payload)  # noqa: SLF001
    migrated = json.loads(ledger.path.read_text(encoding="utf-8"))

    assert migrated["schema_version"] == 4
    assert migrated["attempts"] == old["attempts"]
    assert migrated["test_sessions"] == old["test_sessions"]
    assert migrated["legacy_replacement_authorizations"] == []
    assert migrated["legacy_job_linkage_repairs"] == []


def test_test_session_authorizes_exact_lane_over_historical_daily_reservation(
    tmp_path: Path,
) -> None:
    request = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-session-0001")
    ledger = D2DailyCreditLedger(request.presubmit_request.runtime_state_root)
    ledger.root.mkdir(parents=True)
    historical = {
        "d2_run_id": "D2_history-0001",
        "submission_state": "FORENSICALLY_CLOSED_UNKNOWN",
        "terminal_state": "FORENSICALLY_CLOSED_UNKNOWN",
        "accounted_credit_cost": "50",
        "accounting_date": datetime.now(timezone(timedelta(hours=7))).date().isoformat(),
        "generate_action_count": 1,
    }
    ledger.path.write_text(
        json.dumps({"schema_version": 1, "daily_budget": 50, "attempts": [historical]}),
        encoding="utf-8",
    )
    session_id = _open_test_budget_session(ledger)

    created = ledger.create_test_session_attempt(
        request,
        session_id=session_id,
        auth_profile_key="GFLOW_V2_TEST_AUTH",
        provider_credits_before=40,
    )
    ledger.capture_baseline(
        request.d2_run_id,
        FlowSubmissionBaseline(
            request.provider_project_id,
            frozenset(),
            frozenset(),
            0,
            frozenset(),
            "0:",
        ),
    )
    ledger.mark_generate_action(request.d2_run_id)
    ledger.observe_confirmation(
        request.d2_run_id,
        confirmation_id="new-confirmation",
        video_count=1,
        credit_cost=10,
        visible_balance=40,
    )
    approved = ledger.approve_gate(
        request.d2_run_id,
        video_count=1,
        credit_cost=10,
        confirmation_id="new-confirmation",
        visible_balance=40,
    )
    assert created["test_session_id"] == session_id
    assert approved["approval_action_count"] == 1
    assert ledger.accounted_today() == Decimal("60")


def test_test_session_rejects_concurrent_attempt_and_unknown_outcome(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "state")
    session_id = _open_test_budget_session(ledger)
    first = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-session-0001")
    second = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-session-0002")
    ledger.create_test_session_attempt(
        first,
        session_id=session_id,
        auth_profile_key="GFLOW_V2_TEST_AUTH",
        provider_credits_before=40,
    )
    ledger.capture_baseline(
        first.d2_run_id,
        FlowSubmissionBaseline(
            first.provider_project_id, frozenset(), frozenset(), 0, frozenset(), "0:"
        ),
    )
    ledger.mark_generate_action(first.d2_run_id)
    with pytest.raises(PhaseGError) as concurrent:
        ledger.create_test_session_attempt(
            second,
            session_id=session_id,
            auth_profile_key="GFLOW_V2_TEST_AUTH",
            provider_credits_before=40,
        )
    assert concurrent.value.code == "PHASE_G_TEST_SESSION_IN_FLIGHT"

    ledger.record_state(first.d2_run_id, submission_state="UNKNOWN_SUBMISSION_STATE")
    stopped = ledger.reconcile_test_session_attempt(
        first.d2_run_id, provider_credits_before=40, provider_credits_after=30
    )
    assert stopped["status"] == "STOPPED"
    assert stopped["stopped_reason"] == "UNKNOWN_OR_NONTERMINAL_OUTCOME"
    assert stopped["reserved_credits"] == "40"
    with pytest.raises(PhaseGError) as subsequent:
        ledger.create_test_session_attempt(
            second,
            session_id=session_id,
            auth_profile_key="GFLOW_V2_TEST_AUTH",
            provider_credits_before=30,
        )
    assert subsequent.value.code == "PHASE_G_TEST_SESSION_STOPPED"


def test_test_session_known_failure_reconciles_and_caps_new_spend(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "state")
    session_id = _open_test_budget_session(ledger)
    first = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-session-0001")
    ledger.create_test_session_attempt(
        first,
        session_id=session_id,
        auth_profile_key="GFLOW_V2_TEST_AUTH",
        provider_credits_before=40,
    )
    ledger.capture_baseline(
        first.d2_run_id,
        FlowSubmissionBaseline(
            first.provider_project_id, frozenset(), frozenset(), 0, frozenset(), "0:"
        ),
    )
    ledger.mark_generate_action(first.d2_run_id)
    ledger.observe_confirmation(
        first.d2_run_id,
        confirmation_id="new-confirmation",
        video_count=1,
        credit_cost=10,
        visible_balance=40,
    )
    ledger.approve_gate(
        first.d2_run_id,
        video_count=1,
        credit_cost=10,
        confirmation_id="new-confirmation",
        visible_balance=40,
    )
    ledger.record_state(
        first.d2_run_id,
        submission_state="GENERATION_FAILED",
        terminal_state="GENERATION_FAILED",
        accounted_credit_cost=10,
    )
    reconciled = ledger.reconcile_test_session_attempt(
        first.d2_run_id, provider_credits_before=40, provider_credits_after=30
    )
    assert reconciled["status"] == "ACTIVE"
    assert reconciled["spent_credits"] == "10"
    assert reconciled["reserved_credits"] == "30"
    assert ledger.attempts()[0]["test_session_reconciliation"] == "RECONCILED"
    second = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-session-0002")
    next_attempt = ledger.create_test_session_attempt(
        second,
        session_id=session_id,
        auth_profile_key="GFLOW_V2_TEST_AUTH",
        provider_credits_before=30,
    )
    assert next_attempt["test_session_id"] == session_id
    assert next_attempt["test_session_credits_before"] == "30"


def test_test_session_concurrent_reservations_are_atomic(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "state")
    session_id = _open_test_budget_session(ledger)
    requests = [
        PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), f"D2_test-session-{index:04d}")
        for index in (1, 2)
    ]

    def reserve(candidate: PhaseGExecutionRequest) -> str:
        try:
            ledger.create_test_session_attempt(
                candidate,
                session_id=session_id,
                auth_profile_key="GFLOW_V2_TEST_AUTH",
                provider_credits_before=40,
            )
        except PhaseGError as exc:
            return exc.code
        return "CREATED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, requests))

    assert results.count("CREATED") == 1
    assert results.count("PHASE_G_TEST_SESSION_IN_FLIGHT") == 1
    session = json.loads(ledger.path.read_text(encoding="utf-8"))["test_sessions"][0]
    assert len(session["attempt_run_ids"]) == 1


def test_test_session_rejects_wrong_profile_and_tampered_authorization(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "state")
    session_id = _open_test_budget_session(ledger)
    request = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-session-0001")
    with pytest.raises(PhaseGError) as wrong_profile:
        ledger.create_test_session_attempt(
            request,
            session_id=session_id,
            auth_profile_key="GFLOW_V2_PROD_AUTH",
            provider_credits_before=40,
        )
    assert wrong_profile.value.code == "PHASE_G_TEST_SESSION_LANE_MISMATCH"

    payload = json.loads(ledger.path.read_text(encoding="utf-8"))
    payload["test_sessions"][0]["authorized_new_credits"] = "400"
    ledger.path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PhaseGError) as tampered:
        ledger.create_test_session_attempt(
            request,
            session_id=session_id,
            auth_profile_key="GFLOW_V2_TEST_AUTH",
            provider_credits_before=40,
        )
    assert tampered.value.code == "PHASE_G_TEST_SESSION_INVALID"
    assert ledger.attempts() == ()


def test_test_session_quote_above_remaining_budget_fails_before_approval(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "state")
    session_id = _open_test_budget_session(ledger)
    request = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-session-0001")
    ledger.create_test_session_attempt(
        request,
        session_id=session_id,
        auth_profile_key="GFLOW_V2_TEST_AUTH",
        provider_credits_before=40,
    )
    ledger.capture_baseline(
        request.d2_run_id,
        FlowSubmissionBaseline(
            request.provider_project_id, frozenset(), frozenset(), 0, frozenset(), "0:"
        ),
    )
    ledger.mark_generate_action(request.d2_run_id)
    ledger.observe_confirmation(
        request.d2_run_id,
        confirmation_id="new-confirmation",
        video_count=1,
        credit_cost=41,
        visible_balance=40,
    )
    with pytest.raises(PhaseGError) as over_budget:
        ledger.approve_gate(
            request.d2_run_id,
            video_count=1,
            credit_cost=41,
            confirmation_id="new-confirmation",
            visible_balance=40,
        )
    assert over_budget.value.code == "PHASE_G_TEST_SESSION_QUOTE_INVALID"
    attempt = ledger.attempts()[0]
    assert attempt["approval_action_count"] == 0
    assert attempt["generate_action_count"] == 1


def test_test_session_quote_above_visible_balance_fails_before_approval(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "state")
    session_id = _open_test_budget_session(ledger)
    request = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-session-0001")
    ledger.create_test_session_attempt(
        request,
        session_id=session_id,
        auth_profile_key="GFLOW_V2_TEST_AUTH",
        provider_credits_before=40,
    )
    ledger.capture_baseline(
        request.d2_run_id,
        FlowSubmissionBaseline(
            request.provider_project_id, frozenset(), frozenset(), 0, frozenset(), "0:"
        ),
    )
    ledger.mark_generate_action(request.d2_run_id)
    ledger.observe_confirmation(
        request.d2_run_id,
        confirmation_id="new-confirmation",
        video_count=1,
        credit_cost=10,
        visible_balance=5,
    )
    with pytest.raises(PhaseGError) as insufficient:
        ledger.approve_gate(
            request.d2_run_id,
            video_count=1,
            credit_cost=10,
            confirmation_id="new-confirmation",
            visible_balance=5,
        )
    assert insufficient.value.code == "PHASE_G_TEST_SESSION_QUOTE_INVALID"
    assert ledger.attempts()[0]["approval_action_count"] == 0


def test_test_session_approval_accepts_missing_visible_balance_from_pre_attempt_budget(
    tmp_path: Path,
) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "state")
    session_id = _open_test_budget_session(ledger, starting_balance=40)
    request = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-session-0001")
    ledger.create_test_session_attempt(
        request,
        session_id=session_id,
        auth_profile_key="GFLOW_V2_TEST_AUTH",
        provider_credits_before=40,
    )
    ledger.capture_baseline(
        request.d2_run_id,
        FlowSubmissionBaseline(
            request.provider_project_id, frozenset(), frozenset(), 0, frozenset(), "0:"
        ),
    )
    ledger.mark_generate_action(request.d2_run_id)
    ledger.observe_confirmation(
        request.d2_run_id,
        confirmation_id="new-confirmation",
        video_count=1,
        credit_cost=10,
        visible_balance=None,
    )

    approved = ledger.approve_gate(
        request.d2_run_id,
        video_count=1,
        credit_cost=10,
        confirmation_id="new-confirmation",
        visible_balance=None,
    )

    assert approved["approval_action_count"] == 1
    assert approved["observed_visible_balance"] is None
    assert approved["quoted_credit_cost"] == "10"


def test_test_session_approval_rejects_visible_balance_mismatch(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "state")
    session_id = _open_test_budget_session(ledger, starting_balance=40)
    request = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-session-0001")
    ledger.create_test_session_attempt(
        request,
        session_id=session_id,
        auth_profile_key="GFLOW_V2_TEST_AUTH",
        provider_credits_before=40,
    )
    ledger.capture_baseline(
        request.d2_run_id,
        FlowSubmissionBaseline(
            request.provider_project_id, frozenset(), frozenset(), 0, frozenset(), "0:"
        ),
    )
    ledger.mark_generate_action(request.d2_run_id)
    ledger.observe_confirmation(
        request.d2_run_id,
        confirmation_id="new-confirmation",
        video_count=1,
        credit_cost=10,
        visible_balance=39,
    )

    with pytest.raises(PhaseGError) as mismatch:
        ledger.approve_gate(
            request.d2_run_id,
            video_count=1,
            credit_cost=10,
            confirmation_id="new-confirmation",
            visible_balance=39,
        )

    assert mismatch.value.code == "PHASE_G_TEST_SESSION_QUOTE_INVALID"
    assert ledger.attempts()[0]["approval_action_count"] == 0


def test_test_session_approval_rejects_quote_above_captured_pre_attempt_balance(
    tmp_path: Path,
) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "state")
    session_id = _open_test_budget_session(ledger, starting_balance=5)
    request = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-session-0001")
    ledger.create_test_session_attempt(
        request,
        session_id=session_id,
        auth_profile_key="GFLOW_V2_TEST_AUTH",
        provider_credits_before=5,
    )
    ledger.capture_baseline(
        request.d2_run_id,
        FlowSubmissionBaseline(
            request.provider_project_id, frozenset(), frozenset(), 0, frozenset(), "0:"
        ),
    )
    ledger.mark_generate_action(request.d2_run_id)
    ledger.observe_confirmation(
        request.d2_run_id,
        confirmation_id="new-confirmation",
        video_count=1,
        credit_cost=10,
        visible_balance=None,
    )

    with pytest.raises(PhaseGError) as insufficient:
        ledger.approve_gate(
            request.d2_run_id,
            video_count=1,
            credit_cost=10,
            confirmation_id="new-confirmation",
            visible_balance=None,
        )

    assert insufficient.value.code == "PHASE_G_TEST_SESSION_QUOTE_INVALID"
    assert ledger.attempts()[0]["approval_action_count"] == 0


def test_unknown_no_charge_requires_evidence_operator(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "state")
    request = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-session-0001")
    ledger.create_attempt(request)
    ledger.capture_baseline(
        request.d2_run_id,
        FlowSubmissionBaseline(
            request.provider_project_id, frozenset(), frozenset(), 0, frozenset(), "0:"
        ),
    )
    ledger.mark_generate_action(request.d2_run_id)
    ledger.record_state(request.d2_run_id, submission_state="UNKNOWN_SUBMISSION_STATE")

    with pytest.raises(PhaseGError) as blocked:
        ledger.record_state(
            request.d2_run_id,
            submission_state="RECONCILED_NO_CHARGE",
            terminal_state="RECONCILED_NO_CHARGE",
            accounted_credit_cost=0,
        )

    assert blocked.value.code == "PHASE_G_TEST_SESSION_OPERATOR_REQUIRED"
    assert ledger.attempts()[0]["submission_state"] == "UNKNOWN_SUBMISSION_STATE"


def _prepare_test_session_unknown(
    tmp_path: Path,
) -> tuple[D2DailyCreditLedger, str, PhaseGExecutionRequest]:
    ledger = D2DailyCreditLedger(tmp_path / "state")
    session_id = _open_test_budget_session(ledger, starting_balance=50)
    request = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-session-0001")
    ledger.create_test_session_attempt(
        request,
        session_id=session_id,
        auth_profile_key="GFLOW_V2_TEST_AUTH",
        provider_credits_before=50,
    )
    ledger.capture_baseline(
        request.d2_run_id,
        FlowSubmissionBaseline(
            request.provider_project_id,
            frozenset({"baseline-media"}),
            frozenset({"baseline-execution"}),
            0,
            frozenset({"baseline-confirmation"}),
            "3:" + "a" * 64,
        ),
    )
    ledger.mark_generate_action(request.d2_run_id)
    ledger.record_submit_transport_evidence(
        request.d2_run_id,
        PhaseGSubmitTransportEvidence(
            generate_click_dispatched=True,
            provider_submit_rpc_observed=False,
            provider_submit_request_count=0,
            provider_submit_dispatch_window_completed=True,
            provider_submit_response_observed=False,
            submission_surface="AGENT_ONLY",
            agent_turn_dispatched=True,
        ),
    )
    ledger.observe_confirmation(
        request.d2_run_id,
        confirmation_id="new-confirmation",
        video_count=1,
        credit_cost=10,
        visible_balance=None,
    )
    ledger.record_state(request.d2_run_id, submission_state="UNKNOWN_SUBMISSION_STATE")
    session = ledger.reconcile_test_session_attempt(
        request.d2_run_id,
        provider_credits_before=50,
        provider_credits_after=50,
    )
    assert session["status"] == "STOPPED"
    return ledger, session_id, request


def _preapproval_no_charge_evidence(
    ledger: D2DailyCreditLedger, run_id: str, *, captured_at: str | None = None
) -> dict[str, object]:
    attempt = next(row for row in ledger.attempts() if row["d2_run_id"] == run_id)
    session_id = str(attempt["test_session_id"])
    evidence: dict[str, object] = {
        "SCHEMA_VERSION": "D2_TEST_SESSION_PREAPPROVAL_NO_CHARGE_V1",
        "D2_RUN_ID": run_id,
        "CANONICAL_LEDGER_SHA256": sha256_file(ledger.path),
        "IMPLEMENTATION_COMMIT": "a" * 40,
        "RUNTIME_STATE_ROOT": str(ledger.root.parent.resolve()),
        "TEST_PROFILE_KEY": "GFLOW_V2_TEST_AUTH",
        "PROVIDER_ACCOUNT_ID": "PVA_0001",
        "PROVIDER_PROJECT_ID": str(attempt["provider_project_id"]),
        "SESSION_ID": session_id,
        "CURRENT_SUBMISSION_STATE": "UNKNOWN_SUBMISSION_STATE",
        "CURRENT_TERMINAL_STATE": None,
        "GENERATE_ACTION_COUNT": 1,
        "APPROVAL_ACTION_COUNT": 0,
        "REQUESTED_OUTPUT_COUNT": 1,
        "AGENT_TURN_DISPATCHED": True,
        "OBSERVED_CONFIRMATION_ID": "new-confirmation",
        "OBSERVED_VIDEO_COUNT": 1,
        "OBSERVED_CREDIT_COST": "10",
        "OBSERVED_VISIBLE_BALANCE": None,
        "PROVIDER_EXECUTION_ID": None,
        "PROVIDER_MEDIA_ID": None,
        "PROVIDER_MEDIA_IDS": [],
        "BASELINE_MEDIA_IDS": ["baseline-media"],
        "CURRENT_PROJECT_MEDIA_IDS": ["baseline-media"],
        "BASELINE_EXECUTION_IDS": ["baseline-execution"],
        "CURRENT_PROJECT_EXECUTION_IDS": ["baseline-execution"],
        "BASELINE_CONFIRMATION_IDS": ["baseline-confirmation"],
        "CURRENT_PROJECT_CONFIRMATION_IDS": ["baseline-confirmation", "new-confirmation"],
        "BASELINE_CHAT_COUNT": 3,
        "CURRENT_PROJECT_CHAT_COUNT": 4,
        "PROVIDER_DIRECTIVE_SHA256": str(attempt["provider_directive_sha256"]),
        "NEW_AGENT_PROMPT_BUBBLE_INDEX": 3,
        "PROVIDER_DIRECTIVE_MATCH_COUNT": 1,
        "CHAT_ENUMERATION_COMPLETE": True,
        "BASELINE_PENDING_COUNT": 0,
        "CURRENT_PROJECT_PENDING_COUNT": 0,
        "NEW_FINISHED_MEDIA_IDS": [],
        "NEW_AGENT_ASSOCIATED_MEDIA_IDS": [],
        "PROJECT_SNAPSHOT_HYDRATED": True,
        "PROJECT_ENUMERATION_COMPLETE": True,
        "PROGRESS_BEFORE_CONFIRMATION": False,
        "PROVIDER_GENERATION_STARTED": False,
        "PROVIDER_REFUSAL": False,
        "WAF_REFUSAL": False,
        "NO_OTHER_ACCOUNT_ACTIVITY": True,
        "CREDITS_BEFORE": "50",
        "CREDITS_AFTER": "50",
        "CAPTURED_AT": captured_at or datetime.now(UTC).isoformat(),
    }
    return evidence


def test_evidence_bound_preapproval_no_charge_reconciles_unknown_attempt(
    tmp_path: Path,
) -> None:
    ledger, session_id, request = _prepare_test_session_unknown(tmp_path)
    evidence = _preapproval_no_charge_evidence(ledger, request.d2_run_id)
    evidence_sha = preapproval_no_charge_evidence_digest(evidence)
    before_attempt = ledger.attempts()[0]
    previous_reconciled_at = before_attempt["test_session_reconciled_at"]

    result = ledger.reconcile_test_session_preapproval_no_charge(
        request.d2_run_id,
        expected_ledger_sha256=sha256_file(ledger.path),
        evidence_sha256=evidence_sha,
        implementation_commit="a" * 40,
        evidence=evidence,
    )

    attempt = ledger.attempts()[0]
    assert attempt["submission_state"] == "RECONCILED_NO_CHARGE"
    assert attempt["terminal_state"] == "RECONCILED_NO_CHARGE"
    assert attempt["provider_billing_outcome"] == "UNKNOWN"
    assert attempt["accounted_credit_cost"] == before_attempt["accounted_credit_cost"]
    assert attempt["observed_visible_balance"] is None
    assert attempt["test_session_previous_reconciled_at"] == previous_reconciled_at
    assert attempt["test_session_no_charge_evidence_sha256"] == evidence_sha
    assert result["status"] == "ACTIVE"
    assert result["spent_credits"] == "0"
    assert result["reserved_credits"] == "40"
    assert result["in_flight_run_id"] is None
    assert result["session_id"] == session_id


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("APPROVAL_ACTION_COUNT", 1),
        ("PROVIDER_EXECUTION_ID", "new-execution"),
        ("PROVIDER_MEDIA_ID", "new-media"),
        ("PROVIDER_MEDIA_IDS", ["new-media"]),
        ("NEW_FINISHED_MEDIA_IDS", ["new-media"]),
        ("NEW_AGENT_ASSOCIATED_MEDIA_IDS", ["new-media"]),
        ("CURRENT_PROJECT_PENDING_COUNT", 1),
        ("CREDITS_AFTER", "49"),
        ("PROGRESS_BEFORE_CONFIRMATION", True),
        ("PROVIDER_GENERATION_STARTED", True),
        ("PROVIDER_REFUSAL", True),
        ("WAF_REFUSAL", True),
        ("NO_OTHER_ACCOUNT_ACTIVITY", False),
        ("PROJECT_ENUMERATION_COMPLETE", False),
        ("CHAT_ENUMERATION_COMPLETE", False),
        ("NEW_AGENT_PROMPT_BUBBLE_INDEX", 2),
        ("PROVIDER_DIRECTIVE_MATCH_COUNT", 2),
        ("CURRENT_PROJECT_CHAT_COUNT", 3),
    ],
)
def test_no_charge_reconciliation_rejects_unsafe_or_incomplete_evidence(
    tmp_path: Path, field: str, value: object
) -> None:
    ledger, _, request = _prepare_test_session_unknown(tmp_path)
    evidence = _preapproval_no_charge_evidence(ledger, request.d2_run_id)
    evidence[field] = value

    with pytest.raises(PhaseGError):
        ledger.reconcile_test_session_preapproval_no_charge(
            request.d2_run_id,
            expected_ledger_sha256=sha256_file(ledger.path),
            evidence_sha256=preapproval_no_charge_evidence_digest(evidence),
            implementation_commit="a" * 40,
            evidence=evidence,
        )

    assert ledger.attempts()[0]["submission_state"] == "UNKNOWN_SUBMISSION_STATE"
    session = json.loads(ledger.path.read_text(encoding="utf-8"))["test_sessions"][0]
    assert session["status"] == "STOPPED"
    assert session["in_flight_run_id"] == request.d2_run_id


def test_no_charge_reconciliation_rejects_stale_ledger_or_evidence_hash(tmp_path: Path) -> None:
    ledger, _, request = _prepare_test_session_unknown(tmp_path)
    evidence = _preapproval_no_charge_evidence(ledger, request.d2_run_id)
    evidence_sha = preapproval_no_charge_evidence_digest(evidence)
    with pytest.raises(PhaseGError):
        ledger.reconcile_test_session_preapproval_no_charge(
            request.d2_run_id,
            expected_ledger_sha256="0" * 64,
            evidence_sha256=evidence_sha,
            implementation_commit="a" * 40,
            evidence=evidence,
        )
    with pytest.raises(PhaseGError):
        ledger.reconcile_test_session_preapproval_no_charge(
            request.d2_run_id,
            expected_ledger_sha256=sha256_file(ledger.path),
            evidence_sha256="0" * 64,
            implementation_commit="a" * 40,
            evidence=evidence,
        )
    assert ledger.attempts()[0]["submission_state"] == "UNKNOWN_SUBMISSION_STATE"


def test_test_session_rejects_prod_execution_context(tmp_path: Path) -> None:
    base = request_for(tmp_path)
    request_id = "D2_test-session-prod-context"
    prod_context = ProductionExecutionContext(
        "PROD",
        "PRV_0001",
        "PVA_0001",
        "34e60953-e534-46e9-ac65-993893e61065",
        "GFLOW_V2_TEST_AUTH",
        "SCN_000001",
        "GPR_0001",
        "SPV_000002",
        request_id,
        CreditBudgetContext(provider_remaining_free=Decimal("40")),
        production_job_id="PRD_000001",
        scene_job_id="SJB_000001",
    )
    request = PhaseGExecutionRequest.from_presubmit(
        base, request_id, execution_context=prod_context
    )
    ledger = D2DailyCreditLedger(base.runtime_state_root)
    session_id = _open_test_budget_session(ledger)

    with pytest.raises(PhaseGError) as rejected:
        ledger.create_test_session_attempt(
            request,
            session_id=session_id,
            auth_profile_key="GFLOW_V2_TEST_AUTH",
            provider_credits_before=40,
        )
    assert rejected.value.code == "PHASE_G_TEST_SESSION_LANE_MISMATCH"
    assert ledger.attempts() == ()


def test_test_session_stops_at_exact_40_credit_cap(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "state")
    session_id = _open_test_budget_session(ledger)
    request = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-session-0001")
    ledger.create_test_session_attempt(
        request,
        session_id=session_id,
        auth_profile_key="GFLOW_V2_TEST_AUTH",
        provider_credits_before=40,
    )
    ledger.capture_baseline(
        request.d2_run_id,
        FlowSubmissionBaseline(
            request.provider_project_id, frozenset(), frozenset(), 0, frozenset(), "0:"
        ),
    )
    ledger.mark_generate_action(request.d2_run_id)
    ledger.observe_confirmation(
        request.d2_run_id,
        confirmation_id="new-confirmation",
        video_count=1,
        credit_cost=40,
        visible_balance=40,
    )
    ledger.approve_gate(
        request.d2_run_id,
        video_count=1,
        credit_cost=40,
        confirmation_id="new-confirmation",
        visible_balance=40,
    )
    ledger.record_state(
        request.d2_run_id,
        submission_state="GENERATION_FAILED",
        terminal_state="GENERATION_FAILED",
        accounted_credit_cost=40,
    )
    session = ledger.reconcile_test_session_attempt(
        request.d2_run_id, provider_credits_before=40, provider_credits_after=0
    )
    assert session["spent_credits"] == "40"
    assert session["reserved_credits"] == "0"
    assert session["status"] == "CREDIT_CAP_EXHAUSTED"
    next_request = PhaseGExecutionRequest.from_presubmit(
        request_for(tmp_path), "D2_test-session-0002"
    )
    with pytest.raises(PhaseGError) as exhausted:
        ledger.create_test_session_attempt(
            next_request,
            session_id=session_id,
            auth_profile_key="GFLOW_V2_TEST_AUTH",
            provider_credits_before=0,
        )
    assert exhausted.value.code == "PHASE_G_TEST_SESSION_STOPPED"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_account_id", "PVA_0002"),
        ("provider_project_id", "other-project"),
        ("auth_profile_key", "GFLOW_V2_PROD_AUTH"),
        ("runtime_state_root", Path(Path.cwd().anchor) / "other" / "state"),
        ("authorization_evidence_sha256", "malformed-evidence"),
    ],
)
def test_test_session_rejects_wrong_authorization_binding(
    tmp_path: Path, field: str, value: object
) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "state")
    authorization = _test_budget_session_authorization(ledger, **{field: value})
    with pytest.raises(PhaseGError) as mismatch:
        ledger.open_test_session(
            authorization, expected_ledger_sha256=hashlib.sha256(b"").hexdigest()
        )
    expected_code = (
        "PHASE_G_TEST_SESSION_ROOT_MISMATCH"
        if field == "runtime_state_root"
        else "PHASE_G_TEST_SESSION_AUTHORIZATION_INVALID"
    )
    assert mismatch.value.code == expected_code
    assert ledger.attempts() == ()


def test_test_session_rejects_expired_authorization_and_replay(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "state")
    now_bangkok = datetime.now(timezone(timedelta(hours=7)))
    expired = now_bangkok - timedelta(days=1)
    authorization = _test_budget_session_authorization(ledger, expires_at=expired.isoformat())
    with pytest.raises(PhaseGError) as stale:
        ledger.open_test_session(
            authorization, expected_ledger_sha256=hashlib.sha256(b"").hexdigest()
        )
    assert stale.value.code == "PHASE_G_TEST_SESSION_EXPIRED"

    _open_test_budget_session(ledger)
    current_sha = sha256_file(ledger.path)
    replay = _test_budget_session_authorization(ledger, session_id="D2TS_fedcba9876543210")
    with pytest.raises(PhaseGError) as reused:
        ledger.open_test_session(replay, expected_ledger_sha256=current_sha)
    assert reused.value.code == "PHASE_G_TEST_SESSION_ALREADY_AUTHORIZED"


def test_test_session_waf_and_success_permanently_stop_new_attempts(tmp_path: Path) -> None:
    for run_id, terminal_state, expected_status in (
        ("D2_test-session-waf", "WAF_REJECTED", "STOPPED"),
        ("D2_test-session-success", "D2_SUCCEEDED", "SUCCEEDED"),
    ):
        case_root = tmp_path / terminal_state
        ledger = D2DailyCreditLedger(case_root / "state")
        session_id = _open_test_budget_session(ledger)
        request = PhaseGExecutionRequest.from_presubmit(request_for(case_root), run_id)
        ledger.create_test_session_attempt(
            request,
            session_id=session_id,
            auth_profile_key="GFLOW_V2_TEST_AUTH",
            provider_credits_before=40,
        )
        ledger.capture_baseline(
            run_id,
            FlowSubmissionBaseline(
                request.provider_project_id, frozenset(), frozenset(), 0, frozenset(), "0:"
            ),
        )
        ledger.mark_generate_action(run_id)
        ledger.record_state(run_id, submission_state="UNKNOWN_SUBMISSION_STATE")
        if terminal_state == "D2_SUCCEEDED":
            ledger.record_state(run_id, submission_state="GENERATION_SUCCEEDED")
        ledger.record_state(run_id, submission_state=terminal_state, terminal_state=terminal_state)
        session = ledger.reconcile_test_session_attempt(
            run_id, provider_credits_before=40, provider_credits_after=40
        )
        assert session["status"] == expected_status
        next_request = PhaseGExecutionRequest.from_presubmit(
            request_for(case_root), f"D2_{terminal_state.lower()}-next"
        )
        with pytest.raises(PhaseGError) as stopped:
            ledger.create_test_session_attempt(
                next_request,
                session_id=session_id,
                auth_profile_key="GFLOW_V2_TEST_AUTH",
                provider_credits_before=40,
            )
        assert stopped.value.code == "PHASE_G_TEST_SESSION_STOPPED"


def test_test_session_rejects_next_attempt_when_prior_media_needs_recovery(
    tmp_path: Path,
) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "state")
    session_id = _open_test_budget_session(ledger)
    first = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-session-0001")
    ledger.create_test_session_attempt(
        first,
        session_id=session_id,
        auth_profile_key="GFLOW_V2_TEST_AUTH",
        provider_credits_before=40,
    )
    ledger.capture_baseline(
        first.d2_run_id,
        FlowSubmissionBaseline(
            first.provider_project_id, frozenset(), frozenset(), 0, frozenset(), "0:"
        ),
    )
    ledger.mark_generate_action(first.d2_run_id)
    ledger.record_state(first.d2_run_id, submission_state="UNKNOWN_SUBMISSION_STATE")
    ledger.record_state(
        first.d2_run_id,
        submission_state="GENERATION_FAILED",
        terminal_state="GENERATION_FAILED",
        provider_media_id="media-needs-recovery",
    )
    session = ledger.reconcile_test_session_attempt(
        first.d2_run_id, provider_credits_before=40, provider_credits_after=40
    )
    assert session["status"] == "ACTIVE"

    second = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-session-0002")
    with pytest.raises(PhaseGError) as recovery:
        ledger.create_test_session_attempt(
            second,
            session_id=session_id,
            auth_profile_key="GFLOW_V2_TEST_AUTH",
            provider_credits_before=40,
        )
    assert recovery.value.code == "PHASE_G_ARTIFACT_RECOVERY_REQUIRED"


def test_full_bangkok_day_budget_blocks_generate_before_action_is_consumed(
    tmp_path: Path,
) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    ledger.create_attempt(phase_g_request(tmp_path))
    _capture_ledger_baseline(ledger)
    today = datetime.now(timezone(timedelta(hours=7))).date().isoformat()
    ledger._mutate(
        lambda payload: _set_attempt_accounting(payload, "D2_test-run-0001", today, "50")
    )
    with pytest.raises(PhaseGError) as blocked:
        ledger.mark_generate_action("D2_test-run-0001")
    assert blocked.value.code == "DAILY_FREE_CREDIT_BUDGET_EXHAUSTED"
    attempt = ledger.attempts()[0]
    assert attempt["generate_action_count"] == 0
    assert attempt["submission_state"] == "PREPARED"


def test_prior_bangkok_day_reservation_does_not_consume_today_budget(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    ledger.create_attempt(phase_g_request(tmp_path))
    _capture_ledger_baseline(ledger)
    today = datetime.now(timezone(timedelta(hours=7))).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    ledger._mutate(
        lambda payload: _set_attempt_accounting(payload, "D2_test-run-0001", yesterday, "50")
    )
    assert ledger.accounted_today() == Decimal(0)
    assert ledger.mark_generate_action("D2_test-run-0001")["generate_action_count"] == 1


def _set_attempt_accounting(
    payload: dict[str, object], run_id: str, date: str, cost: str
) -> dict[str, object]:
    attempts = payload["attempts"]
    assert isinstance(attempts, list)
    attempt = next(row for row in attempts if row["d2_run_id"] == run_id)
    attempt["accounting_date"] = date
    attempt["accounted_credit_cost"] = cost
    return {"d2_run_id": run_id}


@pytest.mark.asyncio
async def test_over_budget_quote_is_not_approved_or_accounted(tmp_path: Path) -> None:
    port = FakeGenerationPort()
    port.gate = replace(port.gate, credit_cost=Decimal(51))
    request, ledger, journal, executor = make_executor(tmp_path, port)
    with pytest.raises(PhaseGError) as budget:
        await executor.execute(request)
    assert budget.value.code == "DAILY_FREE_CREDIT_BUDGET_EXHAUSTED"
    assert port.calls.count("approve") == 0
    assert ledger.accounted_today() == Decimal(0)
    assert ledger.attempts()[0]["submission_state"] == "RECONCILED_NO_CHARGE"
    assert journal.read().state == "CLEAN"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_waf_after_approval_keeps_the_reserved_quote(tmp_path: Path) -> None:
    port = FakeGenerationPort()
    port.raise_waf_on_observe = True
    request, ledger, journal, executor = make_executor(tmp_path, port)
    with pytest.raises(WafRejectionError):
        await executor.execute(request)
    attempt = ledger.attempts()[0]
    assert attempt["submission_state"] == "WAF_REJECTED"
    assert attempt["accounted_credit_cost"] == "10"
    assert ledger.accounted_today() == Decimal("10")
    assert port.calls.count("generate") == 1
    assert port.calls.count("approve") == 1
    assert journal.read().state == "CLEAN"  # type: ignore[union-attr]


def test_d2_success_requires_remote_retrieval_and_matching_sha(tmp_path: Path) -> None:
    request = phase_g_request(tmp_path)
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    ledger.create_attempt(request)
    _capture_ledger_baseline(ledger)
    ledger.mark_generate_action(request.d2_run_id)
    _observe_ledger_confirmation(ledger, confirmation_id="new-confirmation")
    ledger.approve_gate(
        request.d2_run_id,
        video_count=1,
        credit_cost=10,
        confirmation_id="new-confirmation",
    )
    ledger.record_state(
        request.d2_run_id,
        submission_state="GENERATION_SUCCEEDED",
        terminal_state="GENERATION_SUCCEEDED",
        provider_media_id="media-new",
        provider_project_id=request.provider_project_id,
    )
    local_file = request.presubmit_request.temp_root / request.d2_run_id / "clip.mp4"
    local_file.parent.mkdir(parents=True)
    local_file.write_bytes(b"validated-local-mp4")
    local_hash = sha256_file(local_file)
    ledger.record_local_artifact_validated(
        request.d2_run_id,
        path=local_file,
        size_bytes=local_file.stat().st_size,
        sha256=local_hash,
    )
    remote_file = tmp_path / "drive-redownload.mp4"
    remote_file.write_bytes(b"different-local-mp4")
    with pytest.raises(PhaseGError) as mismatch:
        ledger.mark_artifact_verified(
            request.d2_run_id,
            remote_download=remote_file,
            remote_file_id="drive-file-1",
            remote_filename="d2-run.mp4",
        )
    assert mismatch.value.code == "PHASE_G_REMOTE_VERIFICATION_FAILED"
    remote_file.write_bytes(local_file.read_bytes())
    row = ledger.mark_artifact_verified(
        request.d2_run_id,
        remote_download=remote_file,
        remote_file_id="drive-file-1",
        remote_filename="d2-run.mp4",
    )
    assert row["submission_state"] == "D2_SUCCEEDED"
    assert row["remote_verified"] is True
    assert row["remote_file_id"] == "drive-file-1"
    assert row["remote_redownload_sha256"] == local_hash
    next_request = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_test-run-0002")
    with pytest.raises(PhaseGError) as stop:
        ledger.create_attempt(next_request)
    assert stop.value.code == "PHASE_G_SUCCESS_ALREADY_PROVEN"


def test_failed_artifact_retrieval_recovers_same_media_without_another_submit(
    tmp_path: Path,
) -> None:
    request = phase_g_request(tmp_path)
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    ledger.create_attempt(request)
    _capture_ledger_baseline(ledger)
    ledger.mark_generate_action(request.d2_run_id)
    _observe_ledger_confirmation(ledger, confirmation_id="new-confirmation")
    ledger.approve_gate(
        request.d2_run_id,
        video_count=1,
        credit_cost=10,
        confirmation_id="new-confirmation",
    )
    ledger.record_state(
        request.d2_run_id,
        submission_state="GENERATION_SUCCEEDED",
        provider_media_id="media-exact-1",
        provider_project_id=request.provider_project_id,
    )
    ledger.record_state(
        request.d2_run_id,
        submission_state="PERSISTENCE_FAILED",
        terminal_state="PERSISTENCE_FAILED",
        provider_media_id="media-exact-1",
        provider_project_id=request.provider_project_id,
    )

    recovered = ledger.record_artifact_recovered(request.d2_run_id)

    assert recovered["submission_state"] == "GENERATION_SUCCEEDED"
    assert recovered["provider_media_id"] == "media-exact-1"
    assert recovered["generate_action_count"] == 1


@pytest.mark.asyncio
async def test_phase_g_rejects_existing_media_identity_as_new_result(tmp_path: Path) -> None:
    port = FakeGenerationPort()
    port.evidence = replace(port.evidence, provider_media_id="old-media")
    request, ledger, journal, executor = make_executor(tmp_path, port)
    with pytest.raises(PhaseGError) as attribution:
        await executor.execute(request)
    assert attribution.value.code == "PHASE_G_ATTEMPT_ATTRIBUTION_FAILED"
    assert ledger.attempts()[0]["submission_state"] == "UNKNOWN_SUBMISSION_STATE"
    assert ledger.accounted_today() == Decimal(10)
    assert port.calls.count("generate") == 1
    assert journal.read().state == "CLEAN"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_phase_g_detects_multiple_new_provider_outputs(tmp_path: Path) -> None:
    port = FakeGenerationPort()
    port.evidence = replace(
        port.evidence,
        new_provider_media_ids=("media-new", "media-extra"),
    )
    request, ledger, journal, executor = make_executor(tmp_path, port)
    with pytest.raises(PhaseGError) as outputs:
        await executor.execute(request)
    assert outputs.value.code == "UNEXPECTED_MULTI_OUTPUT"
    assert ledger.attempts()[0]["submission_state"] == "UNEXPECTED_MULTI_OUTPUT"
    assert port.calls.count("generate") == 1
    assert journal.read().state == "CLEAN"  # type: ignore[union-attr]

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
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
        ),
    )

    assert attempt["provider_submit_rpc_observed"] is True
    assert attempt["provider_submit_request_count"] == 1
    assert attempt["provider_submit_transport_classification"] == ("SUBMIT_SENT_RESPONSE_UNKNOWN")
    assert "request_body" not in attempt
    assert "headers" not in attempt


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
        self.settings = dict(settings)

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

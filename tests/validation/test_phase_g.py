from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
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
    FlowConfirmationGate,
    FlowSubmissionBaseline,
    GoogleFlowAgentOnlyGenerationExecutor,
    PhaseGAttemptEvidence,
    PhaseGError,
    PhaseGExecutionRequest,
    validate_phase_g_request,
)
from gflow_cli.v2_runtime.phase_g_artifacts import sha256_file
from gflow_cli.v2_runtime.phase_g_live_flow import parse_confirmation_quote


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

    async def bind_request(self, request: PhaseGExecutionRequest) -> None:
        self.calls.append("bind")
        assert request.d2_run_id == "D2_test-run-0001"

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
        assert "duration of 8 seconds" in prompt and negative_prompt == ""

    async def read_prompt(self) -> str | None:
        self.calls.append("read_prompt")
        return self.prompt

    async def capture_submission_baseline(self) -> FlowSubmissionBaseline:
        self.calls.append("baseline")
        return replace(self.baseline, pending_count=self.pending_count)

    async def click_generate_once(self) -> None:
        self.calls.append("generate")
        if self.raise_after_click:
            raise TimeoutError("ambiguous post-click timeout")

    async def await_confirmation(self, baseline: FlowSubmissionBaseline) -> FlowConfirmationGate:
        self.calls.append("gate")
        return self.gate

    async def approve_confirmation(self, confirmation_id: str) -> None:
        self.calls.append("approve")
        assert confirmation_id == self.gate.confirmation_id

    async def observe_attempt(
        self, baseline: FlowSubmissionBaseline, confirmation_id: str
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

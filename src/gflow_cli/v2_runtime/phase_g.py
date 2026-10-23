"""Fail-closed Phase G execution records and the local D2 credit ledger.

Phase G is deliberately separate from the Phase F preparation port. This module
contains no browser or provider calls; the concrete executor owns those edges.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol, cast

from gflow_cli.errors import WafRejectionError

from .agent_recovery import AgentSettingsRecoveryJournal, RecoveryJournalError
from .errors import AssetVerificationError, V2RuntimeError
from .google_flow import FlowPresubmitRequest, build_agent_only_directive
from .phase_g_artifacts import sha256_file, verify_remote_redownload
from .production_execution import (
    ProductionCreditGuard,
    ProductionExecutionContext,
    ProductionExecutionError,
)
from .timestamps import format_utc_timestamp

_RUN_ID_RE = re.compile(r"^(?:D2|D6)_[A-Za-z0-9_-]{8,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROJECT_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_D6_RELEASE_CONTRACT_VERSION = 1
_LEDGER_SCHEMA = 1
_BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")
_SUBMISSION_STATES = frozenset(
    {
        "PREPARED",
        "GENERATE_ACTION_RECORDED",
        "COST_RESERVED_AWAITING_APPROVAL",
        "SUBMISSION_ACKNOWLEDGED",
        "GENERATION_PENDING",
        "GENERATION_SUCCEEDED",
        "D2_SUCCEEDED",
        "PROVIDER_REFUSED",
        "WAF_REJECTED",
        "GENERATION_FAILED",
        "UNEXPECTED_MULTI_OUTPUT",
        "MEDIA_VALIDATION_FAILED",
        "PERSISTENCE_FAILED",
        "UNKNOWN_SUBMISSION_STATE",
        "RECONCILED_NO_CHARGE",
        "FORENSICALLY_CLOSED_UNKNOWN",
    }
)
_TERMINAL_STATES = frozenset(
    {
        "D2_SUCCEEDED",
        "PROVIDER_REFUSED",
        "WAF_REJECTED",
        "GENERATION_FAILED",
        "UNEXPECTED_MULTI_OUTPUT",
        "MEDIA_VALIDATION_FAILED",
        "PERSISTENCE_FAILED",
        "RECONCILED_NO_CHARGE",
        "FORENSICALLY_CLOSED_UNKNOWN",
    }
)


class PhaseGError(V2RuntimeError):
    """Stable, non-secret failure from Phase G preconditions or accounting."""

    code = "PHASE_G_HOLD"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class D6PrerequisiteProof:
    """Read-only proof that one exact successful D2 matches a D6 request."""

    prerequisite_d2_run_id: str
    prerequisite_state: str
    d2_fingerprint_sha256: str
    d6_fingerprint_sha256: str
    prerequisite_ledger_sha256: str
    contract_version: int
    verified_at: str


@dataclass(frozen=True, slots=True)
class D2RetryAuthorization:
    """One explicit authorization to create a new D2 after an exact terminal failure."""

    predecessor_d2_run_id: str
    expected_terminal_state: str


@dataclass(frozen=True, slots=True)
class D6ReleaseResult:
    """Structured fail-closed result for one explicit D6 prerequisite check."""

    eligible: bool
    prerequisite_d2_run_id: str | None
    prerequisite_state: str | None
    binding_match: bool
    failure_code: str | None
    proof: D6PrerequisiteProof | None = None


@dataclass(frozen=True, slots=True)
class PhaseGExecutionRequest:
    """Immutable generation contract pinned to one approved Scene prompt."""

    scene_id: str
    scene_prompt_id: str
    generation_profile_id: str
    provider_id: str
    provider_account_id: str
    provider_project_id: str
    model_id: str
    generation_mode: str
    compiled_prompt_text: str
    compiled_negative_prompt: str
    source_context_sha256: str
    prompt_sha256: str
    duration_sec: float
    aspect_ratio: str
    output_count: int
    d2_run_id: str
    presubmit_request: FlowPresubmitRequest
    execution_context: ProductionExecutionContext | None = None
    prerequisite_d2_run_id: str | None = None
    model_compatibility_sha256: str | None = None

    @classmethod
    def from_presubmit(
        cls,
        request: FlowPresubmitRequest,
        d2_run_id: str,
        execution_context: ProductionExecutionContext | None = None,
        *,
        prerequisite_d2_run_id: str | None = None,
    ) -> PhaseGExecutionRequest:
        """Construct a pinned Phase G request from a Phase F input."""
        result = request.preflight
        prompt = request.prompt_version
        config = result.technical_configuration
        duration = config.get("duration_sec")
        aspect = config.get("aspect_ratio")
        count = config.get("output_count")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise PhaseGError("PHASE_G_REQUEST_INVALID", "Phase E duration is unresolved")
        if not isinstance(aspect, str) or not aspect:
            raise PhaseGError("PHASE_G_REQUEST_INVALID", "Phase E aspect ratio is unresolved")
        if isinstance(count, bool) or not isinstance(count, int):
            raise PhaseGError("PHASE_G_REQUEST_INVALID", "Phase E output count is unresolved")
        value = cls(
            result.scene_id,
            prompt.scene_prompt_id,
            prompt.generation_profile_id,
            request.provider_id,
            request.provider_account_id,
            request.provider_project_id,
            request.model_id,
            request.generation_mode,
            prompt.compiled_prompt_text,
            prompt.compiled_negative_prompt,
            prompt.source_context_sha256,
            prompt.prompt_sha256,
            float(duration),
            aspect,
            count,
            d2_run_id,
            request,
            execution_context,
            prerequisite_d2_run_id,
            cast(str | None, result.resolved.get("model_compatibility_sha256")),
        )
        validate_phase_g_request(value)
        return value


@dataclass(frozen=True, slots=True)
class PhaseGAttemptEvidence:
    """Sanitized evidence recorded for one D2 attempt."""

    d2_run_id: str
    submission_state: str
    provider_execution_id: str | None = None
    provider_media_id: str | None = None
    provider_project_id: str | None = None
    terminal_state: str | None = None
    accounted_credit_cost: Decimal | None = None
    attributed_confirmation_id: str | None = None
    new_provider_media_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PhaseGSubmitTransportEvidence:
    """Allowlisted request/response facts for the one Phase G submit window."""

    generate_click_dispatched: bool | None = None
    provider_submit_rpc_observed: bool = False
    provider_submit_rpc_id: str | None = None
    provider_submit_observed_at: str | None = None
    provider_submit_http_method: str | None = None
    provider_submit_project_id: str | None = None
    provider_submit_request_count: int = 0
    provider_submit_dispatch_window_completed: bool = False
    provider_submit_request_failed: bool | None = None
    provider_submit_response_observed: bool | None = None
    provider_submit_response_status: int | None = None
    provider_submit_generation_record_observed: bool | None = None
    provider_submit_refusal_detected: bool | None = None
    submission_surface: str = "CLASSIC_MIGRATED"
    agent_turn_dispatched: bool | None = None


@dataclass(frozen=True, slots=True)
class FlowSubmissionBaseline:
    """Attempt-specific provider state captured immediately before Generate."""

    project_id: str
    media_ids: frozenset[str]
    execution_ids: frozenset[str]
    pending_count: int
    confirmation_ids: frozenset[str]
    agent_turn_id: str | None
    agent_turn_bubble_hashes: tuple[str, ...] = ()
    finished_tile_key_hashes: tuple[str, ...] = ()
    chat_video_media_ids: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class FlowConfirmationGate:
    """One live confirmation gate with its explicit quote."""

    confirmation_id: str
    project_id: str
    video_count: int | None
    credit_cost: Decimal | None
    visible_balance: Decimal | None = None
    progress_before_confirmation: bool = False


@dataclass(frozen=True, slots=True)
class PhaseGExecutionResult:
    """Sanitized result of the single Phase G execution attempt."""

    d2_run_id: str
    submission_state: str
    confirmation_id: str
    provider_execution_id: str | None = None
    provider_media_id: str | None = None
    provider_project_id: str | None = None


class FlowGenerationPort(Protocol):
    """Generation-only interface kept separate from the F5 preparation port."""

    async def bind_request(self, request: PhaseGExecutionRequest) -> None: ...
    async def snapshot_settings(self) -> Mapping[str, object]: ...
    async def apply_settings(self, settings: Mapping[str, object]) -> Mapping[str, object]: ...
    async def read_settings(self) -> Mapping[str, object]: ...
    async def restore_settings(self, settings: Mapping[str, object]) -> None: ...
    async def fill_prompt(self, prompt: str, negative_prompt: str) -> None: ...
    async def read_prompt(self) -> str | None: ...
    async def capture_submission_baseline(self) -> FlowSubmissionBaseline: ...
    async def arm_submit_request_observer(self) -> None: ...
    async def click_generate_once(self) -> None: ...
    async def await_submit_dispatch(self) -> PhaseGSubmitTransportEvidence: ...
    def read_submit_transport_evidence(self) -> PhaseGSubmitTransportEvidence: ...
    def disarm_submit_request_observer(self) -> None: ...
    async def await_confirmation(
        self, baseline: FlowSubmissionBaseline, *, observation_deadline: float
    ) -> FlowConfirmationGate | None: ...
    async def read_current_confirmation(
        self, baseline: FlowSubmissionBaseline
    ) -> FlowConfirmationGate: ...
    async def approve_confirmation(self, confirmation_id: str) -> None: ...
    async def observe_attempt(
        self,
        baseline: FlowSubmissionBaseline,
        confirmation_id: str | None,
        *,
        observation_deadline: float,
    ) -> PhaseGAttemptEvidence: ...


class GoogleFlowAgentOnlyGenerationExecutor:
    """One-attempt controller; concrete browser interactions are injected separately."""

    def __init__(
        self,
        port: FlowGenerationPort,
        ledger: D2DailyCreditLedger,
        recovery_journal: AgentSettingsRecoveryJournal,
        *,
        production_credit_guard: ProductionCreditGuard | None = None,
    ) -> None:
        self.port = port
        self.ledger = ledger
        self.recovery_journal = recovery_journal
        self.production_credit_guard = production_credit_guard

    async def execute(
        self,
        request: PhaseGExecutionRequest,
        *,
        retry_authorization: D2RetryAuthorization | None = None,
    ) -> PhaseGExecutionResult:
        """Apply settings, click once, validate one new gate and restore defaults."""
        validate_phase_g_request(request)
        if request.d2_run_id.startswith("D6_") and retry_authorization is not None:
            raise PhaseGError(
                "D6_RELEASE_PATH_REQUIRED", "D6 attempts cannot use D2 retry authorization"
            )
        if (request.execution_context is None) != (self.production_credit_guard is None):
            raise PhaseGError(
                "PHASE_G_EXECUTION_CONTEXT_REQUIRED",
                "production execution context and shared credit guard must be supplied together",
            )
        d6_proof: D6PrerequisiteProof | None = None
        if request.d2_run_id.startswith("D6_"):
            assert request.prerequisite_d2_run_id is not None
            d2_ledger = D2DailyCreditLedger(request.presubmit_request.runtime_state_root)
            release = evaluate_d6_release(
                prerequisite_d2_run_id=request.prerequisite_d2_run_id,
                d6_request=request,
                ledger=d2_ledger,
            )
            if not release.eligible or release.proof is None:
                failure = release.failure_code or "D6_PREREQUISITE_NOT_SUCCEEDED"
                raise PhaseGError(
                    failure,
                    "identified D2 prerequisite does not authorize this D6 request",
                )
            d6_proof = release.proof
            if (
                request.execution_context is None
                or self.production_credit_guard is None
                or request.execution_context.environment != "PROD"
            ):
                raise PhaseGError(
                    "D6_PRODUCTION_CONTEXT_REQUIRED",
                    "D6 requires a PROD execution context and shared production credit guard",
                )
        try:
            self.recovery_journal.require_clean_or_missing()
        except RecoveryJournalError as exc:
            raise PhaseGError(
                "PHASE_G_RECOVERY_REQUIRED", "settings recovery must be clean before Phase G"
            ) from exc
        await self.port.bind_request(request)

        # Snapshot all relevant defaults before the journal or first mutation.
        original = dict(await self.port.snapshot_settings())
        if not original:
            raise PhaseGError(
                "PHASE_G_SETTINGS_SNAPSHOT_INVALID", "original settings snapshot is empty"
            )
        target: dict[str, object] = {
            "model_id": request.model_id,
            "generation_mode": request.generation_mode,
            "aspect_ratio": request.aspect_ratio,
            "output_count": request.output_count,
            "confirm_before_generating": "Always",
        }
        if not set(target).issubset(original):
            raise PhaseGError(
                "PHASE_G_SETTINGS_SNAPSHOT_INVALID",
                "original settings snapshot omits a setting Phase G will change",
            )
        self.recovery_journal.begin(
            correlation_id=request.d2_run_id,
            original_settings=original,
            intended_settings=target,
        )
        started = False
        generate_recorded = False
        gate_approved = False
        try:
            if request.execution_context is not None and self.production_credit_guard is not None:
                self.production_credit_guard.prepare(request.execution_context)
            try:
                if d6_proof is not None:
                    self.ledger.create_d6_attempt(request, proof=d6_proof)
                elif retry_authorization is not None:
                    self.ledger.create_authorized_retry_attempt(request, retry_authorization)
                else:
                    self.ledger.create_attempt(request)
            except BaseException:
                if (
                    request.execution_context is not None
                    and self.production_credit_guard is not None
                ):
                    self.production_credit_guard.fail_before_submit(
                        request.execution_context.attempt_id
                    )
                raise
            applied = dict(await self.port.apply_settings(target))
            if any(applied.get(key) != value for key, value in target.items()):
                raise PhaseGError(
                    "PHASE_G_SETTINGS_READBACK_MISMATCH", "Flow settings differ from request"
                )
            directive = build_agent_only_directive(
                request.compiled_prompt_text,
                request.presubmit_request.preflight.technical_configuration,
            )
            await self.port.fill_prompt(directive, request.compiled_negative_prompt)
            if await self.port.read_prompt() != directive:
                raise PhaseGError(
                    "PHASE_G_PROMPT_READBACK_MISMATCH",
                    "Flow composer prompt differs from the pinned Agent directive",
                )
            # This baseline is deliberately captured immediately before submit.
            baseline = await self.port.capture_submission_baseline()
            if baseline.project_id != request.provider_project_id:
                raise PhaseGError("PHASE_G_PROJECT_MISMATCH", "baseline belongs to another project")
            if baseline.pending_count:
                raise PhaseGError(
                    "PHASE_G_UNRELATED_GENERATION_ACTIVE", "project already has pending work"
                )
            baseline = self.ledger.include_historical_provider_identities(
                request.d2_run_id, baseline
            )
            self.ledger.capture_baseline(request.d2_run_id, baseline)
            await self.port.arm_submit_request_observer()
            self.ledger.mark_generate_action(request.d2_run_id)
            generate_recorded = True
            started = True
            if request.execution_context is not None and self.production_credit_guard is not None:
                self.production_credit_guard.record_generate_click(
                    request.execution_context.attempt_id
                )
            observation_deadline = time.monotonic() + 240
            await self.port.click_generate_once()
            dispatch = await self.port.await_submit_dispatch()
            self.ledger.record_submit_transport_evidence(request.d2_run_id, dispatch)
            if dispatch.submission_surface == "AGENT_ONLY":
                if dispatch.agent_turn_dispatched is not True:
                    raise PhaseGError(
                        "PHASE_G_AGENT_TURN_NOT_DISPATCHED",
                        "the exact Agent prompt was not observed as a new project chat turn",
                    )
            elif dispatch.submission_surface == "CLASSIC_MIGRATED":
                if dispatch.provider_submit_request_count == 0:
                    raise PhaseGError(
                        "PHASE_G_SUBMIT_RPC_NOT_OBSERVED",
                        "no YhhmEf submit request was observed in the bounded dispatch window",
                    )
            else:
                raise PhaseGError(
                    "PHASE_G_SUBMISSION_SURFACE_UNKNOWN",
                    "submit evidence does not identify a supported Flow surface",
                )
            if dispatch.provider_submit_request_count > 1:
                raise PhaseGError(
                    "PHASE_G_DUPLICATE_SUBMIT_RPC",
                    "multiple YhhmEf submit requests were observed after one Generate click",
                )
            gate = await self.port.await_confirmation(
                baseline, observation_deadline=observation_deadline
            )
            confirmation_id: str | None = None
            if gate is not None and not gate.progress_before_confirmation:
                if (
                    not gate.confirmation_id
                    or gate.confirmation_id in baseline.confirmation_ids
                    or gate.project_id != request.provider_project_id
                ):
                    self._record_no_charge(request)
                    raise PhaseGError(
                        "PHASE_G_CONFIRMATION_NOT_ATTRIBUTABLE",
                        "confirmation is stale or mismatched",
                    )
                confirmation_id = gate.confirmation_id
                self.ledger.observe_confirmation(
                    request.d2_run_id,
                    confirmation_id=gate.confirmation_id,
                    video_count=gate.video_count,
                    credit_cost=gate.credit_cost,
                    visible_balance=gate.visible_balance,
                )
                if gate.video_count != 1 or gate.credit_cost is None or gate.credit_cost <= 0:
                    self._record_no_charge(request)
                    code = (
                        "PHASE_G_VIDEO_COUNT_UNPROVEN"
                        if gate.video_count != 1
                        else "PHASE_G_CREDIT_COST_UNKNOWN"
                    )
                    raise PhaseGError(
                        code, "confirmation must prove one video and known positive cost"
                    )
                try:
                    if (
                        request.execution_context is not None
                        and self.production_credit_guard is not None
                    ):
                        budget = request.execution_context.credit_budget_context
                        if (
                            gate.visible_balance is not None
                            and budget.provider_remaining_free != gate.visible_balance
                        ):
                            raise ProductionExecutionError(
                                "provider free balance changed since the execution context was read"
                            )
                        self.production_credit_guard.reserve_quote(
                            request.execution_context.attempt_id,
                            video_count=gate.video_count,
                            quoted_credits=gate.credit_cost,
                            budget=budget,
                        )
                    self.ledger.approve_gate(
                        request.d2_run_id,
                        video_count=gate.video_count,
                        credit_cost=gate.credit_cost,
                        visible_balance=gate.visible_balance,
                        confirmation_id=gate.confirmation_id,
                    )
                except ProductionExecutionError as exc:
                    self._record_no_charge(request)
                    raise PhaseGError(
                        "PHASE_G_CREDIT_GUARD_HOLD",
                        "shared production credit gate refused approval",
                    ) from exc
                except PhaseGError as exc:
                    if exc.code in {
                        "DAILY_FREE_CREDIT_BUDGET_EXHAUSTED",
                        "PHASE_G_VISIBLE_BALANCE_INSUFFICIENT",
                    }:
                        self._record_no_charge(request)
                    raise
                gate_approved = True
                await self.port.approve_confirmation(gate.confirmation_id)
            evidence = await self.port.observe_attempt(
                baseline,
                confirmation_id,
                observation_deadline=observation_deadline,
            )
            submit_transport = self.port.read_submit_transport_evidence()
            self.ledger.record_submit_transport_evidence(request.d2_run_id, submit_transport)
            if (
                submit_transport.submission_surface == "CLASSIC_MIGRATED"
                and submit_transport.provider_submit_response_observed is True
                and submit_transport.provider_submit_generation_record_observed is False
            ):
                raise PhaseGError(
                    "PHASE_G_SUBMIT_RESPONSE_NO_GENERATION_RECORD",
                    "YhhmEf response did not contain an attributable GenerationRecord",
                )
            if evidence.d2_run_id != request.d2_run_id:
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_ATTRIBUTION_FAILED", "provider evidence belongs to another run"
                )
            if evidence.provider_project_id != request.provider_project_id:
                raise PhaseGError(
                    "PHASE_G_PROJECT_MISMATCH", "provider result belongs to another project"
                )
            if (
                confirmation_id is not None
                and evidence.attributed_confirmation_id != confirmation_id
            ):
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_ATTRIBUTION_FAILED",
                    "provider evidence is not tied to this confirmation",
                )
            media_ids = evidence.new_provider_media_ids
            if not media_ids and evidence.provider_media_id:
                media_ids = (evidence.provider_media_id,)
            if len(media_ids) > 1:
                self.ledger.record_state(
                    request.d2_run_id,
                    submission_state="UNEXPECTED_MULTI_OUTPUT",
                    terminal_state="UNEXPECTED_MULTI_OUTPUT",
                    provider_execution_id=evidence.provider_execution_id,
                    provider_media_ids=media_ids,
                    provider_project_id=evidence.provider_project_id,
                    accounted_credit_cost=evidence.accounted_credit_cost,
                )
                started = False
                raise PhaseGError(
                    "UNEXPECTED_MULTI_OUTPUT", "more than one new output is attributable"
                )
            if any(media_id in baseline.media_ids for media_id in media_ids):
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_ATTRIBUTION_FAILED",
                    "provider media identity existed in the pre-submit baseline",
                )
            if evidence.provider_execution_id in baseline.execution_ids:
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_ATTRIBUTION_FAILED",
                    "provider execution identity existed in the pre-submit baseline",
                )
            if evidence.submission_state == "GENERATION_SUCCEEDED" and len(media_ids) != 1:
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_ATTRIBUTION_FAILED",
                    "success does not identify exactly one new provider media asset",
                )
            if (
                evidence.submission_state in {"SUBMISSION_ACKNOWLEDGED", "GENERATION_PENDING"}
                and evidence.provider_execution_id is None
                and len(media_ids) != 1
            ):
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_ATTRIBUTION_FAILED",
                    "pending attempt has no new provider execution or media identity",
                )
            if evidence.submission_state == "WAF_REJECTED":
                self.ledger.record_state(
                    request.d2_run_id,
                    submission_state="WAF_REJECTED",
                    terminal_state="WAF_REJECTED",
                    provider_execution_id=evidence.provider_execution_id,
                    provider_media_id=evidence.provider_media_id,
                    provider_media_ids=media_ids,
                    provider_project_id=evidence.provider_project_id,
                    accounted_credit_cost=evidence.accounted_credit_cost,
                )
                self._resolve_production_guard(
                    request, "WAF_REJECTION", evidence.accounted_credit_cost
                )
                started = False
                raise PhaseGError(
                    "WAF_REJECTED", "Flow refused unusual activity; no retry is permitted"
                )
            self.ledger.record_state(
                request.d2_run_id,
                submission_state=evidence.submission_state,
                terminal_state=evidence.terminal_state,
                provider_execution_id=evidence.provider_execution_id,
                provider_media_id=evidence.provider_media_id,
                provider_media_ids=media_ids,
                provider_project_id=evidence.provider_project_id,
                accounted_credit_cost=evidence.accounted_credit_cost,
            )
            if evidence.submission_state == "GENERATION_SUCCEEDED":
                guard_state = "SUCCESS"
            elif evidence.submission_state == "PROVIDER_REFUSED":
                guard_state = "PROVIDER_REJECTED"
            elif evidence.submission_state in {"GENERATION_FAILED", "UNEXPECTED_MULTI_OUTPUT"}:
                guard_state = "FAILED_CONFIRMED"
            else:
                guard_state = "UNKNOWN_SUBMISSION_STATE"
            self._resolve_production_guard(request, guard_state, evidence.accounted_credit_cost)
            started = False
            return PhaseGExecutionResult(
                request.d2_run_id,
                evidence.submission_state,
                confirmation_id or "",
                evidence.provider_execution_id,
                evidence.provider_media_id,
                evidence.provider_project_id,
            )
        except WafRejectionError:
            if generate_recorded:
                self._record_submit_transport_evidence(request)
            if generate_recorded:
                self.ledger.record_state(
                    request.d2_run_id,
                    submission_state="WAF_REJECTED",
                    terminal_state="WAF_REJECTED",
                    provider_project_id=request.provider_project_id,
                    accounted_credit_cost=None if gate_approved else 0,
                )
                self._resolve_production_guard(request, "WAF_REJECTION", None)
            started = False
            raise
        except BaseException:
            if started and generate_recorded:
                self._record_submit_transport_evidence(request)
                self._record_unknown(request)
            elif request.execution_context is not None and self.production_credit_guard is not None:
                try:
                    self.production_credit_guard.fail_before_submit(
                        request.execution_context.attempt_id
                    )
                except ProductionExecutionError:
                    pass
            raise
        finally:
            self.port.disarm_submit_request_observer()
            # Failed restoration leaves the journal DIRTY and blocks new work.
            restoring_error: Exception | None = None
            try:
                self.recovery_journal.set_state("RESTORING")
            except Exception as exc:
                restoring_error = exc
            try:
                await self.port.restore_settings(original)
                if dict(await self.port.read_settings()) != original:
                    raise PhaseGError(
                        "PHASE_G_SETTINGS_RESTORE_FAILED", "original settings readback differs"
                    )
                if restoring_error is None:
                    self.recovery_journal.set_state("CLEAN")
            except Exception:
                # Keep DIRTY/RESTORING as durable evidence and block the next run.
                raise
            if restoring_error is not None:
                raise PhaseGError(
                    "PHASE_G_SETTINGS_RESTORE_FAILED",
                    "recovery journal could not record restoration",
                ) from restoring_error

    def _record_no_charge(self, request: PhaseGExecutionRequest) -> None:
        self.ledger.record_state(
            request.d2_run_id,
            submission_state="RECONCILED_NO_CHARGE",
            terminal_state="RECONCILED_NO_CHARGE",
            accounted_credit_cost=0,
            provider_project_id=request.provider_project_id,
        )
        self._resolve_production_guard(request, "FAILED_CONFIRMED", 0)

    def _record_submit_transport_evidence(self, request: PhaseGExecutionRequest) -> None:
        try:
            self.ledger.record_submit_transport_evidence(
                request.d2_run_id, self.port.read_submit_transport_evidence()
            )
        except PhaseGError:
            # Preserve the original Phase G failure; the attempt remains UNKNOWN.
            pass

    def _resolve_production_guard(
        self,
        request: PhaseGExecutionRequest,
        state: str,
        actual_credits: Decimal | int | float | str | None,
    ) -> None:
        if request.execution_context is None or self.production_credit_guard is None:
            return
        try:
            self.production_credit_guard.resolve(
                request.execution_context.attempt_id,
                state,
                actual_credits=actual_credits,
            )
        except ProductionExecutionError as exc:
            raise PhaseGError(
                "PHASE_G_CREDIT_GUARD_HOLD", "shared production credit state is unresolved"
            ) from exc

    def _record_unknown(self, request: PhaseGExecutionRequest) -> None:
        """Reserve the quoted amount or all remaining budget on ambiguous submit."""
        try:
            rows = {str(row["d2_run_id"]): row for row in self.ledger.attempts()}
            current = rows.get(request.d2_run_id, {})
            if current.get("submission_state") in _TERMINAL_STATES:
                return
            self.ledger.record_state(
                request.d2_run_id,
                submission_state="UNKNOWN_SUBMISSION_STATE",
                provider_project_id=request.provider_project_id,
            )
            self._resolve_production_guard(request, "UNKNOWN_SUBMISSION_STATE", None)
        except PhaseGError:
            # Do not mask the triggering exception; the recovery journal still
            # records settings as DIRTY if the browser restore cannot be proven.
            return


def validate_phase_g_request(request: PhaseGExecutionRequest) -> None:
    """Require a fresh, exact Phase E match and a persisted APPROVED prompt."""
    presubmit = request.presubmit_request
    phase_e = presubmit.preflight
    prompt = presubmit.prompt_version
    if not _RUN_ID_RE.fullmatch(request.d2_run_id):
        raise PhaseGError("PHASE_G_RUN_ID_INVALID", "Phase G attempt ID is invalid")
    if request.d2_run_id.startswith("D6_"):
        prerequisite_id = request.prerequisite_d2_run_id
        if not prerequisite_id:
            raise PhaseGError(
                "D6_PREREQUISITE_REQUIRED", "D6 requires an explicit prerequisite D2 run ID"
            )
        if not re.fullmatch(r"D2_[A-Za-z0-9_-]{8,64}", prerequisite_id):
            raise PhaseGError("D6_PREREQUISITE_NOT_FOUND", "D6 prerequisite must identify a D2 run")
        if not isinstance(request.model_compatibility_sha256, str) or not _SHA256_RE.fullmatch(
            request.model_compatibility_sha256
        ):
            raise PhaseGError(
                "D6_MODEL_COMPATIBILITY_REQUIRED",
                "D6 requires a Phase E model/capability compatibility fingerprint",
            )
    elif request.prerequisite_d2_run_id is not None:
        raise PhaseGError(
            "PHASE_G_PREREQUISITE_INVALID", "D2 attempts cannot carry a D6 prerequisite"
        )
    context = request.execution_context
    if context is not None and (
        context.attempt_id != request.d2_run_id
        or context.scene_id != request.scene_id
        or context.scene_prompt_id != request.scene_prompt_id
        or context.generation_profile_id != request.generation_profile_id
        or context.provider_id != request.provider_id
        or context.provider_account_id != request.provider_account_id
        or context.provider_project_id != request.provider_project_id
        or context.prerequisite_d2_run_id != request.prerequisite_d2_run_id
    ):
        raise PhaseGError(
            "PHASE_G_EXECUTION_CONTEXT_MISMATCH",
            "production context differs from the pinned Phase E request",
        )
    if prompt.prompt_status != "APPROVED":
        raise PhaseGError("PHASE_G_PROMPT_NOT_APPROVED", "prompt version is not APPROVED")
    if not phase_e.ready_for_provider_adapter or phase_e.status != "PREFLIGHT_PASS":
        raise PhaseGError("PHASE_E_PREFLIGHT_REQUIRED", "fresh Phase E preflight did not pass")
    if phase_e.errors:
        raise PhaseGError("PHASE_E_PREFLIGHT_REQUIRED", "fresh Phase E has validation errors")
    identities = (
        (request.scene_id, phase_e.scene_id, prompt.scene_id),
        (
            request.generation_profile_id,
            phase_e.generation_profile_id,
            prompt.generation_profile_id,
        ),
        (request.provider_id, str(phase_e.resolved.get("provider_id", "")), presubmit.provider_id),
        (
            request.provider_account_id,
            str(phase_e.resolved.get("provider_account_id", "")),
            presubmit.provider_account_id,
        ),
        (request.model_id, str(phase_e.resolved.get("model_id", "")), presubmit.model_id),
        (
            request.generation_mode,
            str(phase_e.technical_configuration.get("generation_mode", "")),
            presubmit.generation_mode,
        ),
    )
    if any(len(set(values)) != 1 for values in identities):
        raise PhaseGError("PHASE_G_IDENTITY_MISMATCH", "pinned Phase G identities differ")
    if request.provider_project_id != presubmit.provider_project_id:
        raise PhaseGError("PHASE_G_PROJECT_MISMATCH", "provider project identity differs")
    if request.scene_prompt_id != prompt.scene_prompt_id:
        raise PhaseGError("PHASE_G_PROMPT_MISMATCH", "Scene prompt identity differs")
    if (
        request.compiled_prompt_text != phase_e.compiled_prompt
        or prompt.compiled_prompt_text != phase_e.compiled_prompt
    ):
        raise PhaseGError(
            "PHASE_G_PROMPT_MISMATCH", "compiled prompt text differs from fresh Phase E"
        )
    if (
        request.compiled_negative_prompt != (phase_e.compiled_negative_prompt or "")
        or prompt.compiled_negative_prompt != request.compiled_negative_prompt
    ):
        raise PhaseGError(
            "PHASE_G_PROMPT_MISMATCH", "compiled negative prompt differs from fresh Phase E"
        )
    if (
        request.source_context_sha256 != phase_e.source_context_sha256
        or prompt.source_context_sha256 != request.source_context_sha256
    ):
        raise PhaseGError("PHASE_G_HASH_MISMATCH", "source context hash differs from fresh Phase E")
    if (
        request.prompt_sha256 != phase_e.prompt_sha256
        or prompt.prompt_sha256 != request.prompt_sha256
    ):
        raise PhaseGError("PHASE_G_HASH_MISMATCH", "prompt hash differs from fresh Phase E")
    if not _SHA256_RE.fullmatch(request.source_context_sha256) or not _SHA256_RE.fullmatch(
        request.prompt_sha256
    ):
        raise PhaseGError("PHASE_G_HASH_INVALID", "prompt hash is malformed")
    config = phase_e.technical_configuration
    if (
        config.get("duration_sec") != request.duration_sec
        or config.get("aspect_ratio") != request.aspect_ratio
        or config.get("output_count") != request.output_count
    ):
        raise PhaseGError("PHASE_G_SETTINGS_MISMATCH", "generation settings differ from Phase E")
    if request.generation_mode != "TEXT_TO_VIDEO" or request.output_count != 1:
        raise PhaseGError("PHASE_G_REQUEST_UNSUPPORTED", "D2 requires one text-to-video output")
    if presubmit.references or request.compiled_negative_prompt:
        raise PhaseGError(
            "PHASE_G_REQUEST_UNSUPPORTED",
            "controlled Agent-only D2 requires zero references and an empty negative prompt",
        )
    if not _PROJECT_RE.fullmatch(request.provider_project_id):
        raise PhaseGError("PHASE_G_PROJECT_MISMATCH", "provider project ID is invalid")


def _d6_semantic_fingerprint(values: Mapping[str, object]) -> str:
    """Hash only the workload/provider contract shared across TEST and PROD lanes."""
    raw = {
        "contract_version": _D6_RELEASE_CONTRACT_VERSION,
        "scene_id": values.get("scene_id"),
        "provider_id": values.get("provider_id"),
        "generation_mode": values.get("generation_mode"),
        "model_id": values.get("model_id"),
        "model_compatibility_sha256": values.get("model_compatibility_sha256"),
        "duration_sec": str(values.get("requested_duration_sec")),
        "aspect_ratio": values.get("requested_aspect_ratio"),
        "output_count": values.get("requested_output_count"),
    }
    encoded = json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _d6_request_semantics(request: PhaseGExecutionRequest) -> dict[str, object]:
    return {
        "scene_id": request.scene_id,
        "provider_id": request.provider_id,
        "generation_mode": request.generation_mode,
        "model_id": request.model_id,
        "model_compatibility_sha256": request.model_compatibility_sha256,
        "requested_duration_sec": request.duration_sec,
        "requested_aspect_ratio": request.aspect_ratio,
        "requested_output_count": request.output_count,
    }


def _evaluate_d6_release_rows(
    prerequisite_d2_run_id: str | None,
    d6_request: PhaseGExecutionRequest,
    attempts: Sequence[Mapping[str, object]],
    *,
    ledger_sha256: str,
) -> D6ReleaseResult:
    if not prerequisite_d2_run_id or not d6_request.prerequisite_d2_run_id:
        return D6ReleaseResult(
            False, prerequisite_d2_run_id, None, False, "D6_PREREQUISITE_REQUIRED"
        )
    if prerequisite_d2_run_id != d6_request.prerequisite_d2_run_id:
        return D6ReleaseResult(
            False, prerequisite_d2_run_id, None, False, "D6_PREREQUISITE_REQUIRED"
        )
    if not re.fullmatch(r"D2_[A-Za-z0-9_-]{8,64}", prerequisite_d2_run_id):
        return D6ReleaseResult(
            False, prerequisite_d2_run_id, None, False, "D6_PREREQUISITE_NOT_FOUND"
        )
    prerequisite = next(
        (row for row in attempts if row.get("d2_run_id") == prerequisite_d2_run_id), None
    )
    if prerequisite is None:
        return D6ReleaseResult(
            False, prerequisite_d2_run_id, None, False, "D6_PREREQUISITE_NOT_FOUND"
        )
    state = str(prerequisite.get("submission_state", ""))
    if state != "D2_SUCCEEDED" or prerequisite.get("terminal_state") != "D2_SUCCEEDED":
        return D6ReleaseResult(
            False, prerequisite_d2_run_id, state, False, "D6_PREREQUISITE_NOT_SUCCEEDED"
        )
    local_sha = prerequisite.get("local_sha256")
    remote_sha = prerequisite.get("remote_redownload_sha256")
    model_compatibility_sha256 = prerequisite.get("model_compatibility_sha256")
    artifact_verified = (
        prerequisite.get("d6_release_contract_version") == _D6_RELEASE_CONTRACT_VERSION
        and prerequisite.get("remote_verified") is True
        and isinstance(model_compatibility_sha256, str)
        and _SHA256_RE.fullmatch(model_compatibility_sha256) is not None
        and isinstance(prerequisite.get("provider_media_id"), str)
        and bool(prerequisite.get("provider_media_id"))
        and isinstance(prerequisite.get("remote_file_id"), str)
        and bool(prerequisite.get("remote_file_id"))
        and isinstance(local_sha, str)
        and _SHA256_RE.fullmatch(local_sha) is not None
        and local_sha == remote_sha
    )
    if not artifact_verified:
        return D6ReleaseResult(
            False, prerequisite_d2_run_id, state, False, "D6_PREREQUISITE_NOT_SUCCEEDED"
        )
    d2_fingerprint = _d6_semantic_fingerprint(prerequisite)
    d6_fingerprint = _d6_semantic_fingerprint(_d6_request_semantics(d6_request))
    if d2_fingerprint != d6_fingerprint:
        return D6ReleaseResult(
            False, prerequisite_d2_run_id, state, False, "D6_PREREQUISITE_BINDING_MISMATCH"
        )
    proof = D6PrerequisiteProof(
        prerequisite_d2_run_id=prerequisite_d2_run_id,
        prerequisite_state=state,
        d2_fingerprint_sha256=d2_fingerprint,
        d6_fingerprint_sha256=d6_fingerprint,
        prerequisite_ledger_sha256=ledger_sha256,
        contract_version=_D6_RELEASE_CONTRACT_VERSION,
        verified_at=_utc_now(),
    )
    return D6ReleaseResult(True, prerequisite_d2_run_id, state, True, None, proof)


def evaluate_d6_release(
    *,
    prerequisite_d2_run_id: str | None,
    d6_request: PhaseGExecutionRequest,
    ledger: D2DailyCreditLedger,
) -> D6ReleaseResult:
    """Read-only check of an exact, artifact-verified D2 prerequisite for D6."""
    if not d6_request.d2_run_id.startswith("D6_"):
        return D6ReleaseResult(
            False, prerequisite_d2_run_id, None, False, "D6_PREREQUISITE_REQUIRED"
        )
    if (
        not prerequisite_d2_run_id
        or not d6_request.prerequisite_d2_run_id
        or prerequisite_d2_run_id != d6_request.prerequisite_d2_run_id
    ):
        return D6ReleaseResult(
            False, prerequisite_d2_run_id, None, False, "D6_PREREQUISITE_REQUIRED"
        )
    if not re.fullmatch(r"D2_[A-Za-z0-9_-]{8,64}", prerequisite_d2_run_id):
        return D6ReleaseResult(
            False, prerequisite_d2_run_id, None, False, "D6_PREREQUISITE_NOT_FOUND"
        )
    authoritative_path = (
        d6_request.presubmit_request.runtime_state_root / "phase-g" / "d2-credit-ledger.json"
    )
    if ledger.path.resolve() != authoritative_path.resolve():
        return D6ReleaseResult(
            False, prerequisite_d2_run_id, None, False, "D6_PREREQUISITE_NOT_FOUND"
        )
    snapshot = ledger.d6_prerequisite_snapshot()
    if snapshot is None:
        return D6ReleaseResult(
            False, prerequisite_d2_run_id, None, False, "D6_PREREQUISITE_NOT_FOUND"
        )
    attempts, digest = snapshot
    return _evaluate_d6_release_rows(
        prerequisite_d2_run_id, d6_request, attempts, ledger_sha256=digest
    )


class D2DailyCreditLedger:
    """Atomic task-local ledger enforcing a 50-credit Bangkok-day ceiling."""

    def __init__(self, runtime_state_root: Path, *, daily_budget: int = 50) -> None:
        if daily_budget != 50:
            raise PhaseGError("PHASE_G_BUDGET_INVALID", "D2 daily budget is fixed at 50 credits")
        if not runtime_state_root.is_absolute() or runtime_state_root == Path(
            runtime_state_root.anchor
        ):
            raise PhaseGError(
                "PHASE_G_LEDGER_PATH_INVALID", "runtime state root must be absolute and non-root"
            )
        self.root = runtime_state_root / "phase-g"
        self.path = self.root / "d2-credit-ledger.json"
        self.lock_path = self.root / ".d2-credit-ledger.lock"
        self.daily_budget = daily_budget

    def create_attempt(self, request: PhaseGExecutionRequest) -> dict[str, object]:
        """Persist a zero-action attempt before any UI mutation."""
        validate_phase_g_request(request)
        if request.d2_run_id.startswith("D6_"):
            code = (
                "D6_PREREQUISITE_REQUIRED"
                if not request.prerequisite_d2_run_id
                else "D6_RELEASE_PATH_REQUIRED"
            )
            raise PhaseGError(
                code, "D6 attempts must use create_d6_attempt with a verified D2 release proof"
            )
        return self._mutate(lambda payload: self._create(payload, request))

    def create_authorized_retry_attempt(
        self,
        request: PhaseGExecutionRequest,
        authorization: D2RetryAuthorization,
    ) -> dict[str, object]:
        """Consume one exact retry authorization while atomically creating its new D2 row."""
        validate_phase_g_request(request)
        if request.d2_run_id.startswith("D6_"):
            raise PhaseGError(
                "D6_RELEASE_PATH_REQUIRED", "D6 attempts cannot use D2 retry authorization"
            )
        return self._mutate(
            lambda payload: self._create(payload, request, retry_authorization=authorization)
        )

    def create_d6_attempt(
        self, request: PhaseGExecutionRequest, *, proof: D6PrerequisiteProof | None
    ) -> dict[str, object]:
        """Atomically verify and persist a D6 attempt linked to one successful D2."""
        validate_phase_g_request(request)
        if not request.d2_run_id.startswith("D6_"):
            raise PhaseGError(
                "PHASE_G_RUN_ID_INVALID", "dedicated D6 creation requires a D6 run ID"
            )
        prerequisite_id = request.prerequisite_d2_run_id
        if not prerequisite_id:
            raise PhaseGError(
                "D6_PREREQUISITE_REQUIRED", "D6 requires an explicit prerequisite D2 run ID"
            )
        if proof is None:
            raise PhaseGError(
                "D6_PREREQUISITE_REQUIRED", "D6 requires a fresh read-only prerequisite proof"
            )
        d2_ledger = D2DailyCreditLedger(request.presubmit_request.runtime_state_root)
        if d2_ledger.path.is_symlink() or not d2_ledger.path.is_file():
            raise PhaseGError("D6_PREREQUISITE_NOT_FOUND", "authoritative D2 ledger is unavailable")
        ledgers = {str(ledger.path.resolve()): ledger for ledger in (self, d2_ledger)}
        with ExitStack() as stack:
            for key in sorted(ledgers):
                stack.enter_context(ledgers[key]._locked_file())
            payload = self._read()
            d2_payload = (
                payload if self.path.resolve() == d2_ledger.path.resolve() else d2_ledger._read()
            )
            try:
                d2_sha = sha256_file(d2_ledger.path)
            except OSError as exc:
                raise PhaseGError(
                    "D6_PREREQUISITE_NOT_FOUND", "authoritative D2 ledger could not be verified"
                ) from exc
            rows = cast(list[dict[str, object]], d2_payload["attempts"])
            release = _evaluate_d6_release_rows(
                prerequisite_id, request, rows, ledger_sha256=d2_sha
            )
            if not release.eligible or release.proof is None:
                failure = release.failure_code or "D6_PREREQUISITE_NOT_SUCCEEDED"
                raise PhaseGError(
                    failure, "identified D2 prerequisite does not authorize this D6 request"
                )
            if (
                release.proof.prerequisite_d2_run_id != proof.prerequisite_d2_run_id
                or release.proof.prerequisite_state != proof.prerequisite_state
                or release.proof.d2_fingerprint_sha256 != proof.d2_fingerprint_sha256
                or release.proof.d6_fingerprint_sha256 != proof.d6_fingerprint_sha256
                or release.proof.prerequisite_ledger_sha256 != proof.prerequisite_ledger_sha256
                or release.proof.contract_version != proof.contract_version
            ):
                raise PhaseGError(
                    "D6_PREREQUISITE_CHANGED",
                    "D2 prerequisite changed after eligibility evaluation",
                )
            attempts = cast(list[dict[str, object]], payload["attempts"])
            if any(row.get("d2_run_id") == request.d2_run_id for row in attempts):
                raise PhaseGError(
                    "PHASE_G_DUPLICATE_RUN_ID", "D6 run ID already exists in the ledger"
                )
            attempt = self._new_attempt(request)
            attempt["prerequisite_d2_run_id"] = release.proof.prerequisite_d2_run_id
            attempt["d6_prerequisite_proof"] = {
                "prerequisite_state": release.proof.prerequisite_state,
                "d2_fingerprint_sha256": release.proof.d2_fingerprint_sha256,
                "d6_fingerprint_sha256": release.proof.d6_fingerprint_sha256,
                "prerequisite_ledger_sha256": release.proof.prerequisite_ledger_sha256,
                "contract_version": release.proof.contract_version,
                "verified_at": release.proof.verified_at,
            }
            attempts.append(attempt)
            self._write(payload)
            return dict(attempt)

    def mark_generate_action(self, d2_run_id: str) -> dict[str, object]:
        """Durably consume the attempt's sole Generate action before clicking."""

        def update(payload: dict[str, object]) -> dict[str, object]:
            attempt = self._attempt(payload, d2_run_id)
            if _counter(attempt.get("generate_action_count")) != 0:
                raise PhaseGError(
                    "PHASE_G_DUPLICATE_SUBMIT_FORBIDDEN",
                    "this attempt already consumed its Generate action",
                )
            if attempt["submission_state"] != "PREPARED":
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_STATE_INVALID",
                    "attempt is not prepared for its one Generate action",
                )
            if attempt.get("baseline_captured") is not True:
                raise PhaseGError(
                    "PHASE_G_BASELINE_REQUIRED", "attempt has no durable pre-submit baseline"
                )
            today = datetime.now(_BANGKOK).date().isoformat()
            if self._accounted(payload, today) >= Decimal(self.daily_budget):
                raise PhaseGError(
                    "DAILY_FREE_CREDIT_BUDGET_EXHAUSTED",
                    "Bangkok-day accounting already reaches the D2 credit ceiling",
                )
            attempt["generate_action_count"] = 1
            attempt["submission_state"] = "GENERATE_ACTION_RECORDED"
            attempt["updated_at"] = _utc_now()
            return dict(attempt)

        return self._mutate(update)

    def record_submit_transport_evidence(
        self, d2_run_id: str, evidence: PhaseGSubmitTransportEvidence
    ) -> dict[str, object]:
        """Persist only allowlisted submit transport metadata, never wire payloads."""
        count = evidence.provider_submit_request_count
        if evidence.submission_surface not in {"AGENT_ONLY", "CLASSIC_MIGRATED"}:
            raise PhaseGError("PHASE_G_SUBMIT_EVIDENCE_INVALID", "submission surface is invalid")
        if (
            evidence.agent_turn_dispatched is not None
            and type(evidence.agent_turn_dispatched) is not bool
        ):
            raise PhaseGError("PHASE_G_SUBMIT_EVIDENCE_INVALID", "Agent turn flag is invalid")
        if evidence.submission_surface == "AGENT_ONLY" and evidence.agent_turn_dispatched is None:
            raise PhaseGError("PHASE_G_SUBMIT_EVIDENCE_INVALID", "Agent turn evidence is missing")
        if (
            evidence.submission_surface == "CLASSIC_MIGRATED"
            and evidence.agent_turn_dispatched is not None
        ):
            raise PhaseGError("PHASE_G_SUBMIT_EVIDENCE_INVALID", "unexpected Agent turn evidence")
        if type(count) is not int or count < 0:
            raise PhaseGError("PHASE_G_SUBMIT_EVIDENCE_INVALID", "request count is invalid")
        if type(evidence.provider_submit_rpc_observed) is not bool:
            raise PhaseGError("PHASE_G_SUBMIT_EVIDENCE_INVALID", "RPC observation flag is invalid")
        if evidence.provider_submit_rpc_observed != (count > 0):
            raise PhaseGError(
                "PHASE_G_SUBMIT_EVIDENCE_INVALID", "request count conflicts with RPC evidence"
            )
        if count and (
            evidence.provider_submit_rpc_id != "YhhmEf"
            or evidence.provider_submit_http_method != "POST"
            or not evidence.provider_submit_observed_at
            or not evidence.provider_submit_project_id
        ):
            raise PhaseGError(
                "PHASE_G_SUBMIT_EVIDENCE_INVALID", "submit request facts are incomplete"
            )
        if not count and any(
            value is not None
            for value in (
                evidence.provider_submit_rpc_id,
                evidence.provider_submit_observed_at,
                evidence.provider_submit_http_method,
                evidence.provider_submit_project_id,
            )
        ):
            raise PhaseGError(
                "PHASE_G_SUBMIT_EVIDENCE_INVALID", "unobserved request contains request metadata"
            )
        for flag in (
            evidence.generate_click_dispatched,
            evidence.provider_submit_dispatch_window_completed,
            evidence.provider_submit_request_failed,
            evidence.provider_submit_response_observed,
            evidence.provider_submit_generation_record_observed,
            evidence.provider_submit_refusal_detected,
        ):
            if flag is not None and type(flag) is not bool:
                raise PhaseGError("PHASE_G_SUBMIT_EVIDENCE_INVALID", "transport flag is invalid")
        if (
            evidence.provider_submit_observed_at is not None
            and type(evidence.provider_submit_observed_at) is not str
        ):
            raise PhaseGError("PHASE_G_SUBMIT_EVIDENCE_INVALID", "request timestamp is invalid")
        if evidence.provider_submit_response_status is not None and (
            type(evidence.provider_submit_response_status) is not int
            or not 100 <= evidence.provider_submit_response_status <= 599
        ):
            raise PhaseGError("PHASE_G_SUBMIT_EVIDENCE_INVALID", "response status is invalid")
        if evidence.provider_submit_response_observed is not True and (
            evidence.provider_submit_response_status is not None
            or evidence.provider_submit_generation_record_observed is True
            or evidence.provider_submit_refusal_detected is True
        ):
            raise PhaseGError(
                "PHASE_G_SUBMIT_EVIDENCE_INVALID", "response facts require an observed response"
            )

        def update(payload: dict[str, object]) -> dict[str, object]:
            attempt = self._attempt(payload, d2_run_id)
            if _counter(attempt.get("generate_action_count")) != 1:
                raise PhaseGError(
                    "PHASE_G_SUBMIT_EVIDENCE_INVALID",
                    "submit evidence requires the recorded one-time Generate action",
                )
            if count and evidence.provider_submit_project_id != attempt.get("provider_project_id"):
                raise PhaseGError(
                    "PHASE_G_PROJECT_MISMATCH", "submit request project is not pinned"
                )
            previous_count = _counter(attempt.get("provider_submit_request_count"))
            if count < previous_count:
                raise PhaseGError(
                    "PHASE_G_SUBMIT_EVIDENCE_INVALID", "submit request count cannot decrease"
                )
            if attempt.get("generate_click_dispatched") is True and (
                evidence.generate_click_dispatched is not True
            ):
                raise PhaseGError(
                    "PHASE_G_SUBMIT_EVIDENCE_INVALID", "click dispatch evidence cannot be cleared"
                )
            if attempt.get("provider_submit_dispatch_window_completed") is True and (
                evidence.provider_submit_dispatch_window_completed is not True
            ):
                raise PhaseGError(
                    "PHASE_G_SUBMIT_EVIDENCE_INVALID",
                    "dispatch window completion cannot be cleared",
                )
            if attempt.get("provider_submit_response_observed") is True and (
                evidence.provider_submit_response_observed is not True
            ):
                raise PhaseGError(
                    "PHASE_G_SUBMIT_EVIDENCE_INVALID", "response evidence cannot be cleared"
                )
            if attempt.get("provider_submit_generation_record_observed") is True and (
                evidence.provider_submit_generation_record_observed is not True
            ):
                raise PhaseGError(
                    "PHASE_G_SUBMIT_EVIDENCE_INVALID", "record evidence cannot be cleared"
                )
            if attempt.get("agent_turn_dispatched") is True and (
                evidence.agent_turn_dispatched is not True
            ):
                raise PhaseGError(
                    "PHASE_G_SUBMIT_EVIDENCE_INVALID", "Agent turn evidence cannot be cleared"
                )
            attempt.update(
                submission_surface=evidence.submission_surface,
                agent_turn_dispatched=evidence.agent_turn_dispatched,
                generate_click_dispatched=evidence.generate_click_dispatched,
                provider_submit_rpc_observed=evidence.provider_submit_rpc_observed,
                provider_submit_rpc_id=evidence.provider_submit_rpc_id,
                provider_submit_observed_at=evidence.provider_submit_observed_at,
                provider_submit_http_method=evidence.provider_submit_http_method,
                provider_submit_project_id=evidence.provider_submit_project_id,
                provider_submit_request_count=count,
                provider_submit_dispatch_window_completed=(
                    evidence.provider_submit_dispatch_window_completed
                ),
                provider_submit_request_failed=evidence.provider_submit_request_failed,
                provider_submit_response_observed=evidence.provider_submit_response_observed,
                provider_submit_response_status=evidence.provider_submit_response_status,
                provider_submit_generation_record_observed=(
                    evidence.provider_submit_generation_record_observed
                ),
                provider_submit_refusal_detected=evidence.provider_submit_refusal_detected,
                provider_submit_transport_classification=(
                    self._submit_transport_classification(evidence)
                ),
                updated_at=_utc_now(),
            )
            return dict(attempt)

        return self._mutate(update)

    @staticmethod
    def _submit_transport_classification(evidence: PhaseGSubmitTransportEvidence) -> str:
        if evidence.submission_surface == "AGENT_ONLY":
            if evidence.provider_submit_request_count > 1:
                return "DUPLICATE_SUBMIT_RPC"
            if evidence.agent_turn_dispatched is True:
                return "AGENT_TURN_DISPATCHED"
            if evidence.provider_submit_dispatch_window_completed is True:
                return "AGENT_TURN_NOT_OBSERVED"
            return "AGENT_TURN_DISPATCH_OBSERVATION_INCOMPLETE"
        if evidence.provider_submit_dispatch_window_completed is not True:
            return "SUBMIT_DISPATCH_OBSERVATION_INCOMPLETE"
        if evidence.provider_submit_request_count == 0:
            return "SUBMIT_RPC_NOT_OBSERVED"
        if evidence.provider_submit_request_count > 1:
            return "DUPLICATE_SUBMIT_RPC"
        if evidence.provider_submit_response_observed is not True:
            return "SUBMIT_SENT_RESPONSE_UNKNOWN"
        if evidence.provider_submit_refusal_detected is True:
            return "SUBMIT_RESPONSE_PROVIDER_REFUSAL"
        if evidence.provider_submit_generation_record_observed is True:
            return "SUBMIT_RESPONSE_WITH_GENERATION_RECORD"
        return "SUBMIT_RESPONSE_WITHOUT_GENERATION_RECORD"

    def capture_baseline(
        self, d2_run_id: str, baseline: FlowSubmissionBaseline
    ) -> dict[str, object]:
        """Persist the exact project baseline needed to reconcile without resubmitting."""

        def update(payload: dict[str, object]) -> dict[str, object]:
            attempt = self._attempt(payload, d2_run_id)
            if attempt["submission_state"] != "PREPARED" or attempt.get("baseline_captured"):
                raise PhaseGError(
                    "PHASE_G_BASELINE_INVALID", "baseline is stale or already captured"
                )
            if baseline.project_id != attempt["provider_project_id"] or baseline.pending_count:
                raise PhaseGError(
                    "PHASE_G_BASELINE_INVALID", "baseline project or pending state is invalid"
                )
            attempt.update(
                baseline_captured=True,
                baseline_media_ids=sorted(baseline.media_ids),
                baseline_execution_ids=sorted(baseline.execution_ids),
                baseline_pending_count=baseline.pending_count,
                baseline_confirmation_ids=sorted(baseline.confirmation_ids),
                baseline_agent_turn_id=baseline.agent_turn_id,
                baseline_agent_turn_bubble_hashes=list(baseline.agent_turn_bubble_hashes),
                baseline_finished_tile_key_hashes=list(baseline.finished_tile_key_hashes),
                baseline_chat_video_media_ids=sorted(baseline.chat_video_media_ids),
                updated_at=_utc_now(),
            )
            return dict(attempt)

        return self._mutate(update)

    def include_historical_provider_identities(
        self, d2_run_id: str, baseline: FlowSubmissionBaseline
    ) -> FlowSubmissionBaseline:
        """Exclude durable prior-attempt identities from this submit baseline."""
        with self._locked_file():
            payload = self._read()
            current = self._attempt(payload, d2_run_id)
            project_id = current.get("provider_project_id")
            if not isinstance(project_id, str) or baseline.project_id != project_id:
                raise PhaseGError(
                    "PHASE_G_BASELINE_INVALID", "baseline project does not match its D2 attempt"
                )
            historical_media_ids: set[str] = set()
            historical_execution_ids: set[str] = set()
            for attempt in cast(list[dict[str, object]], payload["attempts"]):
                if (
                    attempt.get("d2_run_id") == d2_run_id
                    or attempt.get("provider_project_id") != project_id
                ):
                    continue
                execution_id = attempt.get("provider_execution_id")
                if execution_id is not None:
                    if not isinstance(execution_id, str) or not execution_id:
                        raise PhaseGError(
                            "PHASE_G_LEDGER_INVALID",
                            "stored provider execution identity is invalid",
                        )
                    historical_execution_ids.add(execution_id.lower())
                media_id = attempt.get("provider_media_id")
                if media_id is not None:
                    if not isinstance(media_id, str) or not media_id:
                        raise PhaseGError(
                            "PHASE_G_LEDGER_INVALID", "stored provider media identity is invalid"
                        )
                    historical_media_ids.add(media_id.lower())
                media_ids = attempt.get("provider_media_ids")
                if not isinstance(media_ids, list):
                    raise PhaseGError(
                        "PHASE_G_LEDGER_INVALID", "stored provider media identities are invalid"
                    )
                for value in cast(list[object], media_ids):
                    if not isinstance(value, str) or not value:
                        raise PhaseGError(
                            "PHASE_G_LEDGER_INVALID",
                            "stored provider media identities are invalid",
                        )
                    historical_media_ids.add(value.lower())
            return replace(
                baseline,
                media_ids=baseline.media_ids | frozenset(historical_media_ids),
                execution_ids=baseline.execution_ids | frozenset(historical_execution_ids),
            )

    def approve_gate(
        self,
        d2_run_id: str,
        *,
        video_count: int | None,
        credit_cost: Decimal | int | float | str | None,
        confirmation_id: str,
        visible_balance: Decimal | int | float | str | None = None,
    ) -> dict[str, object]:
        """Validate the new gate and reserve its quote before one approval."""
        quoted = _decimal(credit_cost)
        balance = _decimal(visible_balance, allow_none=True)
        if video_count != 1:
            raise PhaseGError(
                "PHASE_G_VIDEO_COUNT_UNPROVEN", "confirmation must explicitly quote one video"
            )
        if quoted is None or quoted <= 0:
            raise PhaseGError("PHASE_G_CREDIT_COST_UNKNOWN", "confirmation credit cost is unknown")

        def update(payload: dict[str, object]) -> dict[str, object]:
            attempt = self._attempt(payload, d2_run_id)
            if _counter(attempt.get("generate_action_count")) != 1:
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_STATE_INVALID", "new gate has no recorded Generate action"
                )
            if _counter(attempt.get("approval_action_count")) != 0:
                raise PhaseGError(
                    "UNEXPECTED_SECOND_CONFIRMATION", "attempt already approved one confirmation"
                )
            late_reconciled = attempt.get("late_confirmation_reconciled") is True
            if attempt["submission_state"] != "GENERATE_ACTION_RECORDED" and not (
                attempt["submission_state"] == "UNKNOWN_SUBMISSION_STATE" and late_reconciled
            ):
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_STATE_INVALID",
                    "attempt is not awaiting its first confirmation",
                )
            baseline_confirmation_ids = attempt.get("baseline_confirmation_ids")
            if not isinstance(baseline_confirmation_ids, list):
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_STATE_INVALID", "stored baseline confirmations are invalid"
                )
            if (
                not confirmation_id
                or confirmation_id in cast(list[object], baseline_confirmation_ids)
                or attempt.get("observed_confirmation_id") != confirmation_id
                or attempt.get("confirmation_id") is not None
                or attempt.get("observed_video_count") != video_count
                or _decimal(attempt.get("observed_credit_cost"), allow_none=True) != quoted
                or _decimal(attempt.get("observed_visible_balance"), allow_none=True) != balance
            ):
                raise PhaseGError(
                    "PHASE_G_CONFIRMATION_NOT_ATTRIBUTABLE",
                    "confirmation is stale or this attempt already has a gate",
                )
            date = datetime.now(_BANGKOK).date().isoformat()
            cumulative = self._accounted(payload, date)
            if late_reconciled and attempt.get("accounting_date") == date:
                cumulative -= _decimal(attempt.get("accounted_credit_cost")) or Decimal(0)
            if cumulative + quoted > self.daily_budget:
                raise PhaseGError(
                    "DAILY_FREE_CREDIT_BUDGET_EXHAUSTED",
                    "quoted cost exceeds remaining D2 daily budget",
                )
            if balance is not None and quoted > balance:
                raise PhaseGError(
                    "PHASE_G_VISIBLE_BALANCE_INSUFFICIENT",
                    "quoted cost exceeds visible Flow balance",
                )
            attempt.update(
                approval_action_count=1,
                late_confirmation_reconciled=False,
                confirmation_id=confirmation_id,
                quoted_video_count=video_count,
                quoted_credit_cost=str(quoted),
                accounted_credit_cost=str(quoted),
                accounting_date=date,
                submission_state="COST_RESERVED_AWAITING_APPROVAL",
                updated_at=_utc_now(),
            )
            return dict(attempt)

        return self._mutate(update)

    def observe_confirmation(
        self,
        d2_run_id: str,
        *,
        confirmation_id: str,
        video_count: int | None,
        credit_cost: Decimal | int | float | str | None,
        visible_balance: Decimal | int | float | str | None,
    ) -> dict[str, object]:
        """Persist a fresh gate quote even when policy later refuses approval."""
        quoted = _decimal(credit_cost, allow_none=True)
        balance = _decimal(visible_balance, allow_none=True)

        def update(payload: dict[str, object]) -> dict[str, object]:
            attempt = self._attempt(payload, d2_run_id)
            baseline_ids = attempt.get("baseline_confirmation_ids")
            if not isinstance(baseline_ids, list):
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_STATE_INVALID", "stored baseline confirmations are invalid"
                )
            if (
                attempt.get("submission_state") != "GENERATE_ACTION_RECORDED"
                or not confirmation_id
                or confirmation_id in cast(list[object], baseline_ids)
                or attempt.get("observed_confirmation_id") is not None
            ):
                raise PhaseGError(
                    "PHASE_G_CONFIRMATION_NOT_ATTRIBUTABLE",
                    "confirmation is stale or a second gate was observed",
                )
            attempt.update(
                observed_confirmation_id=confirmation_id,
                observed_video_count=video_count,
                observed_credit_cost=str(quoted) if quoted is not None else None,
                observed_visible_balance=str(balance) if balance is not None else None,
                updated_at=_utc_now(),
            )
            return dict(attempt)

        return self._mutate(update)

    def record_late_confirmation(
        self,
        d2_run_id: str,
        *,
        expected_ledger_sha256: str,
        confirmation_id: str,
        project_id: str,
        video_count: int | None,
        credit_cost: Decimal | int | float | str | None,
        visible_balance: Decimal | int | float | str | None = None,
    ) -> dict[str, object]:
        """Reconcile one late gate against an exact non-terminal unknown attempt."""
        if not _SHA256_RE.fullmatch(expected_ledger_sha256):
            raise PhaseGError("PHASE_G_LEDGER_SHA_INVALID", "expected ledger SHA-256 is invalid")
        quoted = _decimal(credit_cost, allow_none=True)
        balance = _decimal(visible_balance, allow_none=True)
        if video_count != 1 or quoted is None or quoted <= 0:
            raise PhaseGError("PHASE_G_CREDIT_GATE_UNPROVEN", "late gate quote is not safe")
        with self._locked_file():
            if self.path.is_symlink() or not self.path.is_file():
                raise PhaseGError("PHASE_G_LEDGER_PATH_INVALID", "D2 ledger is missing or unsafe")
            if sha256_file(self.path) != expected_ledger_sha256:
                raise PhaseGError(
                    "PHASE_G_LEDGER_CHANGED", "D2 ledger changed since recovery review"
                )
            payload = self._read()
            attempt = self._attempt(payload, d2_run_id)
            baseline_ids = attempt.get("baseline_confirmation_ids")
            date = datetime.now(_BANGKOK).date().isoformat()
            if (
                attempt.get("submission_state") != "UNKNOWN_SUBMISSION_STATE"
                or attempt.get("terminal_state") is not None
                or _counter(attempt.get("generate_action_count")) != 1
                or _counter(attempt.get("approval_action_count")) != 0
                or attempt.get("provider_project_id") != project_id
                or not isinstance(baseline_ids, list)
                or attempt.get("baseline_captured") is not True
                or not confirmation_id
                or confirmation_id in baseline_ids
                or attempt.get("observed_confirmation_id") is not None
                or attempt.get("confirmation_id") is not None
                or attempt.get("provider_media_id") is not None
                or attempt.get("provider_execution_id") is not None
                or attempt.get("provider_media_ids")
                or attempt.get("accounting_date") != date
            ):
                raise PhaseGError(
                    "PHASE_G_LATE_CONFIRMATION_NOT_ATTRIBUTABLE",
                    "late gate does not match a recoverable one-submit unknown attempt",
                )
            current_date = attempt.get("accounting_date")
            previous_reservation = _decimal(attempt.get("accounted_credit_cost")) or Decimal(0)
            other_reservations = self._accounted(payload, date)
            if current_date == date:
                other_reservations -= previous_reservation
            if other_reservations + quoted > self.daily_budget:
                raise PhaseGError(
                    "DAILY_FREE_CREDIT_BUDGET_EXHAUSTED",
                    "late quote exceeds the remaining D2 daily budget",
                )
            if balance is not None and quoted > balance:
                raise PhaseGError(
                    "PHASE_G_VISIBLE_BALANCE_INSUFFICIENT",
                    "late quote exceeds visible Flow balance",
                )
            attempt.update(
                observed_confirmation_id=confirmation_id,
                observed_video_count=video_count,
                observed_credit_cost=str(quoted),
                observed_visible_balance=str(balance) if balance is not None else None,
                late_confirmation_reconciled=True,
                prior_unknown_reservation_credit_cost=str(previous_reservation),
                prior_unknown_reservation_accounting_date=current_date,
                quoted_video_count=video_count,
                quoted_credit_cost=str(quoted),
                accounted_credit_cost=str(quoted),
                accounting_date=date,
                provider_billing_outcome="UNKNOWN",
                updated_at=_utc_now(),
            )
            self._write(payload)
            return dict(attempt)

    def close_misattributed_generation_forensic(
        self,
        d2_run_id: str,
        *,
        expected_ledger_sha256: str,
        evidence_sha256: str,
        implementation_commit: str,
        evidence: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Close one historical-identity misattribution under its dedicated evidence contract."""
        if not _SHA256_RE.fullmatch(expected_ledger_sha256):
            raise PhaseGError("PHASE_G_LEDGER_SHA_INVALID", "expected ledger SHA-256 is invalid")
        if not _SHA256_RE.fullmatch(evidence_sha256):
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_HASH_INVALID", "evidence SHA-256 is invalid"
            )
        if not re.fullmatch(r"[0-9a-f]{40,64}", implementation_commit):
            raise PhaseGError(
                "PHASE_G_FORENSIC_COMMIT_INVALID", "implementation commit SHA is invalid"
            )

        from .phase_g_forensic_misattribution_closure import (
            forensic_misattribution_evidence_digest,
            validate_forensic_misattribution_evidence,
        )

        summary = validate_forensic_misattribution_evidence(evidence)
        if summary["D2_RUN_ID"] != d2_run_id:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INVALID", "evidence belongs to a different D2 run"
            )
        if summary["CANONICAL_LEDGER_SHA256"] != expected_ledger_sha256:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INVALID", "evidence names a different ledger SHA"
            )
        if summary["IMPLEMENTATION_COMMIT"] != implementation_commit:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INVALID", "evidence names a different implementation"
            )
        if forensic_misattribution_evidence_digest(summary) != evidence_sha256:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_HASH_MISMATCH",
                "evidence SHA differs from canonical evidence content",
            )

        with self._locked_file():
            if self.path.is_symlink() or not self.path.is_file():
                raise PhaseGError("PHASE_G_LEDGER_PATH_INVALID", "D2 ledger is missing or unsafe")
            if sha256_file(self.path) != expected_ledger_sha256:
                raise PhaseGError(
                    "PHASE_G_LEDGER_CHANGED", "D2 ledger changed since operator review"
                )
            payload = self._read()
            matches = [
                row
                for row in cast(list[dict[str, object]], payload["attempts"])
                if row.get("d2_run_id") == d2_run_id
            ]
            if len(matches) != 1:
                raise PhaseGError("PHASE_G_ATTEMPT_NOT_UNIQUE", "D2 run ID must exist exactly once")
            attempt = matches[0]
            if attempt.get("submission_state") != "GENERATION_SUCCEEDED" or attempt.get(
                "terminal_state"
            ) not in (None, "GENERATION_SUCCEEDED"):
                raise PhaseGError(
                    "PHASE_G_FORENSIC_STATE_MISMATCH",
                    "misattribution closure requires nonterminal GENERATION_SUCCEEDED",
                )
            self.validate_misattributed_attempt(self.root.parent, payload, attempt, summary)
            attempt.update(
                submission_state="FORENSICALLY_CLOSED_UNKNOWN",
                terminal_state="FORENSICALLY_CLOSED_UNKNOWN",
                provider_billing_outcome="UNKNOWN",
                provider_outcome="UNKNOWN",
                credit_outcome="UNKNOWN",
                attribution_invalidated=True,
                artifact_attribution_valid=False,
                forensic_closure_contract_version="D2_FORENSIC_MISATTRIBUTION_CLOSURE_V2",
                forensic_classification="NO_NEW_RETRY_OUTPUT_PROVEN",
                forensic_closed_at=summary["FORENSIC_COMPLETED_AT"],
                forensic_reason=summary["FORENSIC_REASON"],
                forensic_evidence_sha256=evidence_sha256,
                forensic_implementation_commit=implementation_commit,
                forensic_closure_evidence=summary,
                updated_at=_utc_now(),
            )
            self._write(payload)
            return dict(attempt)

    @staticmethod
    def validate_misattributed_attempt(
        runtime_state_root: Path,
        payload: Mapping[str, object],
        attempt: Mapping[str, object],
        evidence: Mapping[str, object],
    ) -> None:
        terminal_state = attempt.get("terminal_state")
        if (
            attempt.get("provider_account_id") != "PVA_0001"
            or attempt.get("provider_project_id") != "34e60953-e534-46e9-ac65-993893e61065"
            or evidence.get("PROVIDER_ACCOUNT_ID") != attempt.get("provider_account_id")
            or evidence.get("PROVIDER_PROJECT_ID") != attempt.get("provider_project_id")
        ):
            raise PhaseGError(
                "PHASE_G_FORENSIC_IDENTITY_MISMATCH",
                "attempt is not the exact TEST account/project",
            )
        if (
            attempt.get("submission_state") != "GENERATION_SUCCEEDED"
            or terminal_state not in (None, "GENERATION_SUCCEEDED")
            or terminal_state in _TERMINAL_STATES
            or evidence.get("CURRENT_SUBMISSION_STATE") != "GENERATION_SUCCEEDED"
            or evidence.get("CURRENT_TERMINAL_STATE")
            != ("NONE" if terminal_state is None else terminal_state)
        ):
            raise PhaseGError(
                "PHASE_G_FORENSIC_STATE_MISMATCH",
                "evidence must bind the current nonterminal generation marker",
            )
        approval_count = attempt.get("approval_action_count", 0)
        generate_count = attempt.get("generate_action_count")
        if (
            isinstance(generate_count, bool)
            or generate_count != 1
            or isinstance(approval_count, bool)
            or not isinstance(approval_count, int)
            or approval_count > 1
            or evidence.get("GENERATE_ACTION_COUNT") != 1
            or evidence.get("APPROVAL_ACTION_COUNT") != approval_count
        ):
            raise PhaseGError(
                "PHASE_G_GENERATE_COUNT_MISMATCH", "misattribution evidence action counts differ"
            )
        authorization_value = attempt.get("retry_authorization")
        if not isinstance(authorization_value, dict):
            raise PhaseGError(
                "PHASE_G_RETRY_AUTHORIZATION_INVALID",
                "exact consumed retry authorization is required",
            )
        authorization = cast(dict[str, object], authorization_value)
        if (
            authorization.get("contract_version") != 1
            or authorization.get("predecessor_d2_run_id") != "D2_2aebc5af59bd423b"
            or authorization.get("expected_terminal_state") != "UNEXPECTED_MULTI_OUTPUT"
            or not isinstance(authorization.get("consumed_at"), str)
            or not authorization.get("consumed_at")
            or evidence.get("RETRY_AUTHORIZATION_PRESENT") is not True
            or evidence.get("RETRY_PREDECESSOR") != authorization.get("predecessor_d2_run_id")
        ):
            raise PhaseGError(
                "PHASE_G_RETRY_AUTHORIZATION_INVALID",
                "exact consumed retry authorization is required",
            )
        attempt_rows = cast(list[dict[str, object]], payload["attempts"])
        predecessor_rows = [
            row
            for row in attempt_rows
            if row.get("d2_run_id") == authorization.get("predecessor_d2_run_id")
        ]
        current_indexes = [
            index
            for index, row in enumerate(attempt_rows)
            if row.get("d2_run_id") == attempt.get("d2_run_id")
        ]
        if len(current_indexes) != 1 or len(predecessor_rows) != 1:
            raise PhaseGError(
                "PHASE_G_RETRY_PREDECESSOR_MISMATCH",
                "retry and predecessor IDs must each exist exactly once",
            )
        predecessor = predecessor_rows[0]
        if attempt_rows.index(predecessor) >= current_indexes[0]:
            raise PhaseGError(
                "PHASE_G_RETRY_PREDECESSOR_MISMATCH",
                "retry predecessor must be an earlier ledger attempt",
            )
        if (
            predecessor.get("provider_project_id") != attempt.get("provider_project_id")
            or predecessor.get("submission_state") != "UNEXPECTED_MULTI_OUTPUT"
            or predecessor.get("terminal_state") != "UNEXPECTED_MULTI_OUTPUT"
        ):
            raise PhaseGError(
                "PHASE_G_RETRY_PREDECESSOR_MISMATCH",
                "retry predecessor is not the exact same-project failure",
            )
        prior_rows = [
            row
            for row in attempt_rows[: current_indexes[0]]
            if row.get("provider_project_id") == attempt.get("provider_project_id")
        ]
        execution_ids = {
            row.get("provider_execution_id")
            for row in prior_rows
            if isinstance(row.get("provider_execution_id"), str)
        }
        media_ids: set[str] = set()
        for row in prior_rows:
            prior_media_id = row.get("provider_media_id")
            if isinstance(prior_media_id, str):
                media_ids.add(prior_media_id)
            prior_media_ids_value = row.get("provider_media_ids", [])
            if not isinstance(prior_media_ids_value, list) or any(
                not isinstance(identity, str)
                for identity in cast(list[object], prior_media_ids_value)
            ):
                raise PhaseGError(
                    "PHASE_G_LEDGER_INVALID", "historical provider media identities are invalid"
                )
            media_ids.update(cast(list[str], prior_media_ids_value))
        recorded_execution = attempt.get("provider_execution_id")
        recorded_media = attempt.get("provider_media_id")
        recorded_media_ids_value = attempt.get("provider_media_ids", [])
        if not isinstance(recorded_media_ids_value, list) or any(
            not isinstance(identity, str)
            for identity in cast(list[object], recorded_media_ids_value)
        ):
            raise PhaseGError(
                "PHASE_G_LEDGER_INVALID", "recorded provider media identities are invalid"
            )
        recorded_media_ids = cast(list[str], recorded_media_ids_value)
        if (
            not isinstance(recorded_execution, str)
            or recorded_execution not in execution_ids
            or not isinstance(recorded_media, str)
            or recorded_media not in media_ids
            or not recorded_media_ids
            or any(identity not in media_ids for identity in recorded_media_ids)
            or evidence.get("RECORDED_PROVIDER_MEDIA_IDS") != recorded_media_ids
            or evidence.get("RECORDED_PROVIDER_EXECUTION_ID") != recorded_execution
            or evidence.get("RECORDED_PROVIDER_MEDIA_ID") != recorded_media
            or evidence.get("RECORDED_PROVIDER_IDS_HISTORICAL") is not True
            or evidence.get("HISTORICAL_EXECUTION_MATCH") is not True
            or evidence.get("HISTORICAL_MEDIA_MATCH") is not True
            or attempt.get("provider_outcome", "UNKNOWN") != "UNKNOWN"
            or attempt.get("credit_outcome", "UNKNOWN") != "UNKNOWN"
            or attempt.get("provider_billing_outcome") != "UNKNOWN"
            or evidence.get("ACCOUNTED_CREDIT_COST_EXPECTED")
            != attempt.get("accounted_credit_cost")
            or evidence.get("ACCOUNTING_DATE_EXPECTED") != attempt.get("accounting_date")
        ):
            raise PhaseGError(
                "PHASE_G_PROVIDER_OUTCOME_PRESENT", "recorded identities are not proven historical"
            )
        if (
            evidence.get("CANDIDATE_NEW_EXECUTION_COUNT") != 0
            or evidence.get("CANDIDATE_NEW_MEDIA_COUNT") != 0
            or evidence.get("POST_BASELINE_NEW_MEDIA_ASSOCIATION") is not False
            or evidence.get("ATTRIBUTION_CLASSIFICATION") != "NO_NEW_RETRY_OUTPUT_PROVEN"
            or evidence.get("REMOTE_UPLOAD_OCCURRED") is not False
            or evidence.get("REMOTE_VERIFIED") is not False
            or attempt.get("remote_verified") is True
            or attempt.get("remote_file_id") is not None
            or attempt.get("submission_state") == "D2_SUCCEEDED"
        ):
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
                "new output or verified remote artifact prevents misattribution closure",
            )
        journal = AgentSettingsRecoveryJournal(
            runtime_state_root,
            str(attempt.get("provider_account_id")),
            str(attempt.get("provider_project_id")),
        )
        try:
            record = journal.read()
        except RecoveryJournalError as exc:
            raise PhaseGError(
                "PHASE_G_RECOVERY_JOURNAL_UNCLEAN", "recovery journal is invalid or unreadable"
            ) from exc
        if (
            record is None
            or record.state != "CLEAN"
            or evidence.get("RECOVERY_JOURNAL_CLEAN") is not True
        ):
            raise PhaseGError("PHASE_G_RECOVERY_JOURNAL_UNCLEAN", "recovery journal must be CLEAN")

    def record_state(
        self,
        d2_run_id: str,
        *,
        submission_state: str,
        terminal_state: str | None = None,
        provider_execution_id: str | None = None,
        provider_media_id: str | None = None,
        provider_media_ids: Sequence[str] | None = None,
        provider_project_id: str | None = None,
        accounted_credit_cost: Decimal | int | float | str | None = None,
    ) -> dict[str, object]:
        """Persist sanitized provider evidence and conservatively reserve charges."""
        if submission_state not in _SUBMISSION_STATES:
            raise PhaseGError("PHASE_G_STATE_INVALID", "submission state is not recognized")
        if terminal_state is not None and terminal_state not in _SUBMISSION_STATES:
            raise PhaseGError("PHASE_G_STATE_INVALID", "terminal state is not recognized")
        cost = _decimal(accounted_credit_cost, allow_none=True)

        def update(payload: dict[str, object]) -> dict[str, object]:
            attempt = self._attempt(payload, d2_run_id)
            current_state = str(attempt["submission_state"])
            if submission_state == "FORENSICALLY_CLOSED_UNKNOWN":
                raise PhaseGError(
                    "PHASE_G_FORENSIC_OPERATOR_REQUIRED",
                    "forensic closure requires the SHA-guarded evidence operator",
                )
            if current_state in _TERMINAL_STATES:
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_TERMINAL", "terminal D2 attempt cannot be changed"
                )
            allowed_next = {
                "PREPARED": {"PREPARED"},
                "GENERATE_ACTION_RECORDED": {
                    "GENERATE_ACTION_RECORDED",
                    "SUBMISSION_ACKNOWLEDGED",
                    "GENERATION_PENDING",
                    "GENERATION_SUCCEEDED",
                    "PROVIDER_REFUSED",
                    "WAF_REJECTED",
                    "UNKNOWN_SUBMISSION_STATE",
                    "RECONCILED_NO_CHARGE",
                },
                "COST_RESERVED_AWAITING_APPROVAL": {
                    "COST_RESERVED_AWAITING_APPROVAL",
                    "SUBMISSION_ACKNOWLEDGED",
                    "GENERATION_PENDING",
                    "GENERATION_SUCCEEDED",
                    "PROVIDER_REFUSED",
                    "WAF_REJECTED",
                    "GENERATION_FAILED",
                    "UNEXPECTED_MULTI_OUTPUT",
                    "MEDIA_VALIDATION_FAILED",
                    "PERSISTENCE_FAILED",
                    "UNKNOWN_SUBMISSION_STATE",
                },
                "SUBMISSION_ACKNOWLEDGED": {
                    "SUBMISSION_ACKNOWLEDGED",
                    "GENERATION_PENDING",
                    "GENERATION_SUCCEEDED",
                    "GENERATION_FAILED",
                    "UNEXPECTED_MULTI_OUTPUT",
                    "MEDIA_VALIDATION_FAILED",
                    "PERSISTENCE_FAILED",
                    "UNKNOWN_SUBMISSION_STATE",
                },
                "GENERATION_PENDING": {
                    "GENERATION_PENDING",
                    "GENERATION_SUCCEEDED",
                    "GENERATION_FAILED",
                    "UNEXPECTED_MULTI_OUTPUT",
                    "MEDIA_VALIDATION_FAILED",
                    "PERSISTENCE_FAILED",
                    "UNKNOWN_SUBMISSION_STATE",
                },
                "UNKNOWN_SUBMISSION_STATE": {
                    "UNKNOWN_SUBMISSION_STATE",
                    "SUBMISSION_ACKNOWLEDGED",
                    "GENERATION_PENDING",
                    "GENERATION_SUCCEEDED",
                    "PROVIDER_REFUSED",
                    "WAF_REJECTED",
                    "GENERATION_FAILED",
                    "UNEXPECTED_MULTI_OUTPUT",
                    "MEDIA_VALIDATION_FAILED",
                    "PERSISTENCE_FAILED",
                    "RECONCILED_NO_CHARGE",
                    "FORENSICALLY_CLOSED_UNKNOWN",
                },
                "GENERATION_SUCCEEDED": {
                    "GENERATION_SUCCEEDED",
                    "D2_SUCCEEDED",
                    "MEDIA_VALIDATION_FAILED",
                    "PERSISTENCE_FAILED",
                },
            }
            if submission_state not in allowed_next.get(current_state, set()):
                raise PhaseGError(
                    "PHASE_G_STATE_TRANSITION_INVALID", "D2 attempt state transition is not allowed"
                )
            if provider_project_id and provider_project_id != attempt["provider_project_id"]:
                raise PhaseGError(
                    "PHASE_G_PROJECT_MISMATCH", "provider evidence names a different project"
                )
            for key, value in (
                ("provider_execution_id", provider_execution_id),
                ("provider_media_id", provider_media_id),
                ("provider_project_id", provider_project_id),
            ):
                if value is not None:
                    attempt[key] = value
            if provider_media_ids is not None:
                if any(not media_id for media_id in provider_media_ids):
                    raise PhaseGError(
                        "PHASE_G_MEDIA_ID_INVALID", "provider media identity is invalid"
                    )
                attempt["provider_media_ids"] = list(provider_media_ids)
            if cost is not None:
                if attempt.get("accounting_date") is None and cost > 0:
                    attempt["accounting_date"] = datetime.now(_BANGKOK).date().isoformat()
                attempt["accounted_credit_cost"] = str(cost)
            if submission_state in {
                "UNKNOWN_SUBMISSION_STATE",
                "SUBMISSION_ACKNOWLEDGED",
                "GENERATION_PENDING",
                "GENERATION_SUCCEEDED",
                "GENERATION_FAILED",
                "UNEXPECTED_MULTI_OUTPUT",
                "MEDIA_VALIDATION_FAILED",
                "PERSISTENCE_FAILED",
            }:
                # Once a submit may have reached Flow, reserve the entire
                # remaining task budget when no trustworthy quote was captured.
                # This blocks accidental same-day paid retries until reconciled.
                date = attempt.get("accounting_date") or datetime.now(_BANGKOK).date().isoformat()
                attempt["accounting_date"] = date
                current = _decimal(attempt.get("accounted_credit_cost")) or Decimal(0)
                if current == 0:
                    already = self._accounted(payload, str(date))
                    attempt["accounted_credit_cost"] = str(
                        max(Decimal(0), Decimal(self.daily_budget) - already)
                    )
                attempt["provider_billing_outcome"] = "UNKNOWN"
            attempt["submission_state"] = submission_state
            attempt["terminal_state"] = terminal_state
            attempt["updated_at"] = _utc_now()
            return dict(attempt)

        return self._mutate(update)

    def close_unknown_forensic(
        self,
        d2_run_id: str,
        *,
        expected_ledger_sha256: str,
        evidence_sha256: str,
        implementation_commit: str,
        evidence: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Permanently close an irrecoverable unknown attempt without claiming billing outcome."""
        if not _SHA256_RE.fullmatch(expected_ledger_sha256):
            raise PhaseGError("PHASE_G_LEDGER_SHA_INVALID", "expected ledger SHA-256 is invalid")
        if not _SHA256_RE.fullmatch(evidence_sha256):
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_HASH_INVALID", "evidence SHA-256 is invalid"
            )
        if not re.fullmatch(r"[0-9a-f]{40,64}", implementation_commit):
            raise PhaseGError(
                "PHASE_G_FORENSIC_COMMIT_INVALID", "implementation commit SHA is invalid"
            )

        from .phase_g_forensic_closure import (
            forensic_evidence_digest,
            validate_forensic_evidence,
        )

        summary = validate_forensic_evidence(evidence)
        if summary["D2_RUN_ID"] != d2_run_id:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INVALID", "evidence belongs to a different D2 run"
            )
        if summary["CANONICAL_LEDGER_SHA256"] != expected_ledger_sha256:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INVALID",
                "evidence names a different canonical ledger SHA",
            )
        if forensic_evidence_digest(summary) != evidence_sha256:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_HASH_MISMATCH",
                "forensic evidence SHA differs from canonical evidence content",
            )
        with self._locked_file():
            if self.path.is_symlink() or not self.path.is_file():
                raise PhaseGError("PHASE_G_LEDGER_PATH_INVALID", "D2 ledger is missing or unsafe")
            try:
                current_sha256 = sha256_file(self.path)
            except OSError as exc:
                raise PhaseGError(
                    "PHASE_G_LEDGER_READ_FAILED", "D2 ledger could not be hashed"
                ) from exc
            if current_sha256 != expected_ledger_sha256:
                raise PhaseGError(
                    "PHASE_G_LEDGER_CHANGED", "D2 ledger changed since operator review"
                )
            payload = self._read()
            attempt = self._attempt(payload, d2_run_id)
            if attempt.get("submission_state") != "UNKNOWN_SUBMISSION_STATE":
                raise PhaseGError(
                    "PHASE_G_FORENSIC_STATE_MISMATCH",
                    "forensic closure requires UNKNOWN_SUBMISSION_STATE",
                )
            if attempt.get("terminal_state") is not None:
                raise PhaseGError("PHASE_G_ATTEMPT_TERMINAL", "D2 attempt is already terminal")
            if attempt.get("provider_media_id") or attempt.get("provider_execution_id"):
                raise PhaseGError(
                    "PHASE_G_PROVIDER_OUTCOME_PRESENT",
                    "provider identities require provider-outcome reconciliation",
                )
            if attempt.get("provider_media_ids"):
                raise PhaseGError(
                    "PHASE_G_PROVIDER_OUTCOME_PRESENT",
                    "provider media identities require provider-outcome reconciliation",
                )
            if _counter(attempt.get("generate_action_count")) != 1:
                raise PhaseGError(
                    "PHASE_G_GENERATE_COUNT_MISMATCH",
                    "forensic closure requires exactly one Generate action",
                )
            if attempt.get("accounted_credit_cost") is None:
                raise PhaseGError(
                    "PHASE_G_ACCOUNTING_MISSING", "historical credit reservation is missing"
                )
            attempt.update(
                submission_state="FORENSICALLY_CLOSED_UNKNOWN",
                terminal_state="FORENSICALLY_CLOSED_UNKNOWN",
                provider_billing_outcome="UNKNOWN",
                provider_outcome="UNKNOWN",
                credit_outcome="UNKNOWN",
                forensic_closed_at=summary["FORENSIC_COMPLETED_AT"],
                forensic_reason=summary["FORENSIC_REASON"],
                forensic_evidence_sha256=evidence_sha256,
                forensic_implementation_commit=implementation_commit,
                forensic_closure_evidence=summary,
                updated_at=_utc_now(),
            )
            self._write(payload)
            return dict(attempt)

    def close_unknown_forensic_v2(
        self,
        d2_run_id: str,
        *,
        expected_ledger_sha256: str,
        evidence_sha256: str,
        implementation_commit: str,
        evidence: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Close a chat-only unknown attempt under the separate V2 evidence contract."""
        if not _SHA256_RE.fullmatch(expected_ledger_sha256):
            raise PhaseGError("PHASE_G_LEDGER_SHA_INVALID", "expected ledger SHA-256 is invalid")
        if not _SHA256_RE.fullmatch(evidence_sha256):
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_HASH_INVALID", "evidence SHA-256 is invalid"
            )
        if not re.fullmatch(r"[0-9a-f]{40,64}", implementation_commit):
            raise PhaseGError(
                "PHASE_G_FORENSIC_COMMIT_INVALID", "implementation commit SHA is invalid"
            )

        from .phase_g_forensic_closure_v2 import (
            forensic_evidence_digest_v2,
            validate_forensic_evidence_v2,
        )

        summary = validate_forensic_evidence_v2(evidence)
        if summary["D2_RUN_ID"] != d2_run_id:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INVALID", "evidence belongs to a different D2 run"
            )
        if summary["CANONICAL_LEDGER_SHA256"] != expected_ledger_sha256:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INVALID",
                "evidence names a different canonical ledger SHA",
            )
        if forensic_evidence_digest_v2(summary) != evidence_sha256:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_HASH_MISMATCH",
                "V2 evidence SHA differs from canonical evidence content",
            )
        with self._locked_file():
            if self.path.is_symlink() or not self.path.is_file():
                raise PhaseGError("PHASE_G_LEDGER_PATH_INVALID", "D2 ledger is missing or unsafe")
            try:
                current_sha256 = sha256_file(self.path)
            except OSError as exc:
                raise PhaseGError(
                    "PHASE_G_LEDGER_READ_FAILED", "D2 ledger could not be hashed"
                ) from exc
            if current_sha256 != expected_ledger_sha256:
                raise PhaseGError(
                    "PHASE_G_LEDGER_CHANGED", "D2 ledger changed since operator review"
                )
            payload = self._read()
            attempt = self._attempt(payload, d2_run_id)
            if attempt.get("submission_state") != "UNKNOWN_SUBMISSION_STATE":
                raise PhaseGError(
                    "PHASE_G_FORENSIC_STATE_MISMATCH",
                    "V2 forensic closure requires UNKNOWN_SUBMISSION_STATE",
                )
            if attempt.get("terminal_state") is not None:
                raise PhaseGError("PHASE_G_ATTEMPT_TERMINAL", "D2 attempt is already terminal")
            if (
                attempt.get("provider_media_id")
                or attempt.get("provider_execution_id")
                or attempt.get("provider_media_ids")
            ):
                raise PhaseGError(
                    "PHASE_G_PROVIDER_OUTCOME_PRESENT",
                    "provider identities require provider-outcome reconciliation",
                )
            generate_count = attempt.get("generate_action_count")
            approval_count = attempt.get("approval_action_count")
            if isinstance(generate_count, bool) or generate_count != 1:
                raise PhaseGError(
                    "PHASE_G_GENERATE_COUNT_MISMATCH",
                    "V2 forensic closure requires exactly one Generate action",
                )
            if (
                isinstance(approval_count, bool)
                or not isinstance(approval_count, int)
                or approval_count > 1
                or approval_count != summary["APPROVAL_ACTION_COUNT"]
            ):
                raise PhaseGError(
                    "PHASE_G_GENERATE_COUNT_MISMATCH",
                    "V2 approval count differs from persisted attempt",
                )
            if attempt.get("provider_billing_outcome") != "UNKNOWN":
                raise PhaseGError(
                    "PHASE_G_FORENSIC_STATE_MISMATCH",
                    "provider billing outcome must remain UNKNOWN",
                )
            if (
                attempt.get("provider_outcome", "UNKNOWN") != "UNKNOWN"
                or attempt.get("credit_outcome", "UNKNOWN") != "UNKNOWN"
            ):
                raise PhaseGError(
                    "PHASE_G_FORENSIC_STATE_MISMATCH",
                    "provider and credit outcomes must remain UNKNOWN",
                )
            if attempt.get("accounted_credit_cost") is None:
                raise PhaseGError(
                    "PHASE_G_ACCOUNTING_MISSING", "historical credit reservation is missing"
                )
            if (
                attempt.get("accounted_credit_cost") != summary["ACCOUNTED_CREDIT_COST_EXPECTED"]
                or attempt.get("prompt_sha256") != summary["PROMPT_SHA256"]
                or attempt.get("baseline_agent_turn_id") != summary["BASELINE_AGENT_TURN_ID"]
                or attempt.get("baseline_confirmation_ids") != summary["BASELINE_CONFIRMATION_IDS"]
            ):
                raise PhaseGError(
                    "PHASE_G_FORENSIC_EVIDENCE_INVALID",
                    "V2 evidence differs from persisted attempt",
                )
            baseline_hashes = attempt.get("baseline_agent_turn_bubble_hashes")
            if baseline_hashes is None and summary["BASELINE_CHAT_COUNT"] == 0:
                baseline_hashes = []
            if baseline_hashes != summary["BASELINE_CHAT_BUBBLE_SHA256S"]:
                raise PhaseGError(
                    "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
                    "persisted ordered baseline chat hashes are missing or differ",
                )
            attempt.update(
                submission_state="FORENSICALLY_CLOSED_UNKNOWN",
                terminal_state="FORENSICALLY_CLOSED_UNKNOWN",
                provider_outcome="UNKNOWN",
                credit_outcome="UNKNOWN",
                provider_billing_outcome="UNKNOWN",
                forensic_closure_contract_version="D2_FORENSIC_CLOSURE_V2",
                forensic_classification="CHAT_ONLY_UNRESOLVED_ACTIVITY",
                forensic_closed_at=summary["FORENSIC_COMPLETED_AT"],
                forensic_reason=summary["FORENSIC_REASON"],
                forensic_evidence_sha256=evidence_sha256,
                forensic_implementation_commit=implementation_commit,
                forensic_closure_evidence=summary,
                updated_at=_utc_now(),
            )
            self._write(payload)
            return dict(attempt)

    def close_provider_silent_forensic_v3(
        self,
        d2_run_id: str,
        *,
        expected_ledger_sha256: str,
        evidence_sha256: str,
        implementation_commit: str,
        evidence: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Close the exact provider-silent unknown D2 under the V3 evidence contract."""
        if not _SHA256_RE.fullmatch(expected_ledger_sha256):
            raise PhaseGError("PHASE_G_LEDGER_SHA_INVALID", "expected ledger SHA-256 is invalid")
        if not _SHA256_RE.fullmatch(evidence_sha256):
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_HASH_INVALID", "evidence SHA-256 is invalid"
            )
        if not re.fullmatch(r"[0-9a-f]{40,64}", implementation_commit):
            raise PhaseGError(
                "PHASE_G_FORENSIC_COMMIT_INVALID", "implementation commit SHA is invalid"
            )

        from .phase_g_forensic_provider_silent_v3 import (
            forensic_provider_silent_digest_v3,
            validate_provider_silent_attempt_binding_v3,
            validate_provider_silent_evidence_v3,
        )

        summary = validate_provider_silent_evidence_v3(evidence)
        if (
            summary["D2_RUN_ID"] != d2_run_id
            or summary["CANONICAL_LEDGER_SHA256"] != expected_ledger_sha256
            or summary["IMPLEMENTATION_COMMIT"] != implementation_commit
        ):
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INVALID", "V3 evidence binding differs from operator"
            )
        if forensic_provider_silent_digest_v3(summary) != evidence_sha256:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_HASH_MISMATCH",
                "V3 evidence SHA differs from canonical evidence content",
            )
        with self._locked_file():
            if self.path.is_symlink() or not self.path.is_file():
                raise PhaseGError("PHASE_G_LEDGER_PATH_INVALID", "D2 ledger is missing or unsafe")
            try:
                current_sha256 = sha256_file(self.path)
            except OSError as exc:
                raise PhaseGError(
                    "PHASE_G_LEDGER_READ_FAILED", "D2 ledger could not be hashed"
                ) from exc
            if current_sha256 != expected_ledger_sha256:
                raise PhaseGError(
                    "PHASE_G_LEDGER_CHANGED", "D2 ledger changed since V3 operator review"
                )
            payload = self._read()
            attempt = self._attempt(payload, d2_run_id)
            validate_provider_silent_attempt_binding_v3(self.root.parent, payload, attempt, summary)
            attempt.update(
                submission_state="FORENSICALLY_CLOSED_UNKNOWN",
                terminal_state="FORENSICALLY_CLOSED_UNKNOWN",
                provider_outcome="UNKNOWN",
                credit_outcome="UNKNOWN",
                provider_billing_outcome="UNKNOWN",
                forensic_closure_contract_version="D2_FORENSIC_PROVIDER_SILENT_CLOSURE_V3",
                forensic_classification="NO_PROVIDER_EVIDENCE",
                forensic_closed_at=summary["FORENSIC_COMPLETED_AT"],
                forensic_reason=summary["FORENSIC_REASON"],
                forensic_evidence_sha256=evidence_sha256,
                forensic_implementation_commit=implementation_commit,
                forensic_closure_evidence=summary,
                updated_at=_utc_now(),
            )
            self._write(payload)
            return dict(attempt)

    def attempts(self) -> tuple[Mapping[str, object], ...]:
        """Return a defensive snapshot of the sanitized attempt records."""
        with self._locked_file():
            payload = self._read()
            rows = cast(list[dict[str, object]], payload["attempts"])
            return tuple({key: value for key, value in row.items()} for row in rows)

    def d6_prerequisite_snapshot(
        self,
    ) -> tuple[tuple[Mapping[str, object], ...], str] | None:
        """Read authoritative D2 rows and their digest under one shared lock."""
        with self._locked_file():
            if self.path.is_symlink() or not self.path.is_file():
                return None
            try:
                digest = sha256_file(self.path)
            except OSError as exc:
                raise PhaseGError(
                    "PHASE_G_LEDGER_READ_FAILED", "D2 ledger could not be hashed"
                ) from exc
            payload = self._read()
            rows = cast(list[dict[str, object]], payload["attempts"])
            return tuple({key: value for key, value in row.items()} for row in rows), digest

    def record_artifact_recovered(self, d2_run_id: str) -> Mapping[str, object]:
        """Resume the same run after its exact stored media ID is revalidated."""

        def update(payload: dict[str, object]) -> dict[str, object]:
            attempt = self._attempt(payload, d2_run_id)
            if attempt.get("submission_state") not in {
                "PERSISTENCE_FAILED",
                "MEDIA_VALIDATION_FAILED",
            } or not attempt.get("provider_media_id"):
                raise PhaseGError(
                    "PHASE_G_ARTIFACT_RECOVERY_REQUIRED",
                    "only an already generated media identity can be recovered",
                )
            attempt["submission_state"] = "GENERATION_SUCCEEDED"
            attempt["terminal_state"] = None
            attempt["updated_at"] = _utc_now()
            return dict(attempt)

        return self._mutate(update)

    def record_local_artifact_validated(
        self, d2_run_id: str, *, path: Path, size_bytes: int, sha256: str
    ) -> Mapping[str, object]:
        """Persist the validated local clip identity for later Drive finalization."""
        if (
            not path.is_absolute()
            or not path.is_file()
            or path.is_symlink()
            or isinstance(size_bytes, bool)
            or size_bytes <= 0
            or not _SHA256_RE.fullmatch(sha256)
            or path.stat().st_size != size_bytes
        ):
            raise PhaseGError(
                "PHASE_G_LOCAL_ARTIFACT_INVALID", "validated local artifact identity is invalid"
            )
        actual_sha256 = sha256_file(path)
        if actual_sha256 != sha256:
            raise PhaseGError(
                "PHASE_G_LOCAL_ARTIFACT_INVALID", "validated artifact SHA-256 does not match file"
            )

        def update(payload: dict[str, object]) -> dict[str, object]:
            attempt = self._attempt(payload, d2_run_id)
            if attempt.get("submission_state") != "GENERATION_SUCCEEDED":
                raise PhaseGError(
                    "PHASE_G_ARTIFACT_RECOVERY_REQUIRED",
                    "attributed generation must succeed before recording local media",
                )
            temp_root = Path(str(attempt.get("temp_root", ""))).resolve()
            resolved_path = path.resolve()
            if not resolved_path.is_relative_to(temp_root):
                raise PhaseGError(
                    "PHASE_G_LOCAL_ARTIFACT_INVALID",
                    "validated artifact is outside its D2 temporary root",
                )
            attempt.update(
                local_artifact_path=str(resolved_path),
                local_artifact_size_bytes=size_bytes,
                local_artifact_sha256=sha256,
                updated_at=_utc_now(),
            )
            return dict(attempt)

        return self._mutate(update)

    def accounted_today(self) -> Decimal:
        """Sum reserved or definitively spent D2 credits for the Bangkok date."""
        with self._locked_file():
            return self._accounted(self._read(), datetime.now(_BANGKOK).date().isoformat())

    def mark_artifact_verified(
        self,
        d2_run_id: str,
        *,
        remote_download: Path,
        remote_file_id: str,
        remote_filename: str,
    ) -> Mapping[str, object]:
        """Verify the downloaded remote copy itself before completing D2."""

        def update(payload: dict[str, object]) -> dict[str, object]:
            attempt = self._attempt(payload, d2_run_id)
            if attempt.get("submission_state") != "GENERATION_SUCCEEDED":
                raise PhaseGError(
                    "PHASE_G_ARTIFACT_RECOVERY_REQUIRED",
                    "exact provider output must be generated before artifact completion",
                )
            if not attempt.get("provider_media_id"):
                raise PhaseGError(
                    "PHASE_G_ARTIFACT_RECOVERY_REQUIRED",
                    "exact provider media identity is required before artifact completion",
                )
            local_path_value = attempt.get("local_artifact_path")
            temp_root_value = attempt.get("temp_root")
            if not isinstance(local_path_value, str) or not isinstance(temp_root_value, str):
                raise PhaseGError(
                    "PHASE_G_ARTIFACT_RECOVERY_REQUIRED",
                    "validated local artifact path is required before remote verification",
                )
            local_candidate = Path(local_path_value)
            temp_root = Path(temp_root_value).resolve()
            if local_candidate.is_symlink() or not local_candidate.is_file():
                raise PhaseGError(
                    "PHASE_G_LOCAL_ARTIFACT_INVALID",
                    "validated local artifact is missing or unsafe",
                )
            local_path = local_candidate.resolve()
            if not local_path.is_relative_to(temp_root):
                raise PhaseGError(
                    "PHASE_G_LOCAL_ARTIFACT_INVALID",
                    "validated local artifact escaped its D2 temp root",
                )
            try:
                evidence = verify_remote_redownload(
                    local_path,
                    remote_download,
                    remote_file_id=remote_file_id,
                    remote_filename=remote_filename,
                )
            except AssetVerificationError as exc:
                raise PhaseGError(
                    "PHASE_G_REMOTE_VERIFICATION_FAILED",
                    "remote re-download could not be verified against the local artifact",
                ) from exc
            if evidence.size_bytes != attempt.get(
                "local_artifact_size_bytes"
            ) or evidence.local_sha256 != attempt.get("local_artifact_sha256"):
                raise PhaseGError(
                    "PHASE_G_REMOTE_VERIFICATION_FAILED",
                    "remote verification differs from the recorded validated local artifact",
                )
            attempt.update(
                remote_file_id=evidence.remote_file_id,
                remote_filename=evidence.remote_filename,
                remote_size_bytes=evidence.size_bytes,
                local_sha256=evidence.local_sha256,
                remote_redownload_sha256=evidence.remote_sha256,
                remote_verified=True,
                submission_state="D2_SUCCEEDED",
                terminal_state="D2_SUCCEEDED",
                updated_at=_utc_now(),
            )
            return dict(attempt)

        return self._mutate(update)

    def _create(
        self,
        payload: dict[str, object],
        request: PhaseGExecutionRequest,
        *,
        retry_authorization: D2RetryAuthorization | None = None,
    ) -> dict[str, object]:
        attempts = cast(list[dict[str, object]], payload["attempts"])
        if any(row["d2_run_id"] == request.d2_run_id for row in attempts):
            raise PhaseGError("PHASE_G_DUPLICATE_RUN_ID", "D2 run ID already exists in the ledger")
        retry_predecessor: dict[str, object] | None = None
        if retry_authorization is not None:
            retry_predecessor = self._validate_retry_authorization(
                payload, request, retry_authorization
            )
        for previous in attempts:
            state = previous.get("submission_state")
            if state == "WAF_REJECTED":
                raise PhaseGError(
                    "WAF_REJECTED", "D2 live testing stopped after provider WAF refusal"
                )
            if state == "D2_SUCCEEDED":
                raise PhaseGError(
                    "PHASE_G_SUCCESS_ALREADY_PROVEN",
                    "D2 stops after its first verified artifact",
                )
            if state in {
                "UNKNOWN_SUBMISSION_STATE",
                "GENERATION_PENDING",
                "SUBMISSION_ACKNOWLEDGED",
            }:
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_UNRESOLVED", "prior D2 attempt requires reconciliation"
                )
            if state == "FORENSICALLY_CLOSED_UNKNOWN":
                # The old run remains permanently non-retryable, but its
                # explicit terminal closure permits a distinct future run ID.
                continue
            if state == "PERSISTENCE_FAILED":
                raise PhaseGError(
                    "PHASE_G_ARTIFACT_RECOVERY_REQUIRED",
                    "recover the already generated artifact before another submit",
                )
            if state == "GENERATION_SUCCEEDED":
                raise PhaseGError(
                    "PHASE_G_ARTIFACT_RECOVERY_REQUIRED",
                    "validate and persist the generated artifact before another submit",
                )
            generate_count = previous.get("generate_action_count", 0)
            if isinstance(generate_count, bool) or not isinstance(generate_count, int):
                raise PhaseGError(
                    "PHASE_G_LEDGER_INVALID", "stored Generate action count is invalid"
                )
            if generate_count > 0:
                if previous is retry_predecessor or self._misattribution_retry_chain_closed(
                    previous, attempts
                ):
                    continue
                raise PhaseGError(
                    "PHASE_G_PAID_ATTEMPT_ALREADY_STARTED",
                    "the authorized D2 live attempt has already used its Generate action",
                )
            if state not in _TERMINAL_STATES:
                raise PhaseGError("PHASE_G_ATTEMPT_UNRESOLVED", "prior D2 attempt is not terminal")
        attempt = self._new_attempt(request)
        if retry_authorization is not None:
            attempt["retry_authorization"] = {
                "contract_version": 1,
                "predecessor_d2_run_id": retry_authorization.predecessor_d2_run_id,
                "expected_terminal_state": retry_authorization.expected_terminal_state,
                "consumed_at": _utc_now(),
            }
        attempts.append(attempt)
        return dict(attempt)

    @staticmethod
    def _misattribution_retry_chain_closed(
        predecessor: Mapping[str, object], attempts: Sequence[Mapping[str, object]]
    ) -> bool:
        """Allow only the exact paid predecessor consumed by a validated closure chain."""
        predecessor_id = predecessor.get("d2_run_id")
        if predecessor.get("submission_state") != "UNEXPECTED_MULTI_OUTPUT":
            return False
        predecessor_indexes = [
            index for index, row in enumerate(attempts) if row.get("d2_run_id") == predecessor_id
        ]
        if len(predecessor_indexes) != 1:
            return False
        for retry_index, retry in enumerate(attempts):
            if retry_index <= predecessor_indexes[0]:
                continue
            authorization_value = retry.get("retry_authorization")
            if not isinstance(authorization_value, dict):
                continue
            authorization = cast(dict[str, object], authorization_value)
            if (
                authorization.get("contract_version") != 1
                or authorization.get("predecessor_d2_run_id") != predecessor_id
                or authorization.get("expected_terminal_state") != "UNEXPECTED_MULTI_OUTPUT"
                or not isinstance(authorization.get("consumed_at"), str)
                or retry.get("generate_action_count") != 1
                or retry.get("approval_action_count") != 0
                or retry.get("provider_project_id") != predecessor.get("provider_project_id")
                or retry.get("submission_state") != "FORENSICALLY_CLOSED_UNKNOWN"
                or retry.get("terminal_state") != "FORENSICALLY_CLOSED_UNKNOWN"
                or retry.get("attribution_invalidated") is not True
                or retry.get("artifact_attribution_valid") is not False
                or retry.get("forensic_closure_contract_version")
                != "D2_FORENSIC_MISATTRIBUTION_CLOSURE_V2"
                or retry.get("forensic_classification") != "NO_NEW_RETRY_OUTPUT_PROVEN"
            ):
                continue
            evidence = retry.get("forensic_closure_evidence")
            evidence_sha = retry.get("forensic_evidence_sha256")
            if not isinstance(evidence, dict) or not isinstance(evidence_sha, str):
                continue
            evidence = cast(dict[str, object], evidence)
            try:
                from .phase_g_forensic_misattribution_closure import (
                    forensic_misattribution_evidence_digest,
                )

                if (
                    evidence.get("D2_RUN_ID") == retry.get("d2_run_id")
                    and evidence.get("RETRY_PREDECESSOR") == predecessor_id
                    and evidence.get("IMPLEMENTATION_COMMIT")
                    == retry.get("forensic_implementation_commit")
                    and predecessor.get("terminal_state") == "UNEXPECTED_MULTI_OUTPUT"
                    and predecessor.get("generate_action_count") == 1
                    and forensic_misattribution_evidence_digest(evidence) == evidence_sha
                ):
                    return True
            except PhaseGError:
                continue
        return False

    def _validate_retry_authorization(
        self,
        payload: dict[str, object],
        request: PhaseGExecutionRequest,
        authorization: D2RetryAuthorization,
    ) -> dict[str, object]:
        attempts = cast(list[dict[str, object]], payload["attempts"])
        predecessor_id = authorization.predecessor_d2_run_id
        if (
            not _RUN_ID_RE.fullmatch(predecessor_id)
            or not predecessor_id.startswith("D2_")
            or predecessor_id == request.d2_run_id
            or authorization.expected_terminal_state != "UNEXPECTED_MULTI_OUTPUT"
        ):
            raise PhaseGError(
                "PHASE_G_RETRY_AUTHORIZATION_INVALID",
                "retry authorization must bind a distinct D2 and the supported failure state",
            )
        if any(row.get("retry_authorization") is not None for row in attempts):
            raise PhaseGError(
                "PHASE_G_RETRY_AUTHORIZATION_CONSUMED",
                "the ledger already contains a consumed one-time retry authorization",
            )
        predecessor = next(
            (row for row in attempts if row.get("d2_run_id") == predecessor_id), None
        )
        if predecessor is None:
            raise PhaseGError(
                "PHASE_G_RETRY_PREDECESSOR_NOT_FOUND",
                "the exact retry predecessor is not present in the ledger",
            )
        state = predecessor.get("submission_state")
        terminal_state = predecessor.get("terminal_state")
        generate_count = predecessor.get("generate_action_count")
        if state in {"UNKNOWN_SUBMISSION_STATE", "GENERATION_PENDING", "SUBMISSION_ACKNOWLEDGED"}:
            raise PhaseGError("PHASE_G_ATTEMPT_UNRESOLVED", "retry predecessor is unresolved")
        if state == "WAF_REJECTED":
            raise PhaseGError("WAF_REJECTED", "D2 live testing stopped after provider WAF refusal")
        if state == "D2_SUCCEEDED" or terminal_state == "D2_SUCCEEDED":
            raise PhaseGError(
                "PHASE_G_SUCCESS_ALREADY_PROVEN", "D2 stops after its first verified artifact"
            )
        if (
            state != authorization.expected_terminal_state
            or terminal_state != authorization.expected_terminal_state
            or state not in _TERMINAL_STATES
            or isinstance(generate_count, bool)
            or generate_count != 1
        ):
            raise PhaseGError(
                "PHASE_G_RETRY_PREDECESSOR_MISMATCH",
                "retry predecessor does not match the exact terminal authorization",
            )
        today = datetime.now(_BANGKOK).date().isoformat()
        if self._accounted(payload, today) >= Decimal(self.daily_budget):
            raise PhaseGError(
                "DAILY_FREE_CREDIT_BUDGET_EXHAUSTED",
                "Bangkok-day accounting already reaches the D2 credit ceiling",
            )
        return predecessor

    @staticmethod
    def _new_attempt(request: PhaseGExecutionRequest) -> dict[str, object]:
        now = _utc_now()
        directive = build_agent_only_directive(
            request.compiled_prompt_text,
            request.presubmit_request.preflight.technical_configuration,
        )
        attempt: dict[str, object] = {
            "d2_run_id": request.d2_run_id,
            "prerequisite_d2_run_id": None,
            "d6_prerequisite_proof": None,
            "d6_release_contract_version": _D6_RELEASE_CONTRACT_VERSION,
            "model_compatibility_sha256": request.model_compatibility_sha256,
            "scene_id": request.scene_id,
            "scene_prompt_id": request.scene_prompt_id,
            "generation_profile_id": request.generation_profile_id,
            "provider_id": request.provider_id,
            "provider_account_id": request.provider_account_id,
            "provider_project_id": request.provider_project_id,
            "model_id": request.model_id,
            "generation_mode": request.generation_mode,
            "source_context_sha256": request.source_context_sha256,
            "prompt_sha256": request.prompt_sha256,
            "approved_prompt_sha256": request.prompt_sha256,
            "provider_directive_sha256": hashlib.sha256(directive.encode("utf-8")).hexdigest(),
            "requested_duration_sec": request.duration_sec,
            "requested_aspect_ratio": request.aspect_ratio,
            "requested_output_count": request.output_count,
            "temp_root": str(request.presubmit_request.temp_root.resolve()),
            "started_at": now,
            "started_bangkok_date": datetime.now(_BANGKOK).date().isoformat(),
            "generate_action_count": 0,
            "generate_click_dispatched": None,
            "provider_submit_rpc_observed": None,
            "submission_surface": None,
            "agent_turn_dispatched": None,
            "provider_submit_rpc_id": None,
            "provider_submit_observed_at": None,
            "provider_submit_http_method": None,
            "provider_submit_project_id": None,
            "provider_submit_request_count": 0,
            "provider_submit_dispatch_window_completed": False,
            "provider_submit_request_failed": None,
            "provider_submit_response_observed": None,
            "provider_submit_response_status": None,
            "provider_submit_generation_record_observed": None,
            "provider_submit_refusal_detected": None,
            "provider_submit_transport_classification": None,
            "quoted_video_count": None,
            "quoted_credit_cost": None,
            "approval_action_count": 0,
            "observed_confirmation_id": None,
            "observed_video_count": None,
            "observed_credit_cost": None,
            "observed_visible_balance": None,
            "baseline_captured": False,
            "baseline_media_ids": [],
            "baseline_execution_ids": [],
            "baseline_pending_count": None,
            "baseline_confirmation_ids": [],
            "baseline_agent_turn_id": None,
            "baseline_agent_turn_bubble_hashes": [],
            "baseline_finished_tile_key_hashes": [],
            "baseline_chat_video_media_ids": [],
            "confirmation_id": None,
            "accounting_date": None,
            "accounted_credit_cost": "0",
            "submission_state": "PREPARED",
            "provider_execution_id": None,
            "provider_media_id": None,
            "provider_media_ids": [],
            "terminal_state": None,
            "updated_at": now,
        }
        return attempt

    def _accounted(self, payload: Mapping[str, object], date: str) -> Decimal:
        total = Decimal(0)
        for attempt in cast(list[dict[str, object]], payload["attempts"]):
            if attempt.get("accounting_date") == date:
                total += _decimal(attempt.get("accounted_credit_cost")) or Decimal(0)
        return total

    @staticmethod
    def _attempt(payload: dict[str, object], d2_run_id: str) -> dict[str, object]:
        for row in cast(list[dict[str, object]], payload["attempts"]):
            if row.get("d2_run_id") == d2_run_id:
                return row
        raise PhaseGError(
            "PHASE_G_ATTEMPT_NOT_FOUND", "D2 attempt does not exist in the local ledger"
        )

    def _mutate(
        self, operation: Callable[[dict[str, object]], dict[str, object]]
    ) -> dict[str, object]:
        with self._locked_file():
            payload = self._read()
            result = operation(payload)
            self._write(payload)
            return result

    def _read(self) -> dict[str, object]:
        if not self.path.exists():
            return {
                "schema_version": _LEDGER_SCHEMA,
                "daily_budget": self.daily_budget,
                "attempts": [],
            }
        if self.path.is_symlink():
            raise PhaseGError("PHASE_G_LEDGER_PATH_INVALID", "ledger may not be a symlink")
        try:
            payload_value: object = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PhaseGError("PHASE_G_LEDGER_CORRUPT", "D2 credit ledger is unreadable") from exc
        if not isinstance(payload_value, dict):
            raise PhaseGError("PHASE_G_LEDGER_CORRUPT", "D2 credit ledger has an invalid shape")
        payload = cast(dict[str, object], payload_value)
        if set(payload) != {
            "schema_version",
            "daily_budget",
            "attempts",
        }:
            raise PhaseGError("PHASE_G_LEDGER_CORRUPT", "D2 credit ledger has an invalid shape")
        if (
            payload["schema_version"] != _LEDGER_SCHEMA
            or payload["daily_budget"] != self.daily_budget
            or not isinstance(payload["attempts"], list)
        ):
            raise PhaseGError(
                "PHASE_G_LEDGER_CORRUPT", "D2 credit ledger version or budget is invalid"
            )
        return payload

    def _write(self, payload: dict[str, object]) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            root = self.root.resolve(strict=True)
            if root == Path(root.anchor) or self.path.resolve(strict=False).parent != root:
                raise PhaseGError("PHASE_G_LEDGER_PATH_INVALID", "D2 ledger path escaped its root")
            fd, temp_name = tempfile.mkstemp(prefix=".d2-ledger-", suffix=".tmp", dir=root)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                    json.dump(
                        payload,
                        stream,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, self.path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        except PhaseGError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise PhaseGError(
                "PHASE_G_LEDGER_WRITE_FAILED", "D2 credit ledger could not be saved"
            ) from exc

    def _locked_file(self):
        return _LedgerFileLock(self)


class _LedgerFileLock:
    """Cross-process lock for the single append-only D2 evidence file."""

    def __init__(self, ledger: D2DailyCreditLedger) -> None:
        self.ledger = ledger
        self.fd: int | None = None

    def __enter__(self):
        self.ledger.root.mkdir(parents=True, exist_ok=True)
        if self.ledger.root.is_symlink() or self.ledger.lock_path.is_symlink():
            raise PhaseGError(
                "PHASE_G_LEDGER_PATH_INVALID", "D2 ledger lock path may not be a symlink"
            )
        self.fd = os.open(self.ledger.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        if os.name == "nt":
            import msvcrt

            if os.fstat(self.fd).st_size == 0:
                os.write(self.fd, b"0")
            os.lseek(self.fd, 0, os.SEEK_SET)
            msvcrt.locking(self.fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_args: object) -> None:
        if self.fd is not None:
            if os.name == "nt":
                import msvcrt

                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def _decimal(value: object, *, allow_none: bool = False) -> Decimal | None:
    """Parse a finite non-negative credit count without rounding."""
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _counter(value: object) -> int:
    """Read a non-negative integer action count from persisted JSON."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PhaseGError("PHASE_G_LEDGER_CORRUPT", "D2 action count is invalid")
    return value


def _utc_now() -> str:
    return format_utc_timestamp(datetime.now(UTC), timespec="seconds")
